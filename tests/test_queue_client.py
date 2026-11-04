from contextlib import aclosing

import httpx
from pytest_httpx import HTTPXMock

from twscrape.accounts_pool import AccountsPool
from twscrape.queue_client import QueueClient

DB_FILE = "/tmp/twscrape_test_queue_client.db"
URL = "https://example.com/api"
CF = tuple[AccountsPool, QueueClient]


async def get_locked(pool: AccountsPool) -> set[str]:
    rep = await pool.get_all()
    return {x.username for x in rep if x.locks.get("SearchTimeline", None) is not None}


async def test_lock_account_when_used(httpx_mock: HTTPXMock, client_fixture):
    pool, client = client_fixture

    locked = await get_locked(pool)
    assert len(locked) == 0

    # should lock account on getting it
    await client.__aenter__()
    locked = await get_locked(pool)
    assert len(locked) == 1
    assert "user1" in locked

    # keep locked on request
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)
    assert (await client.get(URL)).json() == {"foo": "bar"}

    locked = await get_locked(pool)
    assert len(locked) == 1
    assert "user1" in locked

    # unlock on exit
    await client.__aexit__(None, None, None)
    locked = await get_locked(pool)
    assert len(locked) == 0


async def test_do_not_switch_account_on_200(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    # get account and lock it
    await client.__aenter__()
    locked1 = await get_locked(pool)
    assert len(locked1) == 1

    # make several requests with status=200
    for x in range(1):
        httpx_mock.add_response(url=URL, json={"foo": x}, status_code=200)
        rep = await client.get(URL)
        assert rep is not None
        assert rep.json() == {"foo": x}

    # account should not be switched
    locked2 = await get_locked(pool)
    assert locked1 == locked2

    # unlock on exit
    await client.__aexit__(None, None, None)
    locked3 = await get_locked(pool)
    assert len(locked3) == 0


async def test_switch_acc_on_http_error(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    # locked account on enter
    await client.__aenter__()
    locked1 = await get_locked(pool)
    assert len(locked1) == 1

    # fail one request, account should be switched
    httpx_mock.add_response(url=URL, json={"foo": "1"}, status_code=403)
    httpx_mock.add_response(url=URL, json={"foo": "2"}, status_code=200)

    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "2"}

    locked2 = await get_locked(pool)
    assert len(locked2) == 2

    # unlock on exit (failed account still should locked)
    await client.__aexit__(None, None, None)
    locked3 = await get_locked(pool)
    assert len(locked3) == 1
    assert locked1 == locked3  # failed account locked


async def test_retry_with_same_acc_on_network_error(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    # locked account on enter
    await client.__aenter__()
    locked1 = await get_locked(pool)
    assert len(locked1) == 1

    # timeout on first request, account should not be switched
    httpx_mock.add_exception(httpx.ReadTimeout("Unable to read within timeout"))
    httpx_mock.add_response(url=URL, json={"foo": "2"}, status_code=200)

    rep = await client.get(URL)
    assert rep is not None
    assert rep.json() == {"foo": "2"}

    locked2 = await get_locked(pool)
    assert locked2 == locked1

    # check username added to request obj (for logging)
    username = getattr(rep, "__username", None)
    assert username is not None


async def test_ctx_closed_on_break(httpx_mock: HTTPXMock, client_fixture: CF):
    pool, client = client_fixture

    async def get_data_stream():
        async with client as c:
            counter = 0
            while True:
                counter += 1
                check_retry = counter == 2
                before_ctx = c.ctx

                if check_retry:
                    httpx_mock.add_response(url=URL, json={"counter": counter}, status_code=403)
                    httpx_mock.add_response(url=URL, json={"counter": counter}, status_code=200)
                else:
                    httpx_mock.add_response(url=URL, json={"counter": counter}, status_code=200)

                rep = await c.get(URL)

                if check_retry:
                    assert before_ctx != c.ctx
                elif before_ctx is not None:
                    assert before_ctx == c.ctx

                assert rep is not None
                assert rep.json() == {"counter": counter}
                yield rep.json()["counter"]

                if counter == 9:
                    return

    # need to use async with to break to work
    async with aclosing(get_data_stream()) as gen:
        async for x in gen:
            if x == 3:
                break

    # ctx should be None after break
    assert client.ctx is None


async def test_logged_out_xclid_parks_account_and_uses_next(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """XClIdAccountError is about one account's session: park it on this queue and retry
    with the next account, which derives the key with its own cookies."""
    from tests.conftest import ClIdGenMock
    from twscrape.queue_client import XClIdGenStore
    from twscrape.xclid import XClIdAccountError

    pool, client = client_fixture
    asked: list[str] = []

    async def fake_get(cls, username, cookies=None, fresh=False):
        asked.append(username)
        if username == "user1":
            raise XClIdAccountError("Logged-out X web app")
        return ClIdGenMock()

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(fake_get))
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep is not None and rep.json() == {"foo": "bar"}
    assert asked == ["user1", "user2"]
    assert "user1" in await get_locked(pool)  # parked, not deactivated
    assert {a.username for a in await pool.get_all() if a.active} == {"user1", "user2"}
