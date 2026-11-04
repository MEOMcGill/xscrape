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
    XClIdAccountError,
    XClIdParseError,
    _find_indices_url,
    _make_client,
    get_scripts_list,
    parse_anim_idx,
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


# --- 2026-11-04: X stopped serving the indices script to anonymous sessions -----------------
# Live: an anonymous fetch got the logged-out x-web entry, from which sign.o-*.js is no longer
# reachable at any depth; a logged-in fetch got the legacy responsive-web build, whose
# embedded chunk map names ondemand.s with a 16-hex hash. Both halves of the port are below.

LEGACY = "https://abs.twimg.com/responsive-web/client-web"
LEGACY_PAGE = (
    f'<script src="{LEGACY}/vendor.b53dfa6ec5570e89a.js"></script>'
    f'<script src="{LEGACY}/main.3a36e48897e341d1a.js"></script>'
    '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    # webpack's name map and hash map, as embedded in the page
    '{20113:"ondemand.s",4127:"bundle.Grok"}[e]||e)+"."+{20113:"0123456789abcdef",'
    '4127:"fedcba9876543210"}[e]+"a.js"'
)


def test_legacy_build_with_16_hex_hashes_yields_the_indices_chunk():
    """The fork only knew 7-hex hashes, so on the logged-in (legacy) page it reconstructed
    no chunk URLs at all and never found ondemand.s."""
    urls = get_scripts_list(LEGACY_PAGE)
    indices = f"{LEGACY}/ondemand.s.0123456789abcdefa.js"
    assert indices in urls
    assert f"{LEGACY}/main.3a36e48897e341d1a.js" in urls
    assert [u for u in urls if INDICES_FILE_RE.search(u)] == [indices]


def test_challenge_reference_on_a_real_app_page_is_not_an_error():
    """The logged-in page references the challenge script too; only a page with NO app
    scripts is the interstitial."""
    assert get_scripts_list(LEGACY_PAGE)


def test_cloudflare_interstitial_is_a_parse_error():
    page = '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    with pytest.raises(XClIdParseError, match="Cloudflare challenge"):
        get_scripts_list(page)


def test_page_with_no_scripts_is_a_parse_error():
    with pytest.raises(XClIdParseError):
        get_scripts_list("<html></html>")


@pytest.mark.asyncio
async def test_logged_out_app_is_an_account_error():
    """A logged-out page means the cookies did not authenticate; crawling it is futile."""
    page = f'<script type="module" src="{ENTRY}"></script>'
    async with _make_client() as clt:
        with pytest.raises(XClIdAccountError):
            await parse_anim_idx(page, clt)


@pytest.mark.asyncio
async def test_cookies_go_to_x_com_only(httpx_mock: HTTPXMock):
    """Account cookies authenticate the page fetch; they must not leak to the asset CDN."""
    httpx_mock.add_response(url="https://x.com/tesla", text="ok")
    httpx_mock.add_response(url=INDICES, text="ok")
    async with _make_client(cookies={"auth_token": "a", "ct0": "c"}) as clt:
        await clt.get("https://x.com/tesla")
        await clt.get(INDICES)
    page_req, asset_req = httpx_mock.get_requests()
    assert "auth_token=a" in page_req.headers.get("cookie", "")
    assert "cookie" not in asset_req.headers
