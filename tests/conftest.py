"""Shared test isolation.

The Taipei weekend BUY switch is process-wide state applied at startup from
ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS.  A test that builds strategy settings from the
local .env must not leak the operator's value into unrelated tests, so every
test starts from the code default (weekend BUYs blocked).
"""
import pytest

import bot.entry_session_policy as entry_session_policy


@pytest.fixture(autouse=True)
def _default_entry_session_policy():
    entry_session_policy.configure_entry_session_policy(allow_taipei_weekend_buys=False)
    yield
    entry_session_policy.configure_entry_session_policy(allow_taipei_weekend_buys=False)
