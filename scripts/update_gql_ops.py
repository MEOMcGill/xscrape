#!/usr/bin/env python3
"""
Fetches current GraphQL operation IDs from Twitter's JS bundle
and updates them in twscrape/api.py.

Usage:
  uv run scripts/update_gql_ops.py                  # refresh, refusing a partial result
  uv run scripts/update_gql_ops.py --force          # ignore the script cache
  uv run scripts/update_gql_ops.py --write-partial  # write even if operations were missing

For a fully clean refresh, remove the temp cache first:
  rm -rf /tmp/twscrape-ops

IMPORTANT -- this only sees the LOGGED-OUT bundle, and that is not enough.
X serves a different script bundle to anonymous visitors, carrying its own variants of
each GraphQL operation. Those variants have different query ids AND expect different
variables, so an id harvested here is NOT a drop-in replacement for the authenticated
one: measured 2026-09-21, all four ids this script found (UserByScreenName, UserTweets,
TweetDetail, AboutAccountQuery) returned `422 GRAPHQL_VALIDATION_FAILED - must be
defined` on a real request, while the ids already in api.py all worked.

So: the 17 operations the authenticated app owns (SearchTimeline, UserTweetsAndReplies,
UserByRestId, Followers, ...) are invisible here and get reported as "not found", and the
handful that ARE visible are the wrong variant. Until this fetches the page with a logged-
in account's cookies, treat the output as a diagnostic, not as something to commit --
hence the --write-partial gate below. Always verify a changed id with a real request first.
"""

import asyncio
import os
import re
import sys
from urllib.parse import urljoin

from twscrape.http import HttpClient, make_client
from twscrape.xclid import get_scripts_list, get_tw_page_text, script_url

API_FILE = "twscrape/api.py"
CACHE_DIR = "/tmp/twscrape-ops"
MARKER = "# GQL_OPS_CODEGEN"

# A .js path imported from inside a chunk. X's current x-web/Vite build links a single
# entry chunk in the page and lazily imports the rest, so the operation ids sit one or
# two hops deeper and are unreachable from the page HTML alone. Same shape as the walk
# twscrape.xclid does to locate the XClientTxId indices file (CHUNK_REF_RE there).
JS_REF_RE = re.compile(r'(?:from|import)\s*\(?\s*[`"]((?:\.{1,2}/)[^`"]+?\.js)[`"]')

# The legacy webpack build linked main.<hash>.js separately from the chunk list; the
# x-web build has no such bundle. Matched rather than assumed: this used to be an
# unconditional `text.split("/client-web/main.")[1]`, which raised IndexError the moment
# X moved to x-web and took the whole refresh down with it.
LEGACY_MAIN_RE = re.compile(r"/client-web/main\.([^.\"']+)\.js")

# How many levels of chunk-to-chunk references to follow. Bounded so a future bundle
# reshuffle degrades to a clear, finite run rather than crawling abs.twimg.com
# indefinitely -- same reasoning as twscrape.xclid._MAX_INDICES_DEPTH. Six levels covered
# the 2026-09-21 build (1,927 chunks, ~64MB) with slack.
MAX_CHUNK_DEPTH = 6

# Pages to harvest chunk links from. Two, because a logged-out profile page and the home
# route load different entry chunks and neither alone covers every operation.
PAGE_URLS = ("https://x.com/xdevelopers", "https://x.com/home")


def _is_relevant_script(url: str) -> bool:
    return "/i18n/" not in url and "/icons/" not in url and "react-syntax-highlighter" not in url


def _script_entry(url: str) -> tuple[str, str]:
    return (url, f"{CACHE_DIR}/{url.split('/')[-1].split('?')[0]}")


def _source_priority(url: str) -> int:
    """Which build a script came from, when two disagree on the same operation id.

    X serves both builds during a migration and they can carry different ids for one
    operation. Highest priority wins deterministically, instead of last-one-read.
    """
    if "/responsive-web/client-web/" in url:
        return 2
    if "/x-web/" in url:
        return 1
    return 0


