"""
Manual smoke tests - NOT run in CI/CD

These tests hit the real Twitter API to verify the scraper still works.
Run them manually before releases or when you suspect API changes.

Requirements:
- Twitter accounts configured in the database
- Active internet connection
- Valid account credentials

Usage:
    python tests/integration/smoke_test.py

Or run specific tests:
    python tests/integration/smoke_test.py --test user_lookup
    python tests/integration/smoke_test.py --test search
"""

import asyncio
import sys
from datetime import datetime

from twscrape import API


class SmokeTest:
    def __init__(self):
        self.api = API()
        self.passed = 0
        self.failed = 0
        self.errors = []

    def log(self, emoji: str, message: str):
        print(f"{emoji} {message}")

    async def assert_true(self, condition: bool, message: str):
        if condition:
            self.passed += 1
            self.log("✓", message)
        else:
            self.failed += 1
            self.errors.append(message)
            self.log("✗", f"FAILED: {message}")

    async def test_xclid_derivation(self):
        """Test that the x-client-transaction-id generator can still be derived.

        This is the canary for X changing its JS bundle layout. XClIdGen locates an
        "indices" script by walking abs.twimg.com chunk references; when X reshuffles the
        bundle, that walk stops finding it and EVERY request fails with "Couldn't get
        XClientTxId indices script". Unit tests cannot catch it — conftest's autouse
        fixture mocks the generator out entirely — so it belongs here.

        Needs no accounts: the generator is account-agnostic (calc() takes only
        method + path), so this runs even where no credentials are configured.
        """
        self.log("🔍", "Testing xclid derivation...")
        try:
            from twscrape.xclid import XClIdGen

            gen = await XClIdGen.create()
            await self.assert_true(gen is not None, "XClIdGen.create() succeeded")
            tid = gen.calc("GET", "/i/api/graphql/foo/SearchTimeline")
            ok = isinstance(tid, str) and len(tid) > 32
            await self.assert_true(ok, "calc() returns a plausible transaction id")
            self.log("  ", f"transaction id: {tid[:24]}... (len {len(tid)})")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"xclid_derivation: {e}")
            self.log("✗", f"FAILED: xclid_derivation - {e}")

    async def test_user_by_login(self):
        """Test fetching user by username"""
        self.log("🔍", "Testing user_by_login...")
        try:
            user = await self.api.user_by_login("MarkJCarney")
            await self.assert_true(user is not None, "User lookup returned data")
            await self.assert_true(user.username == "MarkJCarney", "Username matches")
            await self.assert_true(user.id > 0, "User ID is valid")
            await self.assert_true(len(user.displayname) > 0, "Display name exists")
            await self.assert_true(user.created is not None, "Created date exists")
            await self.assert_true(isinstance(user.created, datetime), "Created date is datetime")
            self.log("  ", f"Found: {user.displayname} (@{user.username})")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"user_by_login: {e}")
            self.log("✗", f"FAILED: user_by_login - {e}")

    async def test_user_by_id(self):
        """Test fetching user by ID"""
        self.log("🔍", "Testing user_by_id...")
        try:
            user = await self.api.user_by_id(192272849)
            await self.assert_true(user is not None, "User by ID returned data")
            await self.assert_true(user.id == 192272849, "User ID matches")
            await self.assert_true(len(user.username) > 0, "Username exists")
            self.log("  ", f"Found: {user.displayname} (@{user.username})")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"user_by_id: {e}")
            self.log("✗", f"FAILED: user_by_id - {e}")

    async def test_search(self):
        """Test tweet search"""
        self.log("🔍", "Testing search...")
        try:
            tweets = []
            async for tweet in self.api.search("python lang:en", limit=5):
                tweets.append(tweet)

            await self.assert_true(len(tweets) > 0, "Search returned tweets")
            await self.assert_true(len(tweets) <= 5, "Search respects limit")

            for tweet in tweets:
                await self.assert_true(tweet.id > 0, f"Tweet {tweet.id} has valid ID")
                await self.assert_true(tweet.user is not None, f"Tweet {tweet.id} has user")
                await self.assert_true(len(tweet.rawContent) > 0, f"Tweet {tweet.id} has content")

            self.log("  ", f"Found {len(tweets)} tweets")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"search: {e}")
            self.log("✗", f"FAILED: search - {e}")

    async def test_tweet_details(self):
        """Test fetching specific tweet"""
        self.log("🔍", "Testing tweet_details...")
        try:
            tweet = await self.api.tweet_details(1892793839649272278)
            await self.assert_true(tweet is not None, "Tweet details returned data")
            await self.assert_true(tweet.id == 1892793839649272278, "Tweet ID matches")
            await self.assert_true(tweet.user is not None, "Tweet has user")
            await self.assert_true(len(tweet.rawContent) > 0, "Tweet has content")
            self.log("  ", f"Tweet by @{tweet.user.username}: {tweet.rawContent[:50]}...")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"tweet_details: {e}")
            self.log("✗", f"FAILED: tweet_details - {e}")

    async def test_user_tweets(self):
        """Test fetching user's tweets"""
        self.log("🔍", "Testing user_tweets...")
        try:
            tweets = []
            async for tweet in self.api.user_tweets(192272849, limit=5):
                tweets.append(tweet)

            await self.assert_true(len(tweets) > 0, "User tweets returned data")
            await self.assert_true(len(tweets) <= 5, "User tweets respects limit")

            for tweet in tweets:
                await self.assert_true(
                    tweet.user.id == 192272849, f"Tweet {tweet.id} is from correct user"
                )

            self.log("  ", f"Found {len(tweets)} tweets")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"user_tweets: {e}")
            self.log("✗", f"FAILED: user_tweets - {e}")

    async def test_followers(self):
        """Test fetching user's followers"""
        self.log("🔍", "Testing followers...")
        try:
            followers = []
            async for user in self.api.followers(192272849, limit=5):
                followers.append(user)

            await self.assert_true(len(followers) > 0, "Followers returned data")
            await self.assert_true(len(followers) <= 5, "Followers respects limit")

            for user in followers:
                await self.assert_true(user.id > 0, f"Follower {user.username} has ID")
                await self.assert_true(len(user.username) > 0, f"Follower has username")

            self.log("  ", f"Found {len(followers)} followers")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"followers: {e}")
            self.log("✗", f"FAILED: followers - {e}")

    async def test_following(self):
        """Test fetching user's following"""
        self.log("🔍", "Testing following...")
        try:
            following = []
            async for user in self.api.following(192272849, limit=5):
                following.append(user)

            await self.assert_true(len(following) > 0, "Following returned data")
            await self.assert_true(len(following) <= 5, "Following respects limit")

            self.log("  ", f"Found {len(following)} following")
        except Exception as e:
            self.failed += 1
            self.errors.append(f"following: {e}")
            self.log("✗", f"FAILED: following - {e}")

    async def test_parsing_integrity(self):
        """Test that parsed data has no None values in required fields"""
        self.log("🔍", "Testing parsing integrity...")
        try:
            user = await self.api.user_by_login("MarkJCarney")

            # Test required User fields are not None
            await self.assert_true(user.username is not None, "username is not None")
            await self.assert_true(user.displayname is not None, "displayname is not None")
            await self.assert_true(user.rawDescription is not None, "rawDescription is not None")
            await self.assert_true(user.created is not None, "created is not None")
            await self.assert_true(user.followersCount is not None, "followersCount is not None")
            await self.assert_true(user.location is not None, "location is not None")
            await self.assert_true(user.profileImageUrl is not None, "profileImageUrl is not None")

        except KeyError as e:
            self.failed += 1
            self.errors.append(f"parsing_integrity: {e}")
            self.log(
                "✗",
                f"FAILED: Required field missing (this is good - means get_required works!): {e}",
            )
        except Exception as e:
            self.failed += 1
            self.errors.append(f"parsing_integrity: {e}")
            self.log("✗", f"FAILED: parsing_integrity - {e}")

    async def run_all(self):
        """Run all smoke tests"""
        self.log("🚀", "Starting smoke tests...\n")

        # First: if the transaction-id generator can't be derived, every request below
        # fails as a consequence. Checking it up front makes the real cause obvious.
        await self.test_xclid_derivation()
        print()

        await self.test_user_by_login()
        print()

        await self.test_user_by_id()
        print()

        await self.test_search()
        print()

        await self.test_tweet_details()
        print()

        await self.test_user_tweets()
        print()

        await self.test_followers()
        print()

        await self.test_following()
        print()

        await self.test_parsing_integrity()
        print()

        # Summary
        print("=" * 60)
        if self.failed == 0:
            self.log("✅", f"All tests passed! ({self.passed} assertions)")
            return 0
        else:
            self.log("❌", f"Some tests failed: {self.failed} failures, {self.passed} passed")
            print("\nErrors:")
            for error in self.errors:
                print(f"  - {error}")
            return 1

    async def run_single(self, test_name: str):
        """Run a single test by name"""
        test_method = getattr(self, f"test_{test_name}", None)
        if test_method is None:
            self.log("❌", f"Test '{test_name}' not found")
            return 1

        self.log("🚀", f"Running {test_name}...\n")
        await test_method()
        print()

        if self.failed == 0:
            self.log("✅", f"Test passed! ({self.passed} assertions)")
            return 0
        else:
            self.log("❌", f"Test failed")
            return 1


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run smoke tests for xscrape")
    parser.add_argument(
        "--test",
        type=str,
        help="Run a specific test (e.g., user_lookup, search, tweet_details)",
    )
    args = parser.parse_args()

    smoke_test = SmokeTest()

    if args.test:
        exit_code = await smoke_test.run_single(args.test)
    else:
        exit_code = await smoke_test.run_all()

    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
