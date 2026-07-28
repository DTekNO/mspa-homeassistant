"""Tests for MSpaUpdateCoordinator._check_schedule_trigger.

Exercises the async trigger method that fires the API call at the right time.
All internal math helpers (bucket rates, heating minutes) are tested via
behaviour: if the trigger fires or doesn't fire given a specific spa state, the
math is implicitly verified.

Run with: python -m pytest tests/test_trigger.py -v
"""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.mspa.coordinator import MSpaUpdateCoordinator

_NOW_UTC = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


# ── Minimal coordinator factory ───────────────────────────────────────────────

def _coord(**overrides) -> MSpaUpdateCoordinator:
    """Return a minimal coordinator instance via object.__new__ (skips __init__)."""
    c = object.__new__(MSpaUpdateCoordinator)
    c.scheduled_ready_at = None
    c._schedule_triggered = False
    c.schedule_target_temp = 39.0
    c.heat_rate_buckets = [2.0, 2.0, 2.0]
    c._session_scalar = 1.0
    c._session_fresh_buckets = {0, 1, 2}
    c.prediction_bias = 1.0
    c.computed_heat_rate = 2.0
    c.ambient_temp = None
    c.ambient_baseline = None
    c._last_computed_start_at = None
    c.ready_latched = False
    c.near_target = False
    c._last_data = {"heater": "off", "water_temperature": "38.0", "device_heat_perhour": 0}
    c.api = MagicMock()
    c.api.set_temperature_setting = AsyncMock()
    c.set_feature_state = AsyncMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def freeze_time():
    with patch("custom_components.mspa.coordinator.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.as_utc.side_effect = (
            lambda dt: dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        )
        yield mock_dt


# ═══════════════════════════════════════════════════════════════════════════════
# Guard conditions (should NOT fire)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerGuards:

    def test_no_schedule_does_not_fire(self):
        c = _coord(scheduled_ready_at=None)
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_not_called()
        c.set_feature_state.assert_not_called()

    def test_already_triggered_does_not_fire(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(hours=1),
            _schedule_triggered=True,
        )
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_not_called()

    def test_none_current_temp_does_not_fire(self):
        c = _coord(scheduled_ready_at=_NOW_UTC + timedelta(hours=1))
        _run(c._check_schedule_trigger(None, None))
        c.api.set_temperature_setting.assert_not_called()

    def test_too_early_does_not_fire(self):
        # 38→39°C: 1°C at 2°C/h = 30 min needed. Target 2h away → start 90 min from now.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=2),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.0"
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_not_called()

    def test_no_rate_before_target_time_does_not_fire(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=1),
            schedule_target_temp=40.0,
            heat_rate_buckets=[None, None, None],
            computed_heat_rate=None,
        )
        c._last_data["device_heat_perhour"] = 0
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_not_called()

    def test_tiny_delta_before_target_time_does_not_fire(self):
        # 38.8→39°C: delta=0.2 < 0.5 → minutes_needed=0 → start_at=target_utc.
        # Target is still 1 min in the future → should NOT fire.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=1),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.8"
        _run(c._check_schedule_trigger(38.8, None))
        c.api.set_temperature_setting.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Trigger fires
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerFires:

    def test_fires_when_start_time_reached(self):
        # 38→39°C = 30 min needed. Target 30 min away → start = NOW → fires.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=30),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.0"
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_called_once_with(39.0)

    def test_fires_when_target_time_passed(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=10),
            schedule_target_temp=40.0,
        )
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)

    def test_fires_with_correct_target_temp(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=42.0,
        )
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_called_once_with(42.0)

    def test_sets_triggered_flag_on_success(self):
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=5), schedule_target_temp=39.0)
        assert c._schedule_triggered is False
        _run(c._check_schedule_trigger(38.0, None))
        assert c._schedule_triggered is True

    def test_fires_at_target_time_when_delta_is_tiny(self):
        # 38.8→39°C: delta=0.2 < 0.5 → minutes_needed=0, start_at=target_utc.
        # Target is 1 second in the past → fires.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(seconds=1),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.8"
        _run(c._check_schedule_trigger(38.8, None))
        c.api.set_temperature_setting.assert_called_once_with(39.0)

    def test_fires_at_target_time_when_no_rate_data(self):
        # No learned rate; scheduled time has passed → fires immediately.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(seconds=5),
            schedule_target_temp=40.0,
            heat_rate_buckets=[None, None, None],
            computed_heat_rate=None,
        )
        c._last_data["device_heat_perhour"] = 0
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Heater state
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerHeaterState:

    def test_turns_on_heater_when_off(self):
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=5), schedule_target_temp=39.0)
        c._last_data["heater"] = "off"
        _run(c._check_schedule_trigger(38.0, None))
        c.set_feature_state.assert_called_once_with("heater", "on")

    def test_does_not_turn_on_heater_when_already_on(self):
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=5), schedule_target_temp=39.0)
        c._last_data["heater"] = "on"
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_called_once()
        c.set_feature_state.assert_not_called()

    def test_setpoint_confirmed_even_when_heater_already_on(self):
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=5), schedule_target_temp=40.0)
        c._last_data["heater"] = "on"
        c._last_data["water_temperature"] = "39.0"
        _run(c._check_schedule_trigger(39.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerErrors:

    def test_api_error_does_not_raise_and_leaves_triggered_false(self):
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=5), schedule_target_temp=39.0)
        c.api.set_temperature_setting.side_effect = RuntimeError("API timeout")
        _run(c._check_schedule_trigger(38.0, None))
        assert c._schedule_triggered is False

    def test_trigger_is_idempotent(self):
        """A second call after successful trigger must not re-fire the API."""
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=5), schedule_target_temp=39.0)
        _run(c._check_schedule_trigger(38.0, None))
        assert c._schedule_triggered is True
        call_count = c.api.set_temperature_setting.call_count
        _run(c._check_schedule_trigger(38.0, None))
        assert c.api.set_temperature_setting.call_count == call_count
