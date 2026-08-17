# Thin alias package.
#
# This fork's distribution is named `xscrape`, but the actual source lives in
# the `twscrape/` package (kept in sync with upstream twscrape via merges).
# This module re-exports twscrape's public API so that `import xscrape` and
# `from xscrape import API` work without duplicating or renaming the source.
#
# ruff: noqa: F401, F403
from twscrape import *
from twscrape import (
    API,
    Account,
    AccountsPool,
    CloudflareBlockedError,
    ConnectError,
    HttpError,
    HttpStatusError,
    NetworkError,
    NoAccountError,
    RequestStats,
    Response,
    gather,
    set_log_level,
)
