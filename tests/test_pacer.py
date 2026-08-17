import time

import pytest

from twscrape.pacer import RequestPacer
from twscrape.utils import get_env_float


@pytest.fixture(autouse=True)
def reset_pacer():
    RequestPacer.reset()
    yield
    RequestPacer.reset()


def test_gap_jitter_bounds():
    for mean in (0.5, 2.0, 10.0):
        gaps = [RequestPacer._gap(mean) for _ in range(1000)]
        assert all(mean * 0.5 <= g <= mean * 2 for g in gaps)
        # jitter should actually vary, not collapse to a constant
        assert len({round(g, 6) for g in gaps}) > 1


def test_get_env_float(monkeypatch):
    monkeypatch.delenv("SOME_FLOAT", raising=False)
    assert get_env_float("SOME_FLOAT", 2.0) == 2.0

    monkeypatch.setenv("SOME_FLOAT", "0.25")
    assert get_env_float("SOME_FLOAT", 2.0) == 0.25

    monkeypatch.setenv("SOME_FLOAT", "")
    assert get_env_float("SOME_FLOAT", 2.0) == 2.0

    monkeypatch.setenv("SOME_FLOAT", "abc")
    with pytest.raises(ValueError):
        get_env_float("SOME_FLOAT", 2.0)


async def test_wait_disabled_is_instant(monkeypatch):
    monkeypatch.setenv("XSCRAPE_REQ_INTERVAL", "0")
    start = time.monotonic()
    for _ in range(50):
        await RequestPacer.wait()
    assert time.monotonic() - start < 0.1


async def test_wait_spaces_requests(monkeypatch):
    monkeypatch.setenv("XSCRAPE_REQ_INTERVAL", "0.05")
    start = time.monotonic()
    for _ in range(4):
        await RequestPacer.wait()
    # first call is unpaced; the next three each reserve a jittered gap >= 0.025
    assert time.monotonic() - start >= 3 * 0.05 * 0.5
