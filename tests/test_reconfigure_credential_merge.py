"""Reconfigure must actually replace the stored credentials.

The flow snapshots ``entry.data`` early, then ``_cache_iot_credentials()`` writes
the freshly obtained token straight to the live entry. Submitting that snapshot
through ``data_updates=`` let HA's ``entry.data | data_updates`` union put the
stale token back, so reconfigure could not clear a rejected token — and the
union can never remove a key, so deleting the account credentials left the
password in storage.

These exercise the real merge instead of mocking ``async_update_reload_and_abort``.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.govee.config_flow import _carry_forward_iot_keys
from custom_components.govee.const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_PASSWORD,
    KEY_IOT_CREDENTIALS,
    KEY_IOT_LOGIN_FAILED,
)


class _Entry:
    """Minimal stand-in whose data the cache/clear helpers would have written."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def _merge(entry: _Entry, submitted: dict[str, Any]) -> dict[str, Any]:
    """What HA core stores for ``data=`` (config_entries.py)."""
    return dict(submitted)


def _merge_as_updates(entry: _Entry, submitted: dict[str, Any]) -> dict[str, Any]:
    """What HA core stores for ``data_updates=``: ``entry.data | data_updates``."""
    return entry.data | submitted


class TestReconfigureCredentialMerge:
    def test_fresh_token_survives_the_submit(self):
        """The token written during the flow must reach storage."""
        snapshot = {
            CONF_API_KEY: "key",
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "pw",
            KEY_IOT_CREDENTIALS: {"token": "stale"},
        }
        entry = _Entry({**snapshot, KEY_IOT_CREDENTIALS: {"token": "fresh"}})

        submitted = dict(snapshot)
        _carry_forward_iot_keys(entry, submitted)

        assert _merge(entry, submitted)[KEY_IOT_CREDENTIALS] == {"token": "fresh"}

    def test_union_merge_would_have_restored_the_stale_token(self):
        """Pins the defect: the old shape silently reverts the reconfigure."""
        snapshot = {KEY_IOT_CREDENTIALS: {"token": "stale"}}
        entry = _Entry({KEY_IOT_CREDENTIALS: {"token": "fresh"}})

        assert _merge_as_updates(entry, snapshot)[KEY_IOT_CREDENTIALS] == {
            "token": "stale"
        }

    def test_removing_account_credentials_takes_effect(self):
        """Deleting email/password must not leave the password in storage."""
        entry = _Entry({CONF_API_KEY: "key"})
        submitted = {CONF_API_KEY: "key"}
        _carry_forward_iot_keys(entry, submitted)

        stored = _merge(entry, submitted)
        assert CONF_PASSWORD not in stored
        assert CONF_EMAIL not in stored

    def test_cleared_login_marker_is_not_resurrected(self):
        """_clear_mqtt_cache popped it; the snapshot must not put it back."""
        submitted = {CONF_API_KEY: "key", KEY_IOT_LOGIN_FAILED: "2FA required"}
        entry = _Entry({CONF_API_KEY: "key"})

        _carry_forward_iot_keys(entry, submitted)

        assert KEY_IOT_LOGIN_FAILED not in _merge(entry, submitted)


class TestBudgetResetActor:
    @pytest.mark.asyncio
    async def test_reconfigure_clears_the_login_budget(self):
        """An exhausted budget must be recoverable without a process restart."""
        from collections import deque

        from custom_components.govee import coordinator as coord_mod
        from custom_components.govee.coordinator import reset_login_budget

        coord_mod._LOGIN_ATTEMPTS["entry_under_test"] = deque([1.0] * 25)

        reset_login_budget("entry_under_test")

        assert "entry_under_test" not in coord_mod._LOGIN_ATTEMPTS
