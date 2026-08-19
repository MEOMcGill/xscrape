# xscrape

X (Twitter) GraphQL API client with SNScrape-style data models. Fork of [twscrape](https://github.com/vladkens/twscrape) with additions for per-request account rotation, forward-compatible model parsing, endpoint-specific rate-limit spreading, and a few extra API methods (`user_about`, trends, `relogin_all`, etc.).

The Python package is still imported as `import twscrape` to stay drop-in compatible with the upstream; only the repo and this README are re-branded.

## Install

From source (this repo):
```bash
pip install -e /path/to/xscrape
```

The package name on disk is `twscrape`, so existing downstream code that does `from twscrape import API` keeps working after switching to the xscrape fork.

## Features

Inherited from twscrape:
- Search & GraphQL X API support
- Async/await surface — many scrapers in parallel
- Login flow with IMAP email verification
- Saving/restoring account sessions to SQLite
- Raw API responses & SNScrape-compatible models
- Automatic account switching to smooth rate limits

xscrape additions:
- **Endpoint-specific rate-limit distribution.** Per-endpoint lock delays sampled from a Gaussian (15% variance) instead of a fixed 15-minute lockout. Config lives in `AccountsPool.endpoint_to_spread` (e.g. SearchTimeline → 60s mean, Followers → 120s). See the [Rate-limit spread](#rate-limit-spread-configuration) section below.
- **Per-request account rotation within a single query.** `API(..., iterate_accounts=True)` swaps cookies/headers on the existing HTTP client after every successful paginated request, so each page of a long search/listing goes out as a different logged-in user. The TCP/TLS connection pool stays warm across rotations (no extra handshake).
- **Forward-compatible parsing.** Any top-level API field the parser doesn't explicitly consume is captured on `model.extras` instead of being silently dropped. The first occurrence of each `(model, key)` pair logs one INFO line to surface drift. Set `XSCRAPE_KEEP_RAW=1` to also stash the full raw response dict on `model._raw` when debugging parser drift.
- **`product` parameter on search.** `api.search(q, product="Top")` — Top / Latest / People / Photos / Videos. Default is `"Latest"`.
- **`user_about()` API + `AccountAbout` model.** Fetches location, affiliates, verification info, and username-change history from the X about-profile endpoint.
- **Trends support.** `api.trends(id)` with convenient category aliases (`"trending"`, `"news"`, `"sport"`, `"entertainment"`) plus `search_trend()`. `Trend` / `GroupedTrend` / `TrendMetadata` / `TrendUrl` models.
- **`relogin_all()` + CLI `relogin` with no usernames** to re-login every account in one command.
- **Required-field validation on `User`.** `get_required()` raises `KeyError` on missing critical fields instead of producing a half-parsed object.
- **Refreshed GraphQL operation IDs + GQL feature flags** (captured 2026-04-15 against live X frontend).
- **`SuspendedUser` model.** `__typename == "UserUnavailable"` entries are parsed into a dedicated dataclass rather than writing a crash dump.
- **Stuck-cursor detection** in paginated queries. If X returns the same bottom cursor twice we treat it as end-of-pagination instead of looping forever.
- **Pluggable HTTP backend.** Requests go through httpx by default, or [curl_cffi](https://github.com/lexiforest/curl_cffi) when installed (the `[curl]` extra, e.g. `pip install -e ".[curl]"`), which impersonates a real browser's TLS/HTTP2 fingerprint. Force one with `TWS_HTTP_BACKEND=httpx|curl`.
- **Per-account TLS fingerprint.** With the curl backend, each account can carry its own curl_cffi impersonate target (e.g. `chrome124`, `safari184`) via a private `x-tws-impersonate` header stored with the account, so a pool doesn't present one identical JA3/HTTP2 fingerprint across every "browser". The hint is validated and stripped before anything goes on the wire.
- **`X-Client-Transaction-Id` generation.** Computes the transaction ID X requires on GraphQL calls, with one shared generator per process so N accounts don't each re-fetch X's homepage and animation sprites.
- **Global request pacer.** GraphQL request *starts* are spaced process-wide at a jittered mean interval (`XSCRAPE_REQ_INTERVAL`, default 2 s, `0` disables) — X's Cloudflare rate rule is per-IP, so account rotation alone can't avoid it.
- **Cloudflare block backoff.** On a Cloudflare 429 wall, the whole pool backs off with increasing delays; if the block persists past `XSCRAPE_CF_MAX_RETRIES` attempts, a `CloudflareBlockedError` is raised out of the generators so "blocked" is distinguishable from "no more data".

## Usage

```python
import asyncio
from twscrape import API, gather
from twscrape.logger import set_log_level

async def main():
    api = API()  # or API("path-to.db") — default is `accounts.db`

    # --- ADD ACCOUNTS (CLI flow is covered below) ---

    # Cookies-based (more stable)
    cookies = "abc=12; ct0=xyz"  # or '{"abc": "12", "ct0": "xyz"}'
    await api.pool.add_account("u1", "p1", "u1@mail.com", "mp1", cookies=cookies)

    # Password-based — IMAP email login used to receive the verification code
    await api.pool.add_account("u2", "p2", "u2@mail.com", "mp2")
    await api.pool.login_all()

    # --- SEARCHES ---

    # default product is Latest; switch tabs without a kv override
    await gather(api.search("elon musk", limit=20))                  # list[Tweet]
    await gather(api.search("elon musk", limit=20, product="Top"))
    await gather(api.search("elon musk", limit=20, product="People"))

    # --- TWEET / USER ---

    tweet_id = 20
    await api.tweet_details(tweet_id)                 # Tweet
    await gather(api.tweet_replies(tweet_id, limit=20))
    await gather(api.retweeters(tweet_id, limit=20))

    await api.user_by_login("xdevelopers")            # -> User
    await api.user_by_id(2244994945)                  # -> User
    await api.user_about("xdevelopers")               # -> AccountAbout (xscrape)

    user_id = 2244994945
    await gather(api.following(user_id, limit=20))
    await gather(api.followers(user_id, limit=20))
    await gather(api.verified_followers(user_id, limit=20))
    await gather(api.subscriptions(user_id, limit=20))
    await gather(api.user_tweets(user_id, limit=20))
    await gather(api.user_tweets_and_replies(user_id, limit=20))
    await gather(api.user_media(user_id, limit=20))

    # --- LISTS, TRENDS ---

    await gather(api.list_timeline(list_id=123456789))

    await gather(api.trends("news"))                  # category alias
    await gather(api.trends("VGltZWxpbmU6..."))       # raw timeline ID also works
    await gather(api.search_trend("some trend"))      # search with trend_click source

    # --- PER-REQUEST ACCOUNT ROTATION ---
    # Swaps accounts after every paginated request. Use for long single-query runs
    # where you want to spread rate-limit pressure across many accounts instead of
    # exhausting one.
    api = API("accounts.db", iterate_accounts=True)
    await gather(api.search("python programming", limit=500))

    # --- MODEL EXTRAS (unknown API fields) ---
    user = await api.user_by_login("xdevelopers")
    user.extras        # {} if nothing drifted, else dict of unmodeled top-level keys
    user.dict()        # includes an "extras" key iff non-empty
    # XSCRAPE_KEEP_RAW=1 python my_script.py -> also fills user._raw

    # --- MISC ---
    async for rep in api.search_raw("elon musk"):     # raw httpx responses
        print(rep.status_code, rep.json())

    set_log_level("DEBUG")

if __name__ == "__main__":
    asyncio.run(main())
```

### Stopping iteration with `break`

To release the account lock deterministically when breaking early, wrap the generator in `contextlib.aclosing` (see upstream [issue #27](https://github.com/vladkens/twscrape/issues/27#issuecomment-1623395424)):

```python
from contextlib import aclosing

async with aclosing(api.search("elon musk")) as gen:
    async for tweet in gen:
        if tweet.id < 200:
            break
```

## CLI

The console script is `twscrape` (unchanged from upstream).

```sh
twscrape                    # list commands
twscrape search --help      # help on a specific command
```

### Add accounts

```sh
twscrape add_accounts <file_path> <line_format>
```

`<line_format>` is a colon-delimited schema describing each line. Tokens:
- `username`, `password`, `email` — required
- `email_password` — used for IMAP email verification
- `cookies` — string / JSON / base64
- `_` — skip a column

Example (skip a user-agent column):
```sh
twscrape add_accounts ./order-12345.txt username:password:email:email_password:_:cookies
```

### Login

```sh
twscrape login_accounts              # log in every account missing cookies
twscrape login_accounts --manual     # enter email 2FA codes by hand
twscrape relogin user1 user2         # re-login specific accounts
twscrape relogin                     # xscrape: re-login EVERY account (no usernames arg)
twscrape relogin_failed              # retry just the accounts with error_msg set
```

### Inspect & maintain

```sh
twscrape accounts               # list accounts with status
twscrape stats                  # endpoint usage stats
twscrape reset_locks            # clear all per-queue locks
twscrape delete_inactive        # remove accounts marked inactive
twscrape del_accounts user1 user2
```

### Queries

```sh
twscrape search "QUERY" --limit=20
twscrape tweet_details TWEET_ID
twscrape tweet_replies TWEET_ID --limit=20
twscrape retweeters TWEET_ID --limit=20
twscrape user_by_id USER_ID
twscrape user_by_login USERNAME
twscrape user_about USERNAME            # xscrape
twscrape following USER_ID --limit=20
twscrape followers USER_ID --limit=20
twscrape verified_followers USER_ID --limit=20
twscrape subscriptions USER_ID --limit=20
twscrape user_tweets USER_ID --limit=20
twscrape user_tweets_and_replies USER_ID --limit=20
twscrape user_media USER_ID --limit=20
twscrape list_timeline LIST_ID --limit=20
twscrape trends sport                   # or: trending / news / entertainment / raw ID
```

Output is one JSON document per line on stdout — redirect to capture:
```sh
twscrape search "elon mask lang:es" --limit=20 > data.txt
```

`--raw` prints the original X API response for each request instead of parsed models:
```sh
twscrape search "elon mask lang:es" --limit=20 --raw
```

### Different accounts database

```sh
twscrape --db test-accounts.db <command>
```

### About `--limit`

X paginates with a fixed page size per endpoint that the caller can't change. `--limit` / `limit=` is a *floor* — the scraper returns no fewer than that many items if they exist, but often a handful more (whatever the last page yields).

## Proxy

Four ways to configure proxies, highest to lowest priority:

1. `api.proxy = "socks5://user:pass@127.0.0.1:1080"` — can be changed mid-run
2. `API(proxy="...")` — constructor-level, covers all accounts
3. `TWS_PROXY` environment variable
4. Per-account: `await api.pool.add_account(..., proxy="http://...")`

_Note:_ an unreachable proxy raises from inside the API client.

## Environment variables

- `TWS_PROXY` — global proxy (e.g. `socks5://user:pass@127.0.0.1:1080`)
- `TWS_WAIT_EMAIL_CODE` — seconds to wait for the email verification code (default `30`)
- `TWS_RAISE_WHEN_NO_ACCOUNT` — raise `NoAccountError` instead of waiting when every account is locked (`false`/`0`/`true`/`1`, default `false`)
- `TWS_HTTP_BACKEND` — force the HTTP backend: `curl` or `httpx` (default: curl_cffi if installed, else httpx)
- `TWS_LOG_LEVEL` — log level (default `INFO`)
- `XSCRAPE_REQ_INTERVAL` — xscrape: mean seconds between GraphQL request starts, process-wide (default `2.0`, `0` disables pacing)
- `XSCRAPE_CF_MAX_RETRIES` — xscrape: pool-wide backoff attempts on a Cloudflare 429 block before raising `CloudflareBlockedError` (default `4`)
- `XSCRAPE_KEEP_RAW` — xscrape: when truthy, every parsed model keeps the full input dict on `model._raw`. Off by default because it's heavy for million-scale scraping. Useful when debugging parser drift surfaced by `model.extras`.

## Rate-limit spread configuration

Per-endpoint lock means (seconds), sampled from `N(mean, 0.15·mean)` and clamped to `[0.5·mean, 2·mean]`:

| Endpoint | Mean |
|---|---|
| `UserByRestId`, `UserByScreenName`, `AboutAccountQuery` | 30 s |
| `SearchTimeline`, `TweetDetail`, `ListLatestTweetsTimeline`, `Bookmarks` | 60 s |
| `UserTweets`, `UserTweetsAndReplies`, `UserMedia` | 90 s |
| `Followers`, `Following`, `Retweeters`, `BlueVerifiedFollowers`, `UserCreatorSubscriptions` | 120 s |
| (any other endpoint) | `AccountsPool.DEFAULT_SPREAD` = 120 s |

Override via the constructor:
```python
pool = AccountsPool("accounts.db", endpoint_spreads={"SearchTimeline": 30})
```

## Limitations

- X rotates GraphQL operation IDs on every frontend deploy. xscrape ships a recent snapshot but has no auto-updater yet. Symptoms of drift: empty Latest-tab search results, missing user fields, or `.extras` suddenly starting to log. The first occurrence of each unmodeled key logs one INFO line (`xscrape: unknown top-level key on User: …`) and is accessible via `model.extras`.
- `user_tweets` / `user_tweets_and_replies` cap around ~3200 tweets per user (X limit).
- Rate limits vary by account age and verification status — enabling `iterate_accounts` spreads pressure across the pool but can't raise per-account ceilings.

## See also

- [twscrape](https://github.com/vladkens/twscrape) — upstream project this is forked from
- [twitter-advanced-search](https://github.com/igorbrigadir/twitter-advanced-search) — search operator reference
- [snscrape](https://github.com/JustAnotherArchivist/snscrape) — the model shapes this project mirrors
