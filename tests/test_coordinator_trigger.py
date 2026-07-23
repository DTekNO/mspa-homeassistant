"""Tests for MSpaUpdateCoordinator schedule-trigger logic.

Covers:
  _compute_heating_minutes — pure heating-time calculation used by the trigger
  _bucket_rate_at          — per-bucket rate lookup with session-scalar handling
  _check_schedule_trigger  — the async method that fires the API calls

Because conftest.py stubs coordinator dependencies (DataUpdateCoordinator,
Store, mspa_api) without pre-stubbing the coordinator module itself, we can
import the real MSpaUpdateCoordinator class here.

Instances are created with object.__new__ so __init__ is never called; we set
only the attributes the tested method actually reads.
"""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.mspa.coordinator import MSpaUpdateCoordinator

# ── Fixed "now" ────────────────────────────────────────────────────────────────
_NOW_UTC = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _coord(**overrides) -> MSpaUpdateCoordinator:
    """Return a minimal coordinator instance.

    Uses object.__new__ to skip __init__.  Only attributes read by the
    trigger method and the rate helpers are set.
    """
    c = object.__new__(MSpaUpdateCoordinator)
    c.scheduled_ready_at = None
    c._schedule_triggered = False
    c.schedule_target_temp = 39.0
    c.heat_rate_buckets = [2.0, 2.0, 2.0]
    c._session_scalar = 1.0
    c._session_fresh_buckets = {0, 1, 2}
    c.prediction_bias = 1.0
    c.computed_heat_rate = 2.0
    c._last_data = {"heater": "off", "water_temperature": "38.0", "device_heat_perhour": 0}
    c.api = MagicMock()
    c.api.set_temperature_setting = AsyncMock()
    c.set_feature_state = AsyncMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def freeze_time():
    """Patch dt_util inside coordinator.py to a fixed UTC time."""
    with patch("custom_components.mspa.coordinator.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.as_utc.side_effect = (
            lambda dt: dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        )
        yield mock_dt