async def get_scripts() -> list[tuple[str, str]]:
    os.makedirs(CACHE_DIR, exist_ok=True)

    urls: list[str] = []
    async with make_client() as clt:
        for page_url in PAGE_URLS:
            # One page failing is not the run failing: /home is login-walled when logged
            # out and serves markup with no chunk links at all, on which get_scripts_list
            # raises. Only a page that actually yields scripts contributes.
            try:
                text = await get_tw_page_text(page_url, clt)
                found = get_scripts_list(text)
            except Exception as e:
                print(f"  {page_url}: no scripts ({type(e).__name__}: {e})")
                continue
            print(f"  {page_url}: {len(found)} script(s)")
            urls.extend(found)
            # Legacy build only: append the separately-linked main bundle if present.
            if m := LEGACY_MAIN_RE.search(text):
                urls.append(script_url("main", m.group(1)))

    if not urls:
        raise Exception(f"No scripts found on any of {PAGE_URLS}")

    urls = list(dict.fromkeys(urls))
    urls = [x for x in urls if _is_relevant_script(x)]
    return [_script_entry(x) for x in urls]


def _discover_scripts(scripts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The .js chunks the already-downloaded ones import, that we haven't seen yet."""
    urls = {url for url, _ in scripts}
    found: list[str] = []

    for base_url, path in scripts:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fp:
            text = fp.read()
        for ref in JS_REF_RE.findall(text):
            url = urljoin(base_url, ref)
            if url not in urls and _is_relevant_script(url):
                urls.add(url)
                found.append(url)

    return [_script_entry(x) for x in found]


async def fetch_scripts(scripts: list[tuple[str, str]], force: bool) -> list[tuple[str, str]]:
    """Download the linked chunks, then whatever those import, until nothing new appears.

    Returns every script fetched, not just the ones linked in the page -- the caller
    scans all of them for operation ids.
    """
    all_scripts = list(dict.fromkeys(scripts))
    seen = {url for url, _ in all_scripts}
    batch = all_scripts

    for depth in range(MAX_CHUNK_DEPTH):
        if not batch:
            break
        await _fetch_scripts_batch(batch, force)
        discovered = _discover_scripts(batch)
        batch = [x for x in discovered if x[0] not in seen]
        seen.update(url for url, _ in batch)
        all_scripts.extend(batch)
    else:
        if batch:
            print(f"Stopped at depth {MAX_CHUNK_DEPTH} with {len(batch)} chunk(s) unexplored.")

    return all_scripts


async def _fetch_scripts_batch(scripts: list[tuple[str, str]], force: bool) -> None:
    todo = scripts if force else [x for x in scripts if not os.path.exists(x[1])]
    cached = len(scripts) - len(todo)
    print(f"Scripts: {len(scripts)} total, {cached} cached, {len(todo)} to download.")

    if not todo:
        print("Nothing to download.")
        return

    print(f"Downloading {len(todo)} scripts.")
    sem = asyncio.Semaphore(10)

    async def fetch(clt: HttpClient, i: int, url: str, path: str) -> None:
        async with sem:
            print(f"  ({i:3d}/{len(todo):3d}) {url}")
            rep = await clt.get(url)
            # Discovery resolves relative imports optimistically, so some candidates
            # legitimately do not exist. One 404 must not abort the whole refresh.
            if rep.status_code == 404:
                print("             skipped (404)")
                return
            rep.raise_for_status()
            ct = rep.headers.get("content-type", "")
            if "javascript" not in ct:
                raise ValueError(f"Unexpected content-type '{ct}' for {url} — rate limited?")
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(rep.text)

    async with make_client() as clt:
        await asyncio.gather(*[fetch(clt, i, url, path) for i, (url, path) in enumerate(todo, 1)])


def parse_ops(content: str) -> tuple[str, list[tuple[str, str, str]]]:
    parts = content.split(MARKER)
    if len(parts) != 3:
        raise ValueError(f"Expected exactly two {MARKER!r} markers in {API_FILE}")

    res = []
    for line in parts[1].splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("OP_"):
            raise ValueError(f"Unexpected line in {MARKER} block: {line}")

        name, value = line.split("=", 1)
        name = name.strip()[3:]
        value = value.strip().strip('"')
        op_id, gql_name = value.split("/", 1)
        res.append((name, op_id, gql_name))

    return parts[1], res


def render_ops(ops: list[tuple[str, str]]) -> str:
    lines = [f'OP_{name} = "{op_id}/{name}"' for name, op_id in ops]
    return "\n" + "\n".join(lines) + "\n"


def rewrite(content: str, block: str) -> str:
    parts = content.split(MARKER)
    if len(parts) != 3:
        raise ValueError(f"Expected exactly two {MARKER!r} markers in {API_FILE}")
    return parts[0] + MARKER + block + MARKER + parts[2]


async def main() -> int:
    # --force re-downloads the script cache; --write-partial is a separate, deliberate
    # override for the "only saw the logged-out bundle" guard below. Kept distinct so
    # asking for a fresh download can never also wave through a bad refresh.
    force = "--force" in sys.argv
    write_partial = "--write-partial" in sys.argv

    with open(API_FILE, encoding="utf-8") as fp:
        content = fp.read()

    old_text, current_ops = parse_ops(content)
    renamed: list[tuple[str, str]] = []
    for var_suffix, hash_id, gql_name in current_ops:
        if var_suffix != gql_name:
            renamed.append((var_suffix, gql_name))

    print(f"Found {len(current_ops)} operations in {API_FILE}")
    print("")
    print("Downloading scripts...")
    scripts = await get_scripts()
    scripts = await fetch_scripts(scripts, force)
    print("")
    print("Analyzing scripts...")

    all_pairs: dict[str, tuple[str, int]] = {}
    conflicts: list[tuple[str, str, str]] = []

    def _add(op_name: str, op_id: str, url: str):
        priority = _source_priority(url)
        existing = all_pairs.get(op_name)
        if existing is not None and existing[0] != op_id:
            # Only worth reporting when priority cannot settle it; otherwise the
            # higher-priority build simply wins and there is nothing to decide.
            if priority == existing[1]:
                conflicts.append((op_name, existing[0], op_id))
            if priority < existing[1]:
                return
        all_pairs[op_name] = (op_id, priority)

    rgs = [
        r'queryId:[`"](.+?)[`"].+?operationName:[`"](.+?)[`"]',
        r'params:\{id:[`"]([^`"]+)[`"].+?name:[`"]([^`"]+)[`"].+?operationKind:[`"]',
    ]

    for url, path in scripts:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fp:
            txt = fp.read()
        for rg in rgs:
            for op_id, op_name in re.findall(rg, txt):
                _add(op_name, op_id, url)

    if conflicts:
        print("WARNING: same-priority sources disagree on these operations (last one wins):")
        for n, old_id, new_id in conflicts:
            print(f"  {n}: {old_id} vs {new_id}")

    print(f"Found {len(all_pairs)} operations in bundle")

    changed, missing = [], []
    next_ops: list[tuple[str, str]] = []

    for var_suffix, old_id, gql_name in current_ops:
        found = all_pairs.get(gql_name)
        if found is None:
            missing.append(f"OP_{var_suffix}")
            next_ops.append((gql_name, old_id))
        elif found[0] != old_id:
            changed.append((var_suffix, old_id, found[0]))
            next_ops.append((gql_name, found[0]))
        else:
            next_ops.append((gql_name, old_id))

    next_ops.sort()
    new_block = render_ops(next_ops)
    updated = rewrite(content, new_block)

    if changed:
        print("Changed:")
        for n, old_id, new_id in changed:
            print(f"  OP_{n}: {old_id} → {new_id}")
    else:
        print("No ID changes.")

    if missing:
        print("\nNot found in bundle (possibly removed):")
        for n in missing:
            print(f"  {n}")

    if renamed:
        print("\nNon-canonical constant names:")
        for var_suffix, gql_name in renamed:
            print(f"  rename OP_{var_suffix} to OP_{gql_name}")

    if new_block == old_text:
        return 0

    # A refresh that could not see most operations saw the wrong bundle (see module
    # docstring). Writing it would swap working ids for logged-out variants that 422.
    if missing and not write_partial:
        print(
            f"\nREFUSING to write: {len(missing)}/{len(current_ops)} operations were not in the "
            "bundle, which means this run only saw the logged-out build. The ids it did find are "
            "very likely the wrong variant -- verify each one with a real request, then re-run "
            "with --write-partial if they genuinely work."
        )
        return 1

    with open(API_FILE, "w", encoding="utf-8") as fp:
        fp.write(updated)
    print(f"\nSaved changes to {API_FILE}")

    return 0


sys.exit(asyncio.run(main()))
