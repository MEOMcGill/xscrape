import asyncio
import json
import os
import time
from typing import Any
from urllib.parse import urlparse

from .accounts_pool import Account, AccountsPool, NoAccountError
from .http import ConnectError, HttpClient, HttpMethod, HttpStatusError, NetworkError, Response
from .logger import logger
from .pacer import (
    CloudflareBackoff,
    CloudflareBlockedError,
    EndpointRejectedError,
    RequestAbortedError,
    RequestPacer,
    RequestStats,
)
from .utils import find_obj, utc
from .xclid import XClIdAccountError, XClIdGen

ReqParams = dict[str, str | int] | None
TMP_TS = utc.now().isoformat().split(".")[0].replace("T", "_").replace(":", "-")[0:16]


# A gated "Internal server error" (and any other error with no content) parks the account
# on that queue this long. The empty 404 and ProductDisabled suspend it there instead.
TID_404_LOCK_SECONDS = 60 * 15
# At this many consecutive refused accounts, the query fails (EndpointRejectedError).
TID_404_MAX_ACCOUNTS = 5

# Same idea for XClIdAccountError: past this many accounts in a row whose cookies get the
# logged-out page, the problem is not those accounts.
XCLID_ACCOUNT_MAX = 5

# An account X refuses an endpoint -- empty 404, or 200 "ProductDisabled: Closed gate" -- is
# suspended on that endpoint (queue) for good (AccountsPool.suspend, lock value -1), never
# deactivated: its other endpoints still work. Measured 2026-11-27: 79 of 86 pool accounts
# suspended by X since 11-21, every one still serving timelines, and answering search with
# one of those two. Short parks sent them back to be refused all week (8,656 ProductDisabled
# refusals in one crawl, 11-24/25). Once every account is suspended on an endpoint, the
# pool's "no active accounts" check stops the query instead of waiting.


class HandledError(Exception): ...


class AbortReqError(Exception):
    """Internal: give up on this request. req() re-raises it as RequestAbortedError."""


class AccountRefusedError(Exception):
    """X refused this account this endpoint ("ProductDisabled: Closed gate"). The
    account may still work elsewhere, so it is parked on this queue only."""


class TxIdRejectedError(Exception):
    """This account got a 404 from this endpoint even after fresh transaction ids."""


class TimelineFailedError(Exception):
    """This account got a 200 whose timeline X failed to build: an Internal server error
    on the timeline path and no timeline in the data."""


class NoContentError(TimelineFailedError):
    """X reported an error and sent nothing usable back, whatever the error said. Handled
    like TimelineFailedError: park the account on this queue, retry on the next one."""


