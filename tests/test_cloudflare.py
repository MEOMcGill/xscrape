import time

import pytest
from pytest_httpx import HTTPXMock

from twscrape.pacer import CloudflareBackoff, CloudflareBlockedError, RequestStats
from twscrape.queue_client import QueueClient
from twscrape.utils import gather

URL = "https://example.com/api"

CF_HEADERS = {"content-type": "text/html", "cf-ray": "8f3a2b1c0d9e8f7a-YUL"}


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(CloudflareBackoff, "SCHEDULE", (0.01, 0.02))


async def test_cf_block_raises_after_retries(httpx_mock: HTTPXMock, client_fixture, monkeypatch):
    monkeypatch.setenv("XSCRAPE_CF_MAX_RETRIES", "1")
    _, client = client_fixture
    await client.__aenter__()

    httpx_mock.add_response(url=URL, status_code=429, html="<html>blocked</html>", headers=CF_HEADERS)
    httpx_mock.add_response(url=URL, status_code=429, html="<html>blocked</html>", headers=CF_HEADERS)

    with pytest.raises(CloudflareBlockedError):
        await client.get(URL)

    assert RequestStats.cf_blocks == 2
    assert RequestStats.requests_sent == 2
    await client.__aexit__(None, None, None)


async def test_cf_block_recovers_and_resets_streak(httpx_mock: HTTPXMock, client_fixture):
    _, client = client_fixture
    await client.__aenter__()

    httpx_mock.add_response(url=URL, status_code=429, html="<html>blocked</html>", headers=CF_HEADERS)
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

    start = time.monotonic()
    rep = await client.get(URL)
    elapsed = time.monotonic() - start

    assert rep is not None and rep.json() == {"foo": "bar"}
    assert elapsed >= 0.01 * 0.5  # served the (jittered) backoff window before retrying
    assert CloudflareBackoff._streak == 0  # success clears the streak
    assert RequestStats.cf_blocks == 1
    await client.__aexit__(None, None, None)


async def test_non_cf_html_block_keeps_abort_behavior(httpx_mock: HTTPXMock, client_fixture):
    # an HTML 4xx without cf-ray is not a Cloudflare wall: old silent-abort path
    _, client = client_fixture
    await client.__aenter__()

    httpx_mock.add_response(
        url=URL, status_code=403, html="<html>nope</html>", headers={"content-type": "text/html"}
    )

    assert await client.get(URL) is None
    assert RequestStats.cf_blocks == 0
    await client.__aexit__(None, None, None)


async def test_gather_raises_instead_of_partial(tmp_path, monkeypatch):
    # a CloudflareBlockedError from the request layer must escape the API
    # generators and gather() rather than ending iteration as if data ran out
    from twscrape.accounts_pool import AccountsPool
    from twscrape.api import API

    async def mock_get(self, url, params=None):
        raise CloudflareBlockedError("Cloudflare still blocking after 5 attempts")

    monkeypatch.setattr(QueueClient, "get", mock_get)

    pool = AccountsPool(db_file=str(tmp_path / "test.db"))
    await pool.add_account("u1", "p1", "e1", "ep1")
    await pool.set_active("u1", True)
    api = API(pool)

    with pytest.raises(CloudflareBlockedError):
        await gather(api.search("anything"))
