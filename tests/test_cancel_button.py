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
    """A control that does nothing is worse than one visibly not applicable."""

    def test_unavailable_with_nothing_scheduled(self):
        assert _button(scheduled=None).available is False

    def test_available_when_a_schedule_exists(self):
        assert _button(scheduled=_NOW + timedelta(hours=3)).available is True


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
