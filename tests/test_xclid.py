"""Tests for locating X's animation-indices script (twscrape.xclid).

These call `_find_indices_url` directly rather than going through XClIdGenStore, because
conftest's autouse `mock_xclidgenstore` fixture replaces the whole generator for every
test — which is exactly why an X-side bundle change went unnoticed until it broke
collection in the wild. The depth-2 case below is that regression.

Shapes covered:
  depth 0 — legacy webpack build: the indices file is one of the page-linked URLs
  depth 1 — a page-linked chunk references it (the shape before the x-web/Vite rework)
  depth 2 — the page links one entry chunk which only re-exports; the reference lives a
            further hop in, e.g. entry -> sentry-filter-*.js -> ./sign.o-*.js
  absent  — nothing references it anywhere reachable: must raise, not hang
"""

import pytest
from pytest_httpx import HTTPXMock

from twscrape.xclid import (
    _MAX_INDICES_DEPTH,
    INDICES_FILE_RE,
    _find_indices_url,
    _make_client,
)

BASE = "https://abs.twimg.com/x-web/x-web"
ENTRY = f"{BASE}/entry-client-logged-out-BBbZ9GHm.js"
MIDDLE = f"{BASE}/sentry-filter-COJYaPUA.js"
INDICES = f"{BASE}/sign.o-DS0uSAly.js"


def test_indices_file_re_matches_page_linked_urls():
    """Legacy build (depth 0): the indices file is itself one of the page-linked URLs, so
    parse_anim_idx short-circuits on INDICES_FILE_RE before _find_indices_url is reached.
    Assert the regex still recognises both the legacy and current filenames, and doesn't
    fire on lookalikes like design.o-*.js."""
    assert INDICES_FILE_RE.search(f"{BASE}/ondemand.s.abc123a.js")
    assert INDICES_FILE_RE.search(f"{BASE}/sign.o-DS0uSAly.js")
    assert not INDICES_FILE_RE.search(f"{BASE}/design.o-DS0uSAly.js")


@pytest.mark.asyncio
async def test_indices_url_referenced_one_level_deep(httpx_mock: HTTPXMock):
    """A page-linked chunk references the indices file."""
    httpx_mock.add_response(url=ENTRY, text=f'import "./{INDICES.rsplit("/", 1)[-1]}";')
    async with _make_client() as clt:
        got = await _find_indices_url([ENTRY], clt)
    assert got == INDICES


@pytest.mark.asyncio
async def test_indices_url_referenced_two_levels_deep(httpx_mock: HTTPXMock):
    """Regression: current x-web build. The single page-linked entry chunk does NOT
    reference the indices file; a chunk it imports does. Before the depth walk this
    raised "Couldn't get XClientTxId indices script" and every scrape returned 0 rows."""
    httpx_mock.add_response(url=ENTRY, text='import "./sentry-filter-COJYaPUA.js";')
    httpx_mock.add_response(url=MIDDLE, text='const m = await import("./sign.o-DS0uSAly.js");')
    async with _make_client() as clt:
        got = await _find_indices_url([ENTRY], clt)
    assert got == INDICES


@pytest.mark.asyncio
async def test_raises_when_indices_file_is_deeper_than_the_cap(httpx_mock: HTTPXMock):
    """Only reachable beyond _MAX_INDICES_DEPTH: raise rather than keep crawling.
    b-2.js is deliberately not registered — reaching it would mean the cap leaked."""
    httpx_mock.add_response(url=ENTRY, text='import "./a-1.js";')
    httpx_mock.add_response(url=f"{BASE}/a-1.js", text='import "./b-2.js";')
    async with _make_client() as clt:
        with pytest.raises(Exception, match="Couldn't get XClientTxId indices script"):
            await _find_indices_url([ENTRY], clt)


@pytest.mark.asyncio
async def test_unfetchable_chunk_does_not_abort_the_scan(httpx_mock: HTTPXMock):
    """A chunk that 404s is skipped; a sibling still resolves."""
    httpx_mock.add_response(url=f"{BASE}/broken.js", status_code=404)
    httpx_mock.add_response(url=ENTRY, text='import "./sign.o-DS0uSAly.js";')
    async with _make_client() as clt:
        got = await _find_indices_url([f"{BASE}/broken.js", ENTRY], clt)
    assert got == INDICES


def test_depth_cap_is_bounded():
    """Guard the cap itself: unbounded recursion here means crawling abs.twimg.com."""
    assert 2 <= _MAX_INDICES_DEPTH <= 4