# ═══════════════════════════════════════════════════════════════════════════════
# _compute_heating_minutes
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeHeatingMinutes:

    def test_returns_zero_when_already_at_target(self):
        c = _coord()
        assert c._compute_heating_minutes(39.0, 39.0) == 0.0

    def test_returns_zero_when_above_target(self):
        c = _coord()
        assert c._compute_heating_minutes(40.0, 39.0) == 0.0

    def test_returns_zero_when_delta_less_than_half_degree(self):
        # 0.3°C gap < 0.5 → epsilon shortcut → 0.0
        c = _coord()
        assert c._compute_heating_minutes(38.8, 39.0) == 0.0

    def test_returns_zero_when_delta_exactly_half_degree_boundary(self):
        # Condition is (to - from) < 0.5, so exactly 0.5 should NOT be zero
        c = _coord()
        result = c._compute_heating_minutes(38.5, 39.0)
        assert result is not None and result > 0.0

    def test_single_bucket_one_degree(self):
        # 38°C → 39°C: all in bucket 2 (≥ 37°C), rate = 2.0°C/h → 30 min
        c = _coord()
        result = c._compute_heating_minutes(38.0, 39.0)
        assert result == pytest.approx(30.0, abs=0.01)

    def test_single_bucket_cold(self):
        # 20°C → 25°C: all in bucket 0 (< 30°C), rate = 2.0°C/h → 150 min
        c = _coord()
        result = c._compute_heating_minutes(20.0, 25.0)
        assert result == pytest.approx(150.0, abs=0.01)

    def test_full_cold_start_three_buckets(self):
        # 20°C → 40°C crosses all three buckets at 30.0 and 37.0
        # Bucket 0: 10°C at 2°C/h = 300 min
        # Bucket 1: 7°C at 2°C/h  = 210 min
        # Bucket 2: 3°C at 2°C/h  = 90 min
        # Total = 600 min
        c = _coord()
        result = c._compute_heating_minutes(20.0, 40.0)
        assert result == pytest.approx(600.0, abs=0.01)

    def test_returns_none_when_no_rate_for_segment(self):
        # No rate data in any bucket → _bucket_rate_at returns None → None result
        c = _coord(
            heat_rate_buckets=[None, None, None],
            computed_heat_rate=None,
        )
        c._last_data["device_heat_perhour"] = 0
        result = c._compute_heating_minutes(20.0, 40.0)
        assert result is None

    def test_device_rate_fallback_when_no_buckets(self):
        # No bucket data, device reports 20 (= 2.0°C/h after /10 clamp)
        c = _coord(
            heat_rate_buckets=[None, None, None],
            computed_heat_rate=None,
        )
        c._last_data["device_heat_perhour"] = 20  # 2.0°C/h
        # 1°C at 2°C/h → 30 min
        result = c._compute_heating_minutes(38.0, 39.0)
        assert result == pytest.approx(30.0, abs=0.01)

    def test_prediction_bias_scales_result(self):
        c = _coord(prediction_bias=1.2)
        # 1°C at 2°C/h = 30 min → with bias 1.2 → 36 min
        result = c._compute_heating_minutes(38.0, 39.0)
        assert result == pytest.approx(36.0, abs=0.01)

    def test_per_bucket_rates(self):
        # Different rates per bucket: cold=3.0, mid=2.0, hot=1.0
        c = _coord(heat_rate_buckets=[3.0, 2.0, 1.0])
        # 20°C → 40°C:
        # Bucket 0 [20→30]: 10°C / 3.0°C/h = 200 min
        # Bucket 1 [30→37]: 7°C / 2.0°C/h  = 210 min
        # Bucket 2 [37→40]: 3°C / 1.0°C/h  = 180 min
        # Total = 590 min
        result = c._compute_heating_minutes(20.0, 40.0)
        assert result == pytest.approx(590.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# _bucket_rate_at — session scalar and fallback logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestBucketRateAt:

    def test_uses_matching_bucket(self):
        c = _coord(heat_rate_buckets=[3.0, 2.0, 1.0])
        assert c._bucket_rate_at(20.0) == 3.0  # bucket 0
        assert c._bucket_rate_at(33.0) == 2.0  # bucket 1
        assert c._bucket_rate_at(38.0) == 1.0  # bucket 2

    def test_falls_back_to_adjacent_bucket(self):
        # Only bucket 0 has data; temp 33°C is in bucket 1.
        c = _coord(heat_rate_buckets=[3.0, None, None])
        # Should find bucket 0 as fallback
        assert c._bucket_rate_at(33.0) == 3.0

    def test_falls_back_to_device_rate(self):
        c = _coord(heat_rate_buckets=[None, None, None], computed_heat_rate=None)
        c._last_data["device_heat_perhour"] = 20  # 2.0°C/h
        result = c._bucket_rate_at(20.0)
        assert result == pytest.approx(2.0, abs=0.01)

    def test_session_scalar_applied_to_unfresh_bucket(self):
        # Bucket 0 has a stored rate; _session_fresh_buckets does NOT include bucket 0
        # → scalar is applied
        c = _coord(
            heat_rate_buckets=[2.0, None, None],
            _session_scalar=1.5,
            _session_fresh_buckets=set(),  # no fresh buckets
        )
        result = c._bucket_rate_at(20.0)  # bucket 0, not fresh → scaled
        assert result == pytest.approx(3.0, abs=0.01)

    def test_session_scalar_not_applied_to_fresh_bucket(self):
        # Bucket 0 is fresh → scalar NOT applied
        c = _coord(
            heat_rate_buckets=[2.0, None, None],
            _session_scalar=1.5,
            _session_fresh_buckets={0},
        )
        result = c._bucket_rate_at(20.0)
        assert result == pytest.approx(2.0, abs=0.01)

    def test_returns_none_when_no_data(self):
        c = _coord(heat_rate_buckets=[None, None, None], computed_heat_rate=None)
        c._last_data["device_heat_perhour"] = 0
        assert c._bucket_rate_at(20.0) is None


# ═══════════════════════════════════════════════════════════════════════════════
# _check_schedule_trigger — async trigger behavior
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckScheduleTrigger:

    # ── Guard conditions (should NOT fire) ─────────────────────────────────────

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
        c.set_feature_state.assert_not_called()

    def test_none_current_temp_does_not_fire(self):
        c = _coord(scheduled_ready_at=_NOW_UTC + timedelta(hours=1))
        _run(c._check_schedule_trigger(None, None))
        c.api.set_temperature_setting.assert_not_called()

    def test_too_early_does_not_fire(self):
        # Water 38°C → 39°C: 1°C at 2°C/h = 30 min lead needed.
        # Target 2h from now → start is 90 min from now → not yet time.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=2),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.0"
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_not_called()

    def test_no_rate_data_before_target_time_does_not_fire(self):
        # No rate data, target still in future → wait.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=1),
            schedule_target_temp=40.0,
            heat_rate_buckets=[None, None, None],
            computed_heat_rate=None,
        )
        c._last_data["device_heat_perhour"] = 0
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_not_called()

    # ── Trigger fires ─────────────────────────────────────────────────────────

    def test_fires_when_start_time_reached(self):
        # Target 30 min away, lead needed = 30 min → start = NOW → fires.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=30),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.0"
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_called_once_with(39.0)

    def test_fires_when_start_time_passed(self):
        # Target in the past → start time also past → fires.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=10),
            schedule_target_temp=40.0,
        )
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)

    def test_fires_with_correct_schedule_target_temp(self):
        # Confirm that the setpoint sent matches schedule_target_temp, not current target.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=42.0,
        )
        c._last_data["water_temperature"] = "38.0"
        _run(c._check_schedule_trigger(38.0, None))
        c.api.set_temperature_setting.assert_called_once_with(42.0)

    def test_sets_triggered_flag_on_success(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=39.0,
        )
        assert c._schedule_triggered is False
        _run(c._check_schedule_trigger(38.0, None))
        assert c._schedule_triggered is True

    # ── Heater state ─────────────────────────────────────────────────────────

    def test_turns_on_heater_when_off(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=39.0,
        )
        c._last_data["heater"] = "off"
        _run(c._check_schedule_trigger(38.0, None))
        c.set_feature_state.assert_called_once_with("heater", "on")

    def test_does_not_turn_on_heater_when_already_on(self):
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=39.0,
        )
        c._last_data["heater"] = "on"
        _run(c._check_schedule_trigger(38.0, None))
        # Setpoint confirmed but heater already on → no set_feature_state call
        c.api.set_temperature_setting.assert_called_once()
        c.set_feature_state.assert_not_called()

    def test_setpoint_always_confirmed_even_when_heater_on(self):
        # Heater already on (user may have started it manually) — setpoint still confirmed.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=40.0,
        )
        c._last_data["heater"] = "on"
        c._last_data["water_temperature"] = "39.0"
        _run(c._check_schedule_trigger(39.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)

    # ── Epsilon / warm-day case ───────────────────────────────────────────────

    def test_fires_exactly_at_target_time_when_delta_is_tiny(self):
        # Water 38.8°C → target 39.0°C: delta=0.2 < 0.5 → minutes_needed=0.
        # start_at = target_utc → fires when now >= target_utc.
        # Target is 1 second in the past.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(seconds=1),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.8"
        _run(c._check_schedule_trigger(38.8, None))
        c.api.set_temperature_setting.assert_called_once_with(39.0)

    def test_does_not_fire_before_target_time_when_delta_is_tiny(self):
        # Same tiny delta but target is still 1 minute in the future → should NOT fire.
        c = _coord(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=1),
            schedule_target_temp=39.0,
        )
        c._last_data["water_temperature"] = "38.8"
        _run(c._check_schedule_trigger(38.8, None))
        c.api.set_temperature_setting.assert_not_called()

    # ── No rate data fallback ─────────────────────────────────────────────────

    def test_fires_at_target_time_when_no_rate_data(self):
        # No rate data; scheduled time has arrived → fallback fires immediately.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(seconds=5),
            schedule_target_temp=40.0,
            heat_rate_buckets=[None, None, None],
            computed_heat_rate=None,
        )
        c._last_data["device_heat_perhour"] = 0
        _run(c._check_schedule_trigger(20.0, None))
        c.api.set_temperature_setting.assert_called_once_with(40.0)

    # ── Error handling ────────────────────────────────────────────────────────

    def test_api_error_does_not_raise_and_leaves_triggered_false(self):
        # If the API call fails, the exception is caught and logged.
        # _schedule_triggered must NOT be set to True.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=39.0,
        )
        c.api.set_temperature_setting.side_effect = RuntimeError("API timeout")
        _run(c._check_schedule_trigger(38.0, None))
        assert c._schedule_triggered is False

    def test_trigger_idempotent_after_first_fire(self):
        # After the trigger fires once and sets _schedule_triggered=True,
        # a second call must be a no-op.
        c = _coord(
            scheduled_ready_at=_NOW_UTC - timedelta(minutes=5),
            schedule_target_temp=39.0,
        )
        _run(c._check_schedule_trigger(38.0, None))
        assert c._schedule_triggered is True
        call_count = c.api.set_temperature_setting.call_count

        # Second call — should be blocked by the guard
        _run(c._check_schedule_trigger(38.0, None))
        assert c.api.set_temperature_setting.call_count == call_count  # no new calls
