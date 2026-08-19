"""Tests for per-account fingerprint selection in the HTTP clients.

The curl backend drives its TLS/HTTP2 fingerprint (JA3) from the curl_cffi
impersonate target, not from the user-agent string. Accounts carry a private
`x-tws-impersonate` header (stored in accounts.db `headers`) so each can present
a distinct fingerprint. The header must never reach the wire, and the target
must follow account rotation on a reused session — that last point is the
regression that a construction-time-only implementation would silently fail.
"""

import pytest

from twscrape.http import CurlClient, HttpxClient


class _FakeResp:
    status_code = 200


# --- httpx backend ---------------------------------------------------------


def test_httpx_strips_impersonate_at_construction():
    clt = HttpxClient(headers={"x-tws-impersonate": "chrome131", "user-agent": "UA"})
    assert "x-tws-impersonate" not in clt.headers


async def test_httpx_strips_impersonate_on_request(monkeypatch):
    clt = HttpxClient(headers={"user-agent": "UA"})

    async def fake_request(method, url, **kwargs):
        return _FakeResp()

    monkeypatch.setattr(clt._client, "request", fake_request)
    # simulate apply_to_client re-injecting the hint on rotation
    clt._client.headers["x-tws-impersonate"] = "firefox135"
    await clt.request("GET", "https://example.test/")
    assert "x-tws-impersonate" not in clt._client.headers


# --- curl backend ----------------------------------------------------------


def _valid_targets():
    from typing import get_args

    from curl_cffi.requests import BrowserTypeLiteral

    return sorted(str(t) for t in get_args(BrowserTypeLiteral))


class _FakeCurlSession:
    """Records the impersonate target of construction and each request."""

    def __init__(self, *, impersonate=None, proxy=None, allow_redirects=True, headers=None):
        self.construct_impersonate = impersonate
        self.request_impersonate: list = []
        self.headers = dict(headers or {})
        self.cookies = {}

    async def request(self, method, url, **kwargs):
        self.request_impersonate.append(kwargs.get("impersonate"))
        return _FakeResp()

    async def close(self):
        pass


@pytest.fixture
def fake_curl(monkeypatch):
    pytest.importorskip("curl_cffi")
    import curl_cffi.requests as cr

    monkeypatch.setattr(cr, "AsyncSession", _FakeCurlSession)
    return cr


async def test_curl_selects_account_target(fake_curl):
    valid = _valid_targets()
    target = valid[0]
    clt = CurlClient(headers={"x-tws-impersonate": target, "user-agent": "UA"})
    # construction seeds the target and strips the hint from the wire headers
    assert clt._session.construct_impersonate == target
    assert "x-tws-impersonate" not in clt._session.headers

    await clt.request("GET", "https://example.test/")
    assert clt._session.request_impersonate[-1] == target


async def test_curl_invalid_target_falls_back(fake_curl):
    clt = CurlClient(headers={"x-tws-impersonate": "not-a-real-target", "user-agent": "UA"})
    # unknown target → the resolved family default, never the bogus value
    assert clt._impersonate == clt._default_target
    await clt.request("GET", "https://example.test/")
    assert clt._session.request_impersonate[-1] == clt._default_target


async def test_curl_impersonate_follows_rotation(fake_curl):
    """The key regression: reusing one session across accounts must still swap
    the JA3. apply_to_client re-injects x-tws-impersonate onto the session
    headers; request() must consume it and change the impersonate target."""
    valid = _valid_targets()
    first, second = valid[0], valid[-1]
    assert first != second

    clt = CurlClient(headers={"x-tws-impersonate": first, "user-agent": "UA"})
    await clt.request("GET", "https://example.test/")
    assert clt._session.request_impersonate[-1] == first

    # simulate Account.apply_to_client on rotation: header re-injected, no rebuild
    clt._session.headers["x-tws-impersonate"] = second
    await clt.request("GET", "https://example.test/")
    assert clt._session.request_impersonate[-1] == second
    # and the hint is consumed, never left to leak on the wire
    assert "x-tws-impersonate" not in clt._session.headers
