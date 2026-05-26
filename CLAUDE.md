We're currently refactoring the module at /Users/mikad/MEOMcGill/twitter_scraper/app/scraper/twscrape here to create its own standalone library. Here's naming conventions I will use:

* custom twscrape: this is the module I am refactoring and is currently in /Users/mikad/MEOMcGill/twitter_scraper/app/scraper/twscrape.
* original twscrape: this is a library whose URL is https://github.com/vladkens/twscrape
* xscrape: the library I am developing. It's original code is a straight fork from the original twscrape but I have been incorporating some key changes I made from custom twscrape.

The goal of xscrape is to add the features of custom twscrape in the latest original twscrape. However, custom twscrape was not very well coded so the goal is the import these changes in a much cleaner way.

## Changes made in custom twscrape (vs original twscrape)

- **Account management**: added fields (`use_case`, `in_use`, `last_login`, `num_calls`, `automated`) and methods (`set_in_use`, `set_use_case`, `get_active`) to track and categorize accounts by scraping purpose (daily, historical, query).
- **Intelligent rate limiting**: replaced fixed 15-minute lockouts with per-endpoint delays using Gaussian distribution (`np.random.normal`) via an `endpoint_to_spread` mapping.
- **Account humanization**: integrated an HTI (HumanTwitterInteraction) module that humanizes accounts after a configurable number of calls, with semaphore-controlled concurrency.
- **Account rotation**: added `iterate_through_accounts` flag to control whether accounts rotate per request or persist across a session.
- **Stopping conditions**: added a `stopping_condition` parameter to pagination methods and a `Flag` class to halt iteration early based on custom predicates.
- **Response analysis**: queue client parses responses to extract tweet counts, date ranges, and top users for monitoring.
- **X migration handling**: async detection and automatic form submission for X.com migration redirects, with retry and exponential backoff.
- **Async client transaction**: async version of `ClientTransaction` using `AsyncClient` for non-blocking transaction ID generation.
- **Browser header generation**: uses `browserforge.headers.HeaderGenerator` for realistic request headers.

## Modifications already added to xscrape

- **Endpoint-specific rate limit distribution**: per-endpoint lock delays with normal distribution and 15% variance (`_calculate_lock_delay`, `_get_and_lock` in `accounts_pool.py`).
- **Required field validation**: `get_required()` utility that raises `KeyError` on missing required fields, applied to critical User model fields.
- **User about info**: `user_about()` API method and `AccountAbout` model for fetching profile details (location, affiliates, verification, username changes).
- **Trends support**: `parse_trends()`, `search_trend()` and Trend/GroupedTrend model classes for X explore page data.
- **Pool management**: `relogin_all()` method and CLI option to login all accounts with an empty usernames list.
- **GraphQL endpoint updates**: updated endpoint IDs to match X's latest rotated identifiers; added `is_blue_verified` and `verification_info` to User model.

## Features in progress (on feature branches)

- **`feature-rate-limit-distribution`**: refining rate limit distribution with tests (1 commit ahead of main).
- **`feature-stopping-conditions`**: `stopping_condition` parameter for `search()`, `user_tweets()`, `user_tweets_and_replies()` with a `Flag` utility class and tests.
- **`feat-browser-login`**: browser automation module (`browser.py`, ~709 lines) for login as an alternative to email verification.

## TODO: Features not yet ported from custom twscrape

- **Auto-update GraphQL operation IDs**: Twitter/X rotates GraphQL operation IDs (the hash prefix in endpoints like `R0u1RWRf748KzyGBXvOYRA/SearchTimeline`) with every frontend deploy. Stale IDs cause subtle breakage — infinite pagination on "Latest" search, missing user fields — rather than clean errors. The fix is to fetch the current IDs at scraper startup by: (1) fetching `https://x.com` to extract the `main.*.js` bundle URL, (2) downloading the bundle, (3) extracting all `queryId:"...",operationName:"..."` pairs with a regex. Cache with a 24h TTL and fall back to hardcoded values. This also applies to `GQL_FEATURES` which can drift over time.
- **Account humanization (HTI)**: integrated humanization module that performs human-like Twitter interactions after a configurable number of API calls, with semaphore-controlled concurrency.
- **Account rotation**: `iterate_through_accounts` flag to control whether accounts rotate per request or persist across a session.
- **Response analysis**: queue client parses responses to extract tweet counts, date ranges, and top users for real-time monitoring.
- **X migration handling**: async detection and automatic form submission for X.com migration redirects, with retry and exponential backoff.
- **Async client transaction**: async version of `ClientTransaction` using `AsyncClient` for non-blocking transaction ID generation.
- **Browser header generation**: uses `browserforge.headers.HeaderGenerator` for realistic request headers.
- **User object compatibility**: the X API moved `screen_name`, `name`, `created_at` from the user legacy object into a `core` sub-object, and `profile_image_url_https` into `avatar.image_url`, `verified` into `verification.verified`, `protected` into `privacy.protected`. The `to_old_obj` function in `utils.py` needs to merge these back for backward compatibility.