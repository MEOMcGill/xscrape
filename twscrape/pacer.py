import asyncio
import random
import time

from .utils import get_env_float


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
