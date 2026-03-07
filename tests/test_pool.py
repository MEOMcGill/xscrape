from twscrape.accounts_pool import AccountsPool
from twscrape.utils import utc


async def test_add_accounts(pool_mock: AccountsPool):
    # should add account
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"
    assert acc.password == "pass1"
    assert acc.email == "email1"
    assert acc.email_password == "email_pass1"

    # should not add account with same username
    await pool_mock.add_account("user1", "pass2", "email2", "email_pass2")
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"
    assert acc.password == "pass1"
    assert acc.email == "email1"
    assert acc.email_password == "email_pass1"

    # should not add account with different username case
    await pool_mock.add_account("USER1", "pass2", "email2", "email_pass2")
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"
    assert acc.password == "pass1"
    assert acc.email == "email1"
    assert acc.email_password == "email_pass1"

    # should add account with different username
    await pool_mock.add_account("user2", "pass2", "email2", "email_pass2")
    acc = await pool_mock.get("user2")
    assert acc.username == "user2"
    assert acc.password == "pass2"
    assert acc.email == "email2"
    assert acc.email_password == "email_pass2"


async def test_get_all(pool_mock: AccountsPool):
    # should return empty list
    accs = await pool_mock.get_all()
    assert len(accs) == 0

    # should return all accounts
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.add_account("user2", "pass2", "email2", "email_pass2")
    accs = await pool_mock.get_all()
    assert len(accs) == 2
    assert accs[0].username == "user1"
    assert accs[1].username == "user2"


async def test_save(pool_mock: AccountsPool):
    # should save account
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    acc = await pool_mock.get("user1")
    acc.password = "pass2"
    await pool_mock.save(acc)
    acc = await pool_mock.get("user1")
    assert acc.password == "pass2"

    # should not save account
    acc = await pool_mock.get("user1")
    acc.username = "user2"
    await pool_mock.save(acc)
    acc = await pool_mock.get("user1")
    assert acc.username == "user1"


async def test_get_for_queue(pool_mock: AccountsPool):
    Q = "test_queue"

    # should return account
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.set_active("user1", True)
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    assert acc.username == "user1"
    assert acc.active is True
    assert acc.locks is not None
    assert Q in acc.locks
    assert acc.locks[Q] is not None

    # should return None
    acc = await pool_mock.get_for_queue(Q)
    assert acc is None


async def test_account_unlock(pool_mock: AccountsPool):
    Q = "test_queue"

    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    await pool_mock.set_active("user1", True)
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    assert acc.locks[Q] is not None

    # should unlock account and make available for queue
    await pool_mock.unlock(acc.username, Q)
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    assert acc.locks[Q] is not None

    # should update lock time
    end_time = utc.ts() + 60  # + 1 minute
    await pool_mock.lock_until(acc.username, Q, end_time)

    acc = await pool_mock.get(acc.username)
    assert int(acc.locks[Q].timestamp()) == end_time


async def test_get_stats(pool_mock: AccountsPool):
    Q = "SearchTimeline"

    # should return empty stats
    stats = await pool_mock.stats()
    for k, v in stats.items():
        assert v == 0, f"{k} should be 0"

    # should increate total
    await pool_mock.add_account("user1", "pass1", "email1", "email_pass1")
    stats = await pool_mock.stats()
    assert stats["total"] == 1
    assert stats["active"] == 0

    # should increate active
    await pool_mock.set_active("user1", True)
    stats = await pool_mock.stats()
    assert stats["total"] == 1
    assert stats["active"] == 1

    # should update queue stats
    acc = await pool_mock.get_for_queue(Q)
    assert acc is not None
    stats = await pool_mock.stats()
    assert stats["total"] == 1
    assert stats["active"] == 1
    assert stats[f"locked_{Q}"] == 1


def test_calculate_lock_delay_distribution(pool_mock: AccountsPool):
    """Test that lock delays follow expected distribution."""
    Q = "SearchTimeline"
    expected_mean = pool_mock.endpoint_to_spread[Q]

    # Collect many samples to verify distribution
    delays = [pool_mock._calculate_lock_delay(Q) for _ in range(100)]
    mean_delay = sum(delays) / len(delays)

    # Allow 25% variance due to random distribution
    assert expected_mean * 0.75 <= mean_delay <= expected_mean * 1.25, (
        f"Mean delay {mean_delay} is not within 25% of expected {expected_mean}"
    )

    # Verify bounds are enforced (50% to 200% of mean)
    for delay in delays:
        assert delay >= expected_mean * 0.5, f"Delay {delay} is below minimum"
        assert delay <= expected_mean * 2, f"Delay {delay} is above maximum"


def test_unknown_endpoint_default_spread(pool_mock: AccountsPool):
    """Test that unknown endpoints use default spread."""
    delays = [pool_mock._calculate_lock_delay("UnknownEndpoint") for _ in range(50)]

    # Should be around DEFAULT_SPREAD (120) with bounds 60-240
    for delay in delays:
        assert 60 <= delay <= 240, f"Delay {delay} is outside expected range for default"


async def test_custom_endpoint_spreads(tmp_path):
    """Test custom endpoint spread configuration."""
    custom_spreads = {"SearchTimeline": 30, "CustomEndpoint": 45}
    pool = AccountsPool(tmp_path / "test.db", endpoint_spreads=custom_spreads)

    # Custom spread should override default
    assert pool.endpoint_to_spread["SearchTimeline"] == 30
    assert pool.endpoint_to_spread["CustomEndpoint"] == 45

    # Other endpoints should retain defaults
    assert pool.endpoint_to_spread["Followers"] == 120
    assert pool.endpoint_to_spread["UserTweets"] == 90