def _is_empty(obj: Any) -> bool:
    """None, or containers holding nothing but other empty containers. Any scalar counts
    as content (a __typename, an id, a cursor value)."""
    if obj is None:
        return True
    if isinstance(obj, dict):
        return all(_is_empty(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_is_empty(v) for v in obj)
    return False


def _failed_without_content(res: Any) -> bool:
    """An error came back, and the content the request was for did not.

    Two shapes: `data` holds nothing at all (`{"search_timeline": {}}` and the like), or
    an error sits on a `timeline` path and no timeline arrived -- a user-timeline response
    still carries the user object around the missing timeline, so `data` is not empty.
    Deliberately blind to the error's wording: every message that slipped through so far
    was one nobody had seen before."""
    if not isinstance(res, dict) or not isinstance(res.get("errors"), list) or not res["errors"]:
        return False
    data = res.get("data")
    # A timeline that arrived is X's answer, even an empty one (`"instructions": []` is a
    # search with no results): keep it, whatever the error list says.
    if isinstance(data, dict) and find_obj(data, lambda x: "instructions" in x) is not None:
        return False
    if _is_empty(data):
        return True
    return any(
        isinstance(e, dict) and (e.get("path") or [])[-1:] == ["timeline"] for e in res["errors"]
    )


def _timeline_failed(res: Any) -> bool:
    if not isinstance(res, dict) or not isinstance(res.get("errors"), list):
        return False
    failed = any(
        isinstance(e, dict)
        and "Internal server error" in str(e.get("message", ""))
        and (e.get("path") or [])[-1:] == ["timeline"]
        for e in res["errors"]
    )
    # A timeline that did arrive (instructions present) is kept, whatever the error list says.
    data = res.get("data")
    has_timeline = (
        find_obj(data, lambda x: "instructions" in x) if isinstance(data, dict) else None
    )
    return failed and has_timeline is None


class CloudflareBlockSignal(Exception):
    """Internal: one response was Cloudflare-blocked. req() converts this into a
    pool-wide backoff + retry, and into CloudflareBlockedError once retries run out."""


class XClIdGenStore:
    # The generator is account-agnostic: XClIdGen.calc() takes only (method, path)
    # plus per-call time/randomness, no username. So a single instance is shared by
    # every account in the process rather than one per username. Deriving it costs
    # ~300 requests to abs.twimg.com (scanning bundle chunks for the sign.o-*.js
    # indices file), so we cache it and only re-derive when it goes stale (soft TTL)
    # or a caller reports it broken (fresh=True, driven by 404s in Ctx.req).
    # See https://github.com/vladkens/twscrape/issues/312
    #
    # Since 2026-11-04 it must be derived with an account's cookies (only the logged-in web
    # app still carries the indices script; see xclid._make_client). The account that
    # happens to trigger a derivation lends its cookies; the result is still shared. With
    # the logged-in (legacy) build the indices file is found directly in the page's chunk
    # map, so a derivation is ~2 requests, not the ~300-chunk scan described above.
    _gen: XClIdGen | None = None
    _created_at: float = 0.0
    _lock = asyncio.Lock()

    TTL = 3 * 60 * 60  # soft expiry (s): re-derive if the key is older than this
    MIN_AGE = 60  # ignore a fresh=True refresh if the key is younger than this (s)

    @classmethod
    async def get(
        cls, username: str, cookies: dict[str, str] | None = None, fresh: bool = False
    ) -> XClIdGen:
        # `username` is kept for signature compatibility only; the key is global.
        # Fast path: a non-expired key exists and the caller isn't forcing a refresh.
        if cls._gen is not None and not fresh and time.monotonic() - cls._created_at < cls.TTL:
            return cls._gen

        async with cls._lock:
            # Re-check under the lock — another coroutine may have refreshed while
            # we waited, in which case we reuse its result instead of stampeding.
            if cls._gen is not None:
                age = time.monotonic() - cls._created_at
                # A fresh=True request only earns a re-derive if the current key is
                # old enough to be worth replacing. This stops a burst of 404s from
                # every account each triggering its own ~300-request scan.
                if fresh and age < cls.MIN_AGE:
                    return cls._gen
                if not fresh and age < cls.TTL:
                    return cls._gen

            tries = 0
            while tries < 3:
                try:
                    cls._gen = await XClIdGen.create(cookies=cookies)
                    cls._created_at = time.monotonic()
                    return cls._gen
                except XClIdAccountError:
                    # This account's session is the problem; the same cookies will fail
                    # again. Keep a cached key if there is one, else let the caller park
                    # the account so the next one derives with its own cookies.
                    if cls._gen is not None:
                        return cls._gen
                    raise
                except Exception as e:
                    tries += 1
                    logger.warning(
                        f"XClIdGen creation attempt {tries}/3 failed: {type(e).__name__}: {e}"
                    )
                    await asyncio.sleep(1)

            # Derivation failed. A stale key beats aborting the request outright:
            # the soft TTL is conservative, so an "expired" key is often still valid,
            # and even for a 404-driven refresh the caller's own retry loop bounds it.
            if cls._gen is not None:
                logger.warning("XClIdGen refresh failed; falling back to cached key")
                return cls._gen

            # From here every request is aborted. It used to reach the caller as an empty
            # result, which on 2026-11-04 let a whole nightly run "succeed" with 0 tweets;
            # req() now raises it as RequestAbortedError.
            logger.error("XClIdGen creation failed 3 times; aborting the request")
            raise AbortReqError(
                "Failed to create XClIdGen. See: https://github.com/vladkens/twscrape/issues/248"
            )


class Ctx:
    def __init__(self, acc: Account, clt: HttpClient):
        self.req_count = 0
        self.acc = acc
        self.clt = clt

    async def aclose(self):
        await self.clt.aclose()

    async def req(self, method: HttpMethod, url: str, params: ReqParams = None) -> Response:
        # if code 404 on first try then generate new x-client-transaction-id and retry
        # https://github.com/vladkens/twscrape/issues/248
        path = urlparse(url).path or "/"

        tries = 0
        while tries < 3:
            gen = await XClIdGenStore.get(
                self.acc.username, cookies=self.acc.cookies, fresh=tries > 0
            )
            hdr = {"x-client-transaction-id": gen.calc(method, path)}
            rep = await self.clt.request(method, url, params=params, headers=hdr)
            if rep.status_code != 404:
                return rep

            tries += 1
            logger.debug(f"Retrying request with new x-client-transaction-id: {url}")
            await asyncio.sleep(1)

        raise TxIdRejectedError()


def req_id(rep: Response):
    lr = str(rep.headers.get("x-rate-limit-remaining", -1))
    ll = str(rep.headers.get("x-rate-limit-limit", -1))
    sz = max(len(lr), len(ll))
    lr, ll = lr.rjust(sz), ll.rjust(sz)

    username = getattr(rep, "__username", "<UNKNOWN>")
    return f"{lr}/{ll} - {username}"


def dump_rep(rep: Response):
    count = getattr(dump_rep, "__count", -1) + 1
    setattr(dump_rep, "__count", count)

    acc = getattr(rep, "__username", "<unknown>")
    outfile = f"{count:05d}_{rep.status_code}_{acc}.txt"
    outfile = f"/tmp/twscrape-{TMP_TS}/{outfile}"
    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    msg = []
    msg.append(f"{count:,d} - {req_id(rep)}")
    msg.append(f"{rep.status_code} {rep.request.method} {rep.request.url}")
    msg.append("\n")
    # msg.append("\n".join([str(x) for x in list(rep.request.headers.items())]))
    msg.append("\n".join([str(x) for x in list(rep.headers.items())]))
    msg.append("\n")

    try:
        msg.append(json.dumps(rep.json(), indent=2))
    except json.JSONDecodeError:
        msg.append(rep.text)

    txt = "\n".join(msg)
    with open(outfile, "w") as f:
        f.write(txt)


class QueueClient:
    def __init__(
        self,
        pool: AccountsPool,
        queue: str,
        debug=False,
        proxy: str | None = None,
        iterate_accounts: bool = False,
    ):
        self.pool = pool
        self.queue = queue
        self.debug = debug
        self.ctx: Ctx | None = None
        self.proxy = proxy
        # When True, release the current account after every successful request so
        # the next request picks a different one from the pool. The account's
        # existing queue lock (set by _get_and_lock using endpoint_to_spread) is
        # left in place — that lock is what keeps the pool from re-picking it
        # immediately, letting ORDER BY username rotate through the rest.
        self.iterate_accounts = iterate_accounts

    async def __aenter__(self):
        await self._get_ctx()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._close_ctx()

    async def _close_ctx(
        self, reset_at=-1, inactive=False, msg: str | None = None, suspend: bool = False
    ):
        if self.ctx is None:
            return

        ctx, self.ctx, self.req_count = self.ctx, None, 0
        username = ctx.acc.username
        await ctx.aclose()

        if inactive:
            await self.pool.mark_inactive(username, msg)
            return

        if suspend:  # off this queue for good; see AccountsPool.suspend
            await self.pool.suspend(username, self.queue, ctx.req_count)
            return

        if reset_at > 0:
            await self.pool.lock_until(ctx.acc.username, self.queue, reset_at, ctx.req_count)
            return

        await self.pool.unlock(ctx.acc.username, self.queue, ctx.req_count)

    async def _rotate_ctx(self):
        """Swap to a different account while keeping the HTTP client (and its TCP/TLS
        connection pool) open. The old account's existing queue lock is left in
        place so the pool won't re-pick it immediately; it expires naturally per
        endpoint_to_spread. If no other account is available right now we just
        keep the current one and retry on the next request."""
        if self.ctx is None:
            return

        old_username = self.ctx.acc.username
        new_acc = await self.pool.get_for_queue(self.queue)
        if new_acc is None or new_acc.username == old_username:
            return

        new_acc.apply_to_client(self.ctx.clt)
        self.ctx.acc = new_acc
        self.ctx.req_count = 0

    async def _get_ctx(self):
        if self.ctx:
            return self.ctx

        acc = await self.pool.get_for_queue_or_wait(self.queue)
        if acc is None:
            return None

        clt = acc.make_client(proxy=self.proxy)
        self.ctx = Ctx(acc, clt)
        return self.ctx

    async def _check_rep(self, rep: Response) -> None:
        """
        This function can raise Exception and request will be retried or aborted
        Or if None is returned, response will passed to api parser as is
        """

        if self.debug:
            dump_rep(rep)

        if "text/html" in rep.headers.get("content-type", "") and rep.status_code >= 400:
            src = "Cloudflare" if "cf-ray" in rep.headers else "HTML"
            logger.warning(f"Blocked by {src}: {rep.status_code} - {req_id(rep)}")
            if src == "Cloudflare":
                raise CloudflareBlockSignal()
            raise AbortReqError(
                f"blocked by an HTML error page: {rep.status_code} - {req_id(rep)}"
            )

        try:
            res = rep.json()
        except json.JSONDecodeError:
            res: Any = {"_raw": rep.text}

        limit_remaining = int(rep.headers.get("x-rate-limit-remaining", -1))
        limit_reset = int(rep.headers.get("x-rate-limit-reset", -1))
        # limit_max = int(rep.headers.get("x-rate-limit-limit", -1))

        err_msg = "OK"
        if "errors" in res:
            err_msg = {f"({x.get('code', -1)}) {x['message']}" for x in res["errors"]}
            err_msg = "; ".join(list(err_msg))

        log_msg = f"{rep.status_code:3d} - {req_id(rep)} - {err_msg}"
        logger.trace(log_msg)

        # for dev: need to add some features in api.py
        if err_msg.startswith("(336) The following features cannot be null"):
            logger.error(f"[DEV] Update required: {err_msg}")
            exit(1)

        # general api rate limit
        if limit_remaining == 0 and limit_reset > 0:
            logger.debug(f"Rate limited: {log_msg}")
            await self._close_ctx(limit_reset)
            raise HandledError()

        # no way to check is account banned in direct way, but this check should work
        if err_msg.startswith("(88) Rate limit exceeded") and limit_remaining > 0:
            logger.warning(f"Ban detected: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if err_msg.startswith("(326) Authorization: Denied by access control"):
            logger.warning(f"Ban detected: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if err_msg.startswith("(32) Could not authenticate you"):
            logger.warning(f"Session expired or banned: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if err_msg == "OK" and rep.status_code == 403:
            # Deactivation is sticky until someone logs the account back in, so record WHY.
            # msg=None used to leave error_msg NULL, making a 403 deactivation indistinguishable
            # from an account that was never touched -- 19 accounts vanished from a pool this
            # way with no trace of the cause. The queue is worth keeping too: 403 may prove to
            # be endpoint-specific (as the empty-bodied 404 turned out to be), and that is only
            # checkable if the endpoint was written down.
            msg = f"(403) Forbidden, empty error body, on {self.queue}"
            logger.warning(f"Session expired or banned: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=msg)
            raise HandledError()

        # something from twitter side - abort all queries, see: https://github.com/vladkens/twscrape/pull/80
        if err_msg.startswith("(131) Dependency: Internal error"):
            # looks like when data exists, we can ignore this error
            # https://github.com/vladkens/twscrape/issues/166
            if rep.status_code == 200 and "data" in res and "user" in res["data"]:
                err_msg = "OK"
            else:
                logger.warning(f"Dependency error (request skipped): {err_msg}")
                raise AbortReqError(f"dependency error with no data: {log_msg}")

        # A search-gated account does not always answer with the empty 404. About as often it
        # answers 200 with
        #   {"data":{"search_by_raw_query":{"search_timeline":{}}},
        #    "errors":[{"message":"Internal server error","path":[..., "timeline"]}]}
        # which has no entries and no cursor. Passed through, _gql_items took it for the end
        # of the results, so the query came back empty or cut short with only a WARNING to
        # show for it. Measured 2026-11-07 on a 142k-day search run: 12,188 of 12,195 such
        # responses came from the 43 accounts that were also getting the gated 404, and a
        # day one of them answered this way returned 43 tweets on a non-gated account. So
        # it gets the gated-404 treatment: park this account on this queue, retry elsewhere.
        if rep.status_code == 200 and _timeline_failed(res):
            raise TimelineFailedError(log_msg)

        # "(-1) InvalidArgument: ProductDisabled: Closed gate stopped execution of the
        # pipeline (LatestTabSrp...)": X refuses this account Latest search. It fell
        # through to "API unknown error" below and was passed on as an empty page, so the
        # query ended with no results. In the 2026-11-24/25 qc_election snap crawl, 62
        # accounts were refused 8,656 times, and 11,711 of 13,521 keyword units saved 0
        # tweets (meo-airflow#346). Park the account on this queue and retry the query on
        # the next one.
        if "ProductDisabled" in err_msg:
            raise AccountRefusedError(log_msg)

        # content not found: a genuine answer (deleted / never-existing tweet), not a failure.
        # The allowlist for the rule below -- add other known "not found" answers here.
        if rep.status_code == 200 and "_Missing: No status found with that ID" in err_msg:
            return  # ignore this error

        # Any other error that came back without the content asked for is a failure. The
        # checks above each name one message, and each was added only after an unknown
        # message had passed as an empty result and lost data: the gated "Internal server
        # error" (2026-11-07), "ProductDisabled" (2026-11-24). This one does not need to
        # know the message. GraphQL answers 200 with partial errors routinely, so an error
        # alone is not enough -- an error *and* nothing usable is.
        if err_msg != "OK" and _failed_without_content(res):
            raise NoContentError(log_msg)

        # Errors that arrived with content are partial errors (a protected quoted tweet, an
        # optional field): keep the content. See https://github.com/vladkens/twscrape/pull/95
        if rep.status_code == 200 and "Authorization" in err_msg:
            logger.warning(f"Authorization unknown error: {log_msg}")
            return

        if err_msg != "OK":
            logger.warning(f"API unknown error (content kept): {log_msg}")
            return

        try:
            rep.raise_for_status()
        except HttpStatusError:
            logger.error(f"Unhandled API response code: {log_msg}")
            await self._close_ctx(utc.ts() + 60 * 15)  # 15 minutes
            raise HandledError()

    async def get(self, url: str, params: ReqParams = None) -> Response | None:
        return await self.req("GET", url, params=params)

    async def req(self, method: HttpMethod, url: str, params: ReqParams = None) -> Response | None:
        unknown_retry, connection_retry, cf_retry, tid_404, xclid_acc = 0, 0, 0, 0, 0
        timeline_failed, refused = 0, 0

        while True:
            ctx = await self._get_ctx()  # not need to close client, class implements __aexit__
            if ctx is None:
                # The pool has no active account at all (get_for_queue_or_wait already
                # waits out locks). Returning None here ended the query as "no results".
                raise NoAccountError(f"No active accounts for queue {self.queue}")

            try:
                # Cloudflare blocks are per-IP, so all requests share one backoff
                # deadline; the pacer then spaces every request start (retries
                # included) process-wide to stay under Cloudflare's per-IP budget
                await CloudflareBackoff.wait_if_blocked()
                await RequestPacer.wait()
                RequestStats.requests_sent += 1
                rep = await ctx.req(method, url, params=params)
                setattr(rep, "__username", ctx.acc.username)
                await self._check_rep(rep)

                CloudflareBackoff.record_success()
                ctx.req_count += 1  # count only successful
                unknown_retry, connection_retry = 0, 0
                if self.iterate_accounts:
                    await self._rotate_ctx()
                return rep
            except CloudflareBlockSignal:
                RequestStats.cf_blocks += 1
                CloudflareBackoff.record_block()
                cf_retry += 1
                if cf_retry > CloudflareBackoff.max_retries():
                    raise CloudflareBlockedError(
                        f"Cloudflare still blocking after {cf_retry} attempts"
                    ) from None
                continue  # wait_if_blocked at loop top serves the backoff
            except TimelineFailedError as e:
                # The other face of per-account gating (see _check_rep), and any other error
                # that came back without content (NoContentError). Its own counter and
                # message: if every account answers this way, X is failing the query itself,
                # and that has to surface as an error -- not as an empty result.
                timeline_failed += 1
                if timeline_failed >= TID_404_MAX_ACCOUNTS:
                    raise EndpointRejectedError(
                        f"{self.queue}: an error and no content from {timeline_failed} accounts "
                        f"in a row -- X is failing this query. Last: {e}"
                    ) from None
                if isinstance(e, NoContentError):
                    # WARNING, not INFO: a message no check names yet. Worth reading.
                    logger.warning(f"Error with no content, parking on {self.queue}: {e}")
                else:
                    logger.info(f"Timeline Internal server error, parking on {self.queue}: {e}")
                await self._close_ctx(utc.ts() + TID_404_LOCK_SECONDS)
                continue
            except TxIdRejectedError:
                # An empty-bodied 404 with healthy rate-limit headers means X denies this
                # account this endpoint; retrying it is futile, so park it (per-queue, so it
                # stays usable elsewhere) and take the next account. Without this the whole
                # query aborted on the first gated account -- and since the pool hands out
                # accounts least-recently-used first, one gated account at the head of the
                # queue silently zeroed every search.
                #
                # Suspended on this endpoint right away (see SUSPENDED_ON_QUEUE above). An empty
                # 404 from many accounts can also mean the operation id went stale (a few
                # times a year); that is checked by hand, and whoever updates the op id clears
                # the suspensions (`twscrape reset_locks`).
                tid_404 += 1
                logger.warning(f"Empty 404, suspended on {self.queue}: {ctx.acc.username}")
                await self._close_ctx(suspend=True)
                if tid_404 >= TID_404_MAX_ACCOUNTS:
                    raise EndpointRejectedError(
                        f"{self.queue}: empty 404 from {tid_404} accounts in a row, each "
                        "suspended on it -- the accounts are gated or suspended by X, or the "
                        "operation id is stale (check the request format if this keeps happening)"
                    ) from None
                continue
            except XClIdAccountError as e:
                # X served this account the logged-out app: its session did not authenticate
                # the page fetch. Park it on this queue (not deactivate -- relogin is
                # destructive on this pool) and let the next account derive the key.
                xclid_acc += 1
                if xclid_acc > XCLID_ACCOUNT_MAX:
                    raise XClIdAccountError(
                        f"{self.queue}: logged-out web app for {xclid_acc} accounts in a row"
                    ) from None
                logger.warning(f"{e}; username={ctx.acc.username}; queue={self.queue}")
                await self._close_ctx(utc.ts() + 60 * 15)
                continue
            except AccountRefusedError as e:
                # X names the refusal ("ProductDisabled"): suspend the account on this queue
                # right away, take the next account, and at a run of refusals say that X is
                # refusing this endpoint to the pool.
                refused += 1
                logger.warning(f"Account refused, suspended on {self.queue}: {e}")
                await self._close_ctx(suspend=True)
                if refused >= TID_404_MAX_ACCOUNTS:
                    raise EndpointRejectedError(
                        f"{self.queue}: ProductDisabled (closed gate) from {refused} accounts "
                        "in a row -- X is refusing this endpoint to the pool"
                    ) from None
                continue
            except AbortReqError as e:
                # Raise, never return: a bare return here reached the caller as an empty
                # result, indistinguishable from "X found nothing" (see RequestAbortedError).
                raise RequestAbortedError(f"{self.queue}: {e}") from None
            except HandledError:
                # retry with new account
                continue
            except NetworkError:
                # http transport failed, just retry with same account
                continue
            except ConnectError as e:
                # if proxy misconfigured or host unreachable
                connection_retry += 1
                if connection_retry >= 3:
                    raise e
            except Exception as e:
                unknown_retry += 1
                if unknown_retry >= 3:
                    msg = [
                        "Unknown error. Account timeouted for 15 minutes.",
                        "Create issue please: https://github.com/vladkens/twscrape/issues",
                        f"If it mistake, you can unlock accounts with `twscrape reset_locks`. Err: {type(e)}: {e}",
                    ]

                    logger.warning(" ".join(msg))
                    await self._close_ctx(utc.ts() + 60 * 15)  # 15 minutes
