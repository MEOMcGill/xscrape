import dataclasses
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

from httpx import AsyncClient, AsyncHTTPTransport

from .models import JSONTrait
from .utils import utc

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

    def apply_to_client(self, client: AsyncClient) -> None:
        """Load this account's cookies/headers onto an existing client.

        Called when rotating accounts without tearing down the TCP/TLS pool.
        Clears any previous account's cookie jar first so requests don't go
        out with stale auth — if we only `update`, the x-csrf-token header
        ends up on a fresh cookie jar (no `ct0`) and X rejects the request.
        """
        client.cookies.clear()
        client.cookies.update(self.cookies)
        client.headers.update(self.headers)
        client.headers["user-agent"] = self.user_agent
        client.headers["content-type"] = "application/json"
        client.headers["authorization"] = TOKEN
        client.headers["x-twitter-active-user"] = "yes"
        client.headers["x-twitter-client-language"] = "en"
        if "ct0" in client.cookies:
            client.headers["x-csrf-token"] = client.cookies["ct0"]
        else:
            client.headers.pop("x-csrf-token", None)

    def make_client(self, proxy: str | None = None) -> AsyncClient:
        proxies = [proxy, os.getenv("TWS_PROXY"), self.proxy]
        proxies = [x for x in proxies if x is not None]
        proxy = proxies[0] if proxies else None

        transport = AsyncHTTPTransport(retries=3)
        client = AsyncClient(proxy=proxy, follow_redirects=True, transport=transport)
        self.apply_to_client(client)
        return client
