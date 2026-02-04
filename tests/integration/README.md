# Integration Tests

These tests are **NOT run automatically in CI/CD**. They hit the real Twitter API and require valid account credentials.

## Purpose

- Verify the scraper still works with the live Twitter API
- Catch API changes that break the scraper
- Run before releases to ensure everything works end-to-end

## Prerequisites

1. **Twitter accounts configured**:
   ```bash
   twscrape add_accounts accounts.txt username:password:email:email_password
   twscrape login_accounts
   ```

2. **Active internet connection**

3. **Valid account credentials** (accounts must not be rate-limited)

## Running Tests

### Run all smoke tests:
```bash
python tests/integration/smoke_test.py
```

### Run a specific test:
```bash
python tests/integration/smoke_test.py --test user_by_login
python tests/integration/smoke_test.py --test search
python tests/integration/smoke_test.py --test tweet_details
```

### Available tests:
- `user_by_login` - Fetch user by username
- `user_by_id` - Fetch user by ID
- `search` - Search tweets
- `tweet_details` - Fetch specific tweet
- `user_tweets` - Fetch user's tweets
- `followers` - Fetch user's followers
- `following` - Fetch user's following
- `parsing_integrity` - Verify required fields are not None

## When to Run

✅ **Before releasing a new version**
✅ **After updating mocked data** (`make update-mocks`)
✅ **When you suspect Twitter changed their API**
✅ **After making significant parsing changes**

## Expected Results

- All tests should pass ✅
- If parsing_integrity fails with KeyError, that's **good** - it means your `get_required()` function caught missing data
- If other tests fail, investigate whether:
  - Twitter changed their API structure
  - Your accounts are rate-limited
  - The test assumptions are outdated

## Updating Tests

When Twitter adds new fields or changes structure:
1. Update the assertions in smoke_test.py
2. Update the mocked data: `make update-mocks`
3. Update parsing logic in `twscrape/models.py`
4. Re-run smoke tests to verify
