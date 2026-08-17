import asyncio
import random
import time

from .logger import logger
from .utils import get_env_float


class CloudflareBlockedError(Exception):
    """Cloudflare kept 429-blocking this IP after the backoff schedule was exhausted.

    Raised out of the request path (and therefore out of `gather()` / the API
    generators) instead of silently returning partial results, so callers can
    tell "blocked" apart from "no more data"."""


class RequestPacer:
    # X fronts its GraphQL endpoints with a Cloudflare per-IP rate rule (observed
    # 2026-08: ~900 requests / 10 min from one IP trips a 429 wall that blocks every
    # account on that IP at once). Account rotation can't help — the budget is the
    # IP's — so request *starts* are spaced process-wide here, at a jittered mean
    # interval, regardless of how many accounts or concurrent queries are active.
    # Metronomic spacing is itself a bot signature, hence the gaussian jitter
    # (same shape as AccountsPool._calculate_lock_delay).
    #
    # State is class-level like XClIdGenStore: one pacer per process, shared by
    # every QueueClient. XSCRAPE_REQ_INTERVAL (seconds, mean gap between requests)
    # tunes it; 0 disables pacing entirely.
    DEFAULT_INTERVAL = 2.0

    _lock = asyncio.Lock()
    _next_at: float = 0.0  # time.monotonic() before which no request may start

    @staticmethod
    def _gap(mean: float) -> float:
        gap = random.gauss(mean, mean * 0.15)
        return max(mean * 0.5, min(gap, mean * 2))

    @classmethod
    async def wait(cls) -> None:
        interval = get_env_float("XSCRAPE_REQ_INTERVAL", cls.DEFAULT_INTERVAL)
        if interval <= 0:
            return

        # Reserve a start slot under the lock, then sleep outside it so waiters
        # queue up on distinct slots instead of serialising on the lock itself.
        async with cls._lock:
            now = time.monotonic()
            delay = cls._next_at - now
            cls._next_at = max(cls._next_at, now) + cls._gap(interval)
        if delay > 0:
            await asyncio.sleep(delay)

    @classmethod
    def reset(cls) -> None:
        cls._next_at = 0.0


class CloudflareBackoff:
    # Cloudflare's 429 wall is per-IP: when one request is blocked, every in-flight
    # request on this IP is about to be blocked too, so the pause must be shared.
    # One block sets a process-wide deadline all requests wait behind; repeated
    # blocks *after* a window expires (meaning the previous pause was too short)
    # escalate through SCHEDULE. Concurrent failures inside an active window do not
    # escalate — they are the same wall, observed many times (same stampede guard
    # idea as XClIdGenStore.MIN_AGE). Any successful response resets the streak.
    SCHEDULE = (60, 120, 240, 480, 900)  # seconds, jittered

    _blocked_until: float = 0.0  # time.monotonic() deadline shared by all requests
    _streak: int = 0

    @classmethod
    def max_retries(cls) -> int:
        return int(get_env_float("XSCRAPE_CF_MAX_RETRIES", 4))

    @classmethod
    async def wait_if_blocked(cls) -> None:
        # loop: the deadline may be extended by new blocks while we sleep
        while True:
            delay = cls._blocked_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    @classmethod
    def record_block(cls) -> None:
        now = time.monotonic()
        if now < cls._blocked_until:
            return
        step = cls.SCHEDULE[min(cls._streak, len(cls.SCHEDULE) - 1)]
        delay = RequestPacer._gap(step)
        cls._streak += 1
        cls._blocked_until = now + delay
        logger.warning(f"Cloudflare block #{cls._streak}: pausing ALL requests for {delay:.0f}s")

    @classmethod
    def record_success(cls) -> None:
        cls._streak = 0

    @classmethod
    def reset(cls) -> None:
        cls._blocked_until = 0.0
        cls._streak = 0


class RequestStats:
    """Process-wide request counters, so callers can report run-level metrics."""

    requests_sent: int = 0
    cf_blocks: int = 0

    @classmethod
    def snapshot(cls) -> dict:
        return {"requests_sent": cls.requests_sent, "cf_blocks": cls.cf_blocks}

    @classmethod
    def reset(cls) -> None:
        cls.requests_sent = 0
        cls.cf_blocks = 0
