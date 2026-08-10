"""Tests for the Cancel Heat Schedule button.

Requested on GitHub (romd87, 2026-08-08): Home Assistant offers no gesture for
clearing a datetime entity, so setting **Scheduled for** was a one-way door.
Editing the date to a past day was the only escape, and that fired the trigger on
the way out and switched the heater on.

Run with: python -m pytest tests/test_cancel_button.py -v
"""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import time

from custom_components.mspa import button as button_mod
from custom_components.mspa.button import MSpaCancelHeatSchedule

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _button(scheduled=None):
    coord = MagicMock()
    coord.scheduled_ready_at = scheduled
    coord.device_id = "dev"
    real = {}

    def _clear(reason, current_temp=None):
        real["reason"] = reason
        coord.scheduled_ready_at = None
    coord.clear_schedule.side_effect = _clear

    b = object.__new__(MSpaCancelHeatSchedule)
    b.coordinator = coord
    b._reason = real
    return b


def _run(coro):
    return asyncio.run(coro)


class TestAvailability:
    """Availability must NOT track whether a schedule exists.

    Regression source: 2026-08-10.  A button's state is its last-press timestamp and
    ButtonEntity restores it across restarts, so gating availability made the entity
    flip from `unavailable` to that restored timestamp the moment a schedule was
    applied — which the logbook reports as "Pressed".  The calendar automation set a
    schedule at 11:28:51 and the feed showed a press at 11:28:53, looking exactly as
    though the button had cancelled what the automation had just set.
    """

    def test_availability_ignores_the_schedule(self):
        for scheduled in (None, _NOW + timedelta(hours=3)):
            b = _button(scheduled=scheduled)
            assert "available" not in type(b).__dict__, (
                "availability must not be overridden — it makes the restored press "
                "timestamp surface as a state change")


class TestPress:

    def test_press_clears_the_schedule(self):
        b = _button(scheduled=_NOW + timedelta(hours=3))
        _run(b.async_press())
        b.coordinator.clear_schedule.assert_called_once()
        assert b.coordinator.scheduled_ready_at is None

    def test_press_never_touches_the_heater(self):
        """The whole point: cancelling a plan for later says nothing about now."""
        b = _button(scheduled=_NOW + timedelta(hours=3))
        _run(b.async_press())
        b.coordinator.api.set_temperature_setting.assert_not_called()
        b.coordinator.set_feature_state.assert_not_called()

    def test_press_pushes_the_new_state_to_listeners(self):
        """Scheduled for must re-render as unknown at once, not on the next poll."""
        b = _button(scheduled=_NOW + timedelta(hours=3))
        _run(b.async_press())
        b.coordinator.async_update_listeners.assert_called_once()

    def test_reason_is_recorded_for_the_log(self):
        b = _button(scheduled=_NOW + timedelta(hours=3))
        _run(b.async_press())
        assert "cancel" in b._reason["reason"].lower()


class TestStartupGuard:
    """Cancelling is destructive and silent, so a restart must not be able to do it.

    The hazard is concrete: the owner's calendar automation applies a schedule during
    startup, so a press landing in that window would clear the plan it had just set.
    """

    def _fresh(self, scheduled):
        b = _button(scheduled=scheduled)
        b._added_at = time.monotonic()          # just appeared
        return b

    def test_press_during_the_grace_window_is_refused(self):
        b = self._fresh(_NOW + timedelta(hours=3))
        _run(b.async_press())
        b.coordinator.clear_schedule.assert_not_called()
        assert b.coordinator.scheduled_ready_at is not None, "schedule was cleared"

    def test_press_after_the_grace_window_works(self):
        b = self._fresh(_NOW + timedelta(hours=3))
        b._added_at = time.monotonic() - (button_mod._PRESS_GRACE_SECONDS + 1)
        _run(b.async_press())
        b.coordinator.clear_schedule.assert_called_once()

    def test_press_with_nothing_scheduled_is_a_logged_no_op(self):
        b = self._fresh(None)
        b._added_at = time.monotonic() - (button_mod._PRESS_GRACE_SECONDS + 1)
        _run(b.async_press())               # must not raise
        b.coordinator.clear_schedule.assert_not_called()
