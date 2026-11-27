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


# What a search-gated account sent for a day that has 43 tweets (captured live 2026-11-07).
TIMELINE_ISE = {
    "data": {"search_by_raw_query": {"search_timeline": {}}},
    "errors": [
        {
            "locations": [{"column": 7, "line": 4}],
            "message": "Internal server error",
            "path": ["search_by_raw_query", "search_timeline", "timeline"],
        }
    ],
}


async def test_timeline_internal_error_parks_account_and_uses_next(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    """A 200 with no timeline and an Internal server error on it is the gated account's
    other answer. Passed through, it ended the search as an empty result."""
    pool, client = client_fixture
    httpx_mock.add_response(url=URL, json=TIMELINE_ISE, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep is not None and rep.json() == {"foo": "bar"}
    assert await get_locked(pool) == {"user1"}  # parked on this queue
    assert {a.username for a in await pool.get_all() if a.active} == {"user1", "user2"}


async def test_timeline_internal_error_with_a_timeline_passes_through(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    """If the timeline did arrive, keep it: the error list alone is not a failure."""
    pool, client = client_fixture
    body = {
        "data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": []}}}},
        "errors": TIMELINE_ISE["errors"],
    }
    httpx_mock.add_response(url=URL, json=body, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep is not None and rep.json() == body


async def test_timeline_internal_error_from_every_account_fails_loudly(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """If every account gets it, X is failing the query: raise, never return empty."""
    import pytest

    from twscrape import queue_client
    from twscrape.pacer import EndpointRejectedError

    pool, client = client_fixture
    monkeypatch.setattr(queue_client, "TID_404_MAX_ACCOUNTS", 1)
    httpx_mock.add_response(url=URL, json=TIMELINE_ISE, status_code=200)
    httpx_mock.add_response(url=URL, json=TIMELINE_ISE, status_code=200)

    with pytest.raises(EndpointRejectedError, match="Internal server error"):
        async with client:
            await client.get(URL)


# -- a request X never answered must raise, never come back as an empty result -----------


async def test_xclid_failure_raises_instead_of_returning_empty(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    """2026-11-04: the transaction-id generator could not be built, every request was
    aborted, and each query came back as "no results". It must raise."""
    import pytest

    from twscrape.pacer import RequestAbortedError
    from twscrape.queue_client import AbortReqError, XClIdGenStore

    pool, client = client_fixture

    async def broken_get(cls, username, cookies=None, fresh=False):
        raise AbortReqError("Failed to create XClIdGen")

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(broken_get))

    with pytest.raises(RequestAbortedError, match="XClIdGen"):
        async with client:
            await client.get(URL)


# What a search-refused account sent (meo-airflow#346, 2026-11-24).
PRODUCT_DISABLED = {
    "errors": [
        {
            "code": -1,
            "message": "InvalidArgument: ProductDisabled: Closed gate stopped execution of the "
            "pipeline (LatestTabSrpProduct)",
        }
    ]
}


async def test_product_disabled_parks_account_and_uses_next(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    """It fell through to "API unknown error" and ended the search as an empty page."""
    pool, client = client_fixture
    httpx_mock.add_response(url=URL, json=PRODUCT_DISABLED, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep is not None and rep.json() == {"foo": "bar"}
    assert await get_locked(pool) == {"user1"}  # parked on this queue, not deactivated
    assert {a.username for a in await pool.get_all() if a.active} == {"user1", "user2"}


async def test_product_disabled_from_every_account_fails_loudly(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    import pytest

    from twscrape import queue_client
    from twscrape.pacer import EndpointRejectedError

    pool, client = client_fixture
    monkeypatch.setattr(queue_client, "TID_404_MAX_ACCOUNTS", 1)
    httpx_mock.add_response(url=URL, json=PRODUCT_DISABLED, status_code=200)
    httpx_mock.add_response(url=URL, json=PRODUCT_DISABLED, status_code=200)

    with pytest.raises(EndpointRejectedError, match="ProductDisabled"):
        async with client:
            await client.get(URL)


async def test_no_active_account_raises(client_fixture: CF):
    """With no active account the request returned None, i.e. an empty result."""
    import pytest

    from twscrape.accounts_pool import NoAccountError

    pool, client = client_fixture
    for name in ("user1", "user2"):
        await pool.set_active(name, False)

    with pytest.raises(NoAccountError):
        async with client:
            await client.get(URL)


# -- any error that comes back without content is a failure, whatever it says -------------

# A made-up message: the rule must not depend on the wording.
UNKNOWN_EMPTY = {
    "data": {"search_by_raw_query": {"search_timeline": {}}},
    "errors": [{"code": 999, "message": "SomethingNew: nobody has seen this one"}],
}


async def test_unknown_error_without_content_parks_account_and_uses_next(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    pool, client = client_fixture
    httpx_mock.add_response(url=URL, json=UNKNOWN_EMPTY, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep is not None and rep.json() == {"foo": "bar"}
    assert await get_locked(pool) == {"user1"}  # parked on this queue, not deactivated
    assert {a.username for a in await pool.get_all() if a.active} == {"user1", "user2"}


async def test_unknown_error_without_content_from_every_account_fails_loudly(
    httpx_mock: HTTPXMock, client_fixture: CF, monkeypatch
):
    import pytest

    from twscrape import queue_client
    from twscrape.pacer import EndpointRejectedError

    pool, client = client_fixture
    monkeypatch.setattr(queue_client, "TID_404_MAX_ACCOUNTS", 1)
    httpx_mock.add_response(url=URL, json=UNKNOWN_EMPTY, status_code=200)
    httpx_mock.add_response(url=URL, json=UNKNOWN_EMPTY, status_code=200)

    with pytest.raises(EndpointRejectedError, match="SomethingNew"):
        async with client:
            await client.get(URL)


async def test_error_on_timeline_path_with_user_around_it_is_a_failure(
    httpx_mock: HTTPXMock, client_fixture: CF
):
    """A user timeline keeps the user object around the missing timeline, so `data` is not
    empty -- the error's path is what says the timeline failed."""
    pool, client = client_fixture
    body = {
        "data": {"user": {"result": {"__typename": "User", "timeline": {}}}},
        "errors": [{"message": "Timeout: Unspecified", "path": ["user", "result", "timeline"]}],
    }
    httpx_mock.add_response(url=URL, json=body, status_code=200)
    httpx_mock.add_response(url=URL, json={"foo": "bar"}, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep.json() == {"foo": "bar"}
    assert await get_locked(pool) == {"user1"}


async def test_error_with_content_is_kept(httpx_mock: HTTPXMock, client_fixture: CF):
    """A partial error (here: a protected quoted tweet) with the main content present is
    not a failure: keep the response."""
    pool, client = client_fixture
    body = {
        "data": {"tweetResult": {"result": {"__typename": "Tweet", "rest_id": "1"}}},
        "errors": [{"message": "Authorization: Denied by access control", "path": ["quoted"]}],
    }
    httpx_mock.add_response(url=URL, json=body, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep.json() == body
    assert {a.username for a in await pool.get_all() if a.active} == {"user1", "user2"}


async def test_empty_timeline_with_an_error_is_an_answer():
    """`"instructions": []` is X saying "no results": content, not a failure."""
    from twscrape.queue_client import _failed_without_content

    empty_search = {
        "data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": []}}}},
        "errors": [{"message": "SomethingNew", "path": ["x"]}],
    }
    assert not _failed_without_content(empty_search)
    assert _failed_without_content(UNKNOWN_EMPTY)
    assert not _failed_without_content({"data": {}})  # no error: a plain empty answer


async def test_missing_status_is_a_genuine_not_found(httpx_mock: HTTPXMock, client_fixture: CF):
    """The allowlist: a deleted tweet answers with an error and no data, and that is the
    true answer, so no account is parked and no retry happens."""
    pool, client = client_fixture
    body = {"data": {}, "errors": [{"message": "_Missing: No status found with that ID"}]}
    httpx_mock.add_response(url=URL, json=body, status_code=200)

    async with client:
        rep = await client.get(URL)

    assert rep.json() == body
