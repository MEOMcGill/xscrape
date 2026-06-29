import dataclasses
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .http import HttpClient
from .http import make_client as _make_http_client
from .models import JSONTrait
from .utils import parse_proxy, utc

TOKEN = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"


@dataclass
class Account(JSONTrait):
    username: str
    password: str
    email: str
    email_password: str
    user_agent: str
    active: bool
    locks: dict[str, datetime] = field(default_factory=dict)  # queue: datetime
    stats: dict[str, int] = field(default_factory=dict)  # queue: requests
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    mfa_code: str | None = None
    proxy: str | None = None
    error_msg: str | None = None
    last_used: datetime | None = None
    _tx: str | None = None

    @staticmethod
    def from_rs(rs: sqlite3.Row):
        doc = dict(rs)
        # Legacy column name from custom twscrape; keep 2FA secret under mfa_code.
        if "twofa_id" in doc and doc.get("mfa_code") is None:
            doc["mfa_code"] = doc["twofa_id"]
        # Drop columns that are not fields on the Account dataclass (e.g. num_calls,
        # in_use, use_case, last_login, automated from a custom twscrape migration).
        known = {f.name for f in dataclasses.fields(Account)}
        doc = {k: v for k, v in doc.items() if k in known}
        doc["locks"] = {k: utc.from_iso(v) for k, v in json.loads(doc["locks"]).items()}
        doc["stats"] = {k: v for k, v in json.loads(doc["stats"]).items() if isinstance(v, int)}
        doc["headers"] = json.loads(doc["headers"])
        doc["cookies"] = json.loads(doc["cookies"])
        doc["active"] = bool(doc["active"])
        doc["last_used"] = utc.from_iso(doc["last_used"]) if doc["last_used"] else None
        return Account(**doc)

    def to_rs(self):
        rs = asdict(self)
        rs["locks"] = json.dumps(rs["locks"], default=lambda x: x.isoformat())
        rs["stats"] = json.dumps(rs["stats"])
        rs["headers"] = json.dumps(rs["headers"])
        rs["cookies"] = json.dumps(rs["cookies"])
        rs["last_used"] = rs["last_used"].isoformat() if rs["last_used"] else None
        return rs

    def _auth_headers(self) -> dict[str, str]:
        headers = {**self.headers}
        headers["user-agent"] = self.user_agent
        headers["content-type"] = "application/json"
        headers["authorization"] = TOKEN
        headers["x-twitter-active-user"] = "yes"
        headers["x-twitter-client-language"] = "en"
        if "ct0" in self.cookies:
            headers["x-csrf-token"] = self.cookies["ct0"]
        return headers

    def apply_to_client(self, client: HttpClient) -> None:
        """Load this account's cookies/headers onto an existing client.

        Called when rotating accounts without tearing down the TCP/TLS pool.
        Clears any previous account's cookie jar first so requests don't go
        out with stale auth — if we only `update`, the x-csrf-token header
        ends up on a fresh cookie jar (no `ct0`) and X rejects the request.
        """
        client.cookies.clear()
        client.cookies.update(self.cookies)
        headers = client.headers
        headers.update(self.headers)
        # curl-cffi sets its own user-agent to match the impersonated TLS
        # fingerprint; overriding it would create a UA/fingerprint mismatch.
        if client.backend != "curl":
            headers["user-agent"] = self.user_agent
        headers["content-type"] = "application/json"
        headers["authorization"] = TOKEN
        headers["x-twitter-active-user"] = "yes"
        headers["x-twitter-client-language"] = "en"
        if "ct0" in self.cookies:
            headers["x-csrf-token"] = self.cookies["ct0"]
        else:
            headers.pop("x-csrf-token", None)

    def make_client(self, proxy: str | None = None) -> HttpClient:
        proxies = [proxy, os.getenv("TWS_PROXY"), self.proxy]
        proxies = [x for x in proxies if x is not None]
        proxy = parse_proxy(proxies[0]) if proxies else None

        seed = int(hashlib.sha256(self.username.encode()).hexdigest()[:8], 16)
        return _make_http_client(
            proxy=proxy, headers=self._auth_headers(), cookies=self.cookies, seed=seed
        )
