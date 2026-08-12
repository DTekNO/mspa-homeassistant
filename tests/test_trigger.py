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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

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


# ═══════════════════════════════════════════════════════════════════════════════
# Schedule expiry — the latch must release with the schedule
# ═══════════════════════════════════════════════════════════════════════════════

class TestScheduleExpiry:
    """Once the scheduled ready time passes, the whole schedule state retires —
    including ready_latched.  The latch holds 'Ready' steady through the
    session, but leaving it set afterwards pinned the Ready at sensor on
    'Ready' indefinitely (until the next schedule or an integration reload)
    while the water cooled or the thermostat was lowered."""

    def test_expiry_clears_schedule_trigger_and_latch(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            _schedule_triggered=True,
            ready_latched=True,
            _last_computed_start_at=_NOW_UTC - timedelta(hours=3),
        )
        c._clear_schedule_if_expired(39.5)
        assert c.scheduled_ready_at is None
        assert c._schedule_triggered is False
        assert c._last_computed_start_at is None
        assert c.ready_latched is False

    def test_future_schedule_is_left_alone(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=2),
            _schedule_triggered=True,
            ready_latched=True,
        )
        c._clear_schedule_if_expired(39.5)
        assert c.scheduled_ready_at is not None
        assert c._schedule_triggered is True
        assert c.ready_latched is True

    def test_no_schedule_is_a_noop(self):
        c = _coord(scheduled_ready_at=None, ready_latched=True)
        c._clear_schedule_if_expired(39.5)
        assert c.ready_latched is True, "free-session latch is not the expiry's business"

    def test_expiry_with_none_temp_does_not_raise(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=1),
            ready_latched=True,
        )
        c._clear_schedule_if_expired(None)
        assert c.ready_latched is False


# ═══════════════════════════════════════════════════════════════════════════════
# CANCELLING A SCHEDULE  (github: romd87, 2026-08-08)
# ═══════════════════════════════════════════════════════════════════════════════

class TestClearSchedule:
    """clear_schedule is shared by the expiry auto-clear and the Cancel button.

    Its contract: retire the schedule and everything derived from it, and leave the
    heater alone. Cancelling a plan for later says nothing about whether the spa
    should be heating now.
    """

    def test_clears_every_derived_flag(self):
        c = _coord(scheduled_ready_at=_NOW_UTC + timedelta(hours=5),
                   schedule_target_temp=40.0)
        c._schedule_triggered = True
        c.ready_latched = True
        c.ready_latched_temp = 39.0
        c._last_computed_start_at = _NOW_UTC
        c.clear_schedule("test")
        assert c.scheduled_ready_at is None
        assert c._schedule_triggered is False
        assert c.ready_latched is False
        assert c.ready_latched_temp is None
        assert c._last_computed_start_at is None

    def test_does_not_touch_the_heater(self):
        c = _coord(scheduled_ready_at=_NOW_UTC + timedelta(hours=5),
                   schedule_target_temp=40.0)
        c.clear_schedule("test")
        c.api.set_temperature_setting.assert_not_called()
        c.api.set_feature_state.assert_not_called()

    def test_is_a_no_op_with_nothing_scheduled(self):
        c = _coord()
        c.clear_schedule("test")
        assert c.scheduled_ready_at is None


class TestStaleTargetIsAbandoned:
    """A target well in the past is an abandoned plan, not a window just opened.

    Reported 2026-08-08: editing the schedule's date to a past day — the only way
    to clear it before the Cancel button existed — cleared the schedule *and*
    switched the heater on, because the trigger runs before the expiry clear.
    """

    def test_a_day_old_target_does_not_start_the_heater(self):
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(days=3),
                   schedule_target_temp=40.0)
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_not_called()
        c.api.set_feature_state.assert_not_called()
        assert c.scheduled_ready_at is None, "stale schedule should be cleared"

    def test_a_recently_passed_target_still_fires(self):
        """The deliberate 'fire as soon as the window opens' behaviour must stand."""
        c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=10),
                   schedule_target_temp=40.0)
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)

    def test_the_boundary_is_an_hour(self):
        for mins, should_fire in ((55, True), (65, False)):
            c = _coord(scheduled_ready_at=_NOW_UTC - timedelta(minutes=mins),
                       schedule_target_temp=40.0)
            _run(c._check_schedule_trigger(20.0, None))
            fired = c.api.set_temperature_setting.called
            assert fired is should_fire, f"{mins} min late: fired={fired}"


class TestFaultBlocksSwitchingOn:
    """A faulting spa must say so in the UI, not just in the log.

    F1 is the flow fault — raised when the pump starts with a physical problem in the
    way, and not clearable remotely. So actuation refuses rather than retrying into it,
    and the refusal is a ServiceValidationError, which Home Assistant renders as a
    message to the user instead of "unknown error".
    """

    def _coord(self, fault="OK"):
        c = MSpaUpdateCoordinator.__new__(MSpaUpdateCoordinator)
        c._last_data = {"fault": fault, "filter": "on", "heater": "off"}
        c.api = MagicMock()
        c.api.set_heater_state = AsyncMock(return_value={"message": "SUCCESS"})
        c.api.set_filter_state = AsyncMock(return_value={"message": "SUCCESS"})
        c._enable_rapid_polling = MagicMock()
        c.async_request_refresh = AsyncMock()
        return c

    def test_fault_code_is_none_when_healthy(self):
        assert self._coord("OK").fault_code is None
        assert self._coord("").fault_code is None

    def test_fault_code_reports_the_code(self):
        assert self._coord("F1").fault_code == "F1"

    def test_switching_on_during_a_fault_raises_for_the_ui(self):
        c = self._coord("F1")
        with pytest.raises(ServiceValidationError) as err:
            _run(c.set_feature_state("heater", "on"))
        assert "F1" in str(err.value)
        c.api.set_heater_state.assert_not_called()

    def test_switching_off_during_a_fault_is_allowed(self):
        """Shutting a faulting spa down is exactly what the user needs to be able to do."""
        c = self._coord("F1")
        _run(c.set_feature_state("heater", "off"))
        c.api.set_heater_state.assert_awaited_once_with(0)

    def test_healthy_spa_switches_on_normally(self):
        c = self._coord("OK")
        _run(c.set_feature_state("heater", "on"))
        c.api.set_heater_state.assert_awaited_once_with(1)

    def test_pump_refusal_is_reported_to_the_ui(self):
        """Not a bare RuntimeError, which the frontend shows as 'unknown error'."""
        c = self._coord("OK")
        c._last_data["filter"] = "off"
        c.api.set_filter_state = AsyncMock(return_value={"message": "FAIL"})
        with pytest.raises(HomeAssistantError) as err:
            _run(c.set_feature_state("heater", "on"))
        assert "circulation pump" in str(err.value)
        c.api.set_heater_state.assert_not_called()
