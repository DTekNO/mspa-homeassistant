"""Unit tests for MSpa sensor.py pure functions.

Uses unittest.mock to fake the coordinator — no Home Assistant runtime required.
Run with:  python -m pytest tests/test_sensor.py -v

conftest.py stubs homeassistant and sets up the custom_components.mspa package,
so no sys.modules manipulation is needed here.
"""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from custom_components.mspa.sensor import (
    _compute_ready_at_value,
    _compute_schedule_value,
    _spa_direction,
    _effective_heat_rate,
    _effective_cool_rate,
    _segmented_heating_minutes,
    _heat_bucket_rate,
    _minutes_to_target,
)
from custom_components.mspa.const import ambient_rate_factor, AMBIENT_FACTOR_MIN, AMBIENT_FACTOR_MAX

# Fixed reference time used by dt_util patches in this module.
_NOW_UTC   = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
_NOW_LOCAL = datetime(2026, 7, 22, 14, 0, 0)   # UTC+2, no tzinfo (local naive)


def _make_coordinator(**overrides):
    """Minimal mock coordinator with sensible defaults."""
    c = MagicMock()
    c.near_target = overrides.get("near_target", False)
    c.ready_latched = overrides.get("ready_latched", False)
    c._schedule_triggered = overrides.get("_schedule_triggered", False)
    c.scheduled_ready_at = None
    c.schedule_target_temp = 40.0
    c.temp_anchor_time = _NOW_UTC
    c.temp_anchor_temp = 35.0
    c.temp_anchor_target = 40.0
    c.computed_heat_rate = 1.5
    c.computed_cool_rate = 0.3
    c.heat_rate_buckets = [1.8, 1.5, 1.0]
    c._session_scalar = 1.0
    c._session_fresh_buckets = set()
    c.prediction_bias = 1.0
    c.ambient_temp = None
    c.ambient_baseline = None
    c._last_data = {
        "water_temperature": 35.0,
        "target_temperature": 40.0,
        "heater": "on",
        "device_heat_perhour": 15,
    }
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# ─────────────────────────────────────────────────────────────────────────────
class TestSpaDirection(unittest.TestCase):

    def test_heating(self):
        c = _make_coordinator()
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0}
        self.assertEqual(_spa_direction(c), "heating")

    def test_cooling(self):
        c = _make_coordinator()
        c._last_data = {"water_temperature": 40.0, "target_temperature": 35.0}
        self.assertEqual(_spa_direction(c), "cooling")

    def test_at_target(self):
        c = _make_coordinator()
        c._last_data = {"water_temperature": 40.0, "target_temperature": 40.0}
        self.assertEqual(_spa_direction(c), "at_target")

    def test_no_data(self):
        c = _make_coordinator()
        c._last_data = {"water_temperature": None, "target_temperature": 40.0}
        self.assertIsNone(_spa_direction(c))


# ─────────────────────────────────────────────────────────────────────────────
class TestEffectiveHeatRate(unittest.TestCase):

    def test_uses_computed_rate(self):
        c = _make_coordinator(computed_heat_rate=2.0)
        self.assertAlmostEqual(_effective_heat_rate(c), 2.0)

    def test_falls_back_to_device_rate(self):
        c = _make_coordinator(computed_heat_rate=None)
        c._last_data = {"device_heat_perhour": 15}
        self.assertAlmostEqual(_effective_heat_rate(c), 1.5)

    def test_clamps_device_rate_max(self):
        c = _make_coordinator(computed_heat_rate=None)
        c._last_data = {"device_heat_perhour": 25}
        self.assertAlmostEqual(_effective_heat_rate(c), 2.0)

    def test_clamps_device_rate_min(self):
        c = _make_coordinator(computed_heat_rate=None)
        c._last_data = {"device_heat_perhour": 1}
        self.assertAlmostEqual(_effective_heat_rate(c), 0.5)

    def test_no_rate(self):
        c = _make_coordinator(computed_heat_rate=None)
        c._last_data = {"device_heat_perhour": 0}
        self.assertIsNone(_effective_heat_rate(c))


# ─────────────────────────────────────────────────────────────────────────────
class TestEffectiveCoolRate(unittest.TestCase):

    def test_uses_computed_rate(self):
        c = _make_coordinator(computed_cool_rate=0.4)
        self.assertAlmostEqual(_effective_cool_rate(c), 0.4)

    def test_no_rate(self):
        c = _make_coordinator(computed_cool_rate=None)
        self.assertIsNone(_effective_cool_rate(c))


# ─────────────────────────────────────────────────────────────────────────────
class TestReadyAtValue(unittest.TestCase):
    """Six-state logic in _compute_ready_at_value."""

    # ── SCHEDULE PENDING context (sra_future, not triggered) ─────────────────
    # In this context the display is driven by sched_temp, not the thermostat.

    def test_schedule_pending_shows_scheduled_time_when_cold(self):
        """Schedule pending, spa cold: show the scheduled time."""
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {"water_temperature": 30.0, "target_temperature": 40.0, "heater": "off"}
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)
        self.assertIn(":", result)
        self.assertNotEqual(result, "Ready")

    def test_schedule_pending_shows_ready_when_near_sched_temp(self):
        """Schedule pending, spa already at schedule target (±1°C): Ready."""
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {"water_temperature": 39.5, "target_temperature": 35.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_schedule_pending_ignores_near_target_for_lower_thermostat(self):
        """near_target=True at maintenance setpoint must NOT override schedule-pending display."""
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator(near_target=True, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        # water at 35 (near thermostat=35), but sched_temp=40 → not near sched
        c._last_data = {"water_temperature": 35.0, "target_temperature": 35.0, "heater": "off"}
        result = _compute_ready_at_value(c)
        # near_target is irrelevant — schedule drives display
        self.assertIn(":", result)
        self.assertNotEqual(result, "Ready")

    # ── SCHEDULED HEATING (triggered) ────────────────────────────────────────

    def test_scheduled_heating_shows_eta_to_sched_temp(self):
        """triggered=True → anchor-based ETA to schedule_target_temp."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=True)
        c.scheduled_ready_at = None  # auto-clear may have already run
        c.schedule_target_temp = 40.0
        c.temp_anchor_time = datetime.now(timezone.utc)
        c.temp_anchor_temp = 35.0
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0, "heater": "on"}
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)
        self.assertIn(":", result)

    def test_scheduled_heating_near_done_shows_ready(self):
        """Triggered, ≤5min remaining → Ready."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=True)
        c.schedule_target_temp = 40.0
        c.temp_anchor_time = datetime.now(timezone.utc) - timedelta(minutes=2)
        c.temp_anchor_temp = 39.95
        c._last_data = {"water_temperature": 39.95, "target_temperature": 40.0, "heater": "on"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    # ── FREE context (no schedule, not triggered) ─────────────────────────────

    def test_free_near_target_returns_ready(self):
        """Free context: near_target flag → Ready."""
        c = _make_coordinator(near_target=True, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_free_latched_returns_ready(self):
        """Free context: latch persists Ready (e.g. after schedule auto-clears)."""
        c = _make_coordinator(near_target=False, ready_latched=True, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": 39.5, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_free_at_target_returns_ready(self):
        """Free context: water == thermostat (at_target direction) → Ready."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": 40.0, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_free_cooling_returns_none(self):
        """Free context: cooling direction → None (no ETA for cooling)."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": 42.0, "target_temperature": 40.0, "heater": "off"}
        self.assertIsNone(_compute_ready_at_value(c))

    def test_free_no_data_returns_none(self):
        """Free context: missing temperature data → None."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": None, "target_temperature": 40.0, "heater": "off"}
        self.assertIsNone(_compute_ready_at_value(c))

    def test_free_heating_shows_eta(self):
        """Free context: heater on, direction=heating → anchor ETA to thermostat."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c.temp_anchor_time = datetime.now(timezone.utc)
        c.temp_anchor_temp = 35.0
        c.temp_anchor_target = 40.0
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0, "heater": "on"}
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)
        self.assertIn(":", result)

    def test_free_heating_no_rate_returns_none(self):
        """Free context: heater on but no rate data → None."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c.computed_heat_rate = None
        c.heat_rate_buckets = [None, None, None]
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0,
                        "heater": "on", "device_heat_perhour": 0}
        self.assertIsNone(_compute_ready_at_value(c))

    def test_free_heater_off_returns_none(self):
        """Free context: heater off, direction=heating → None."""
        c = _make_coordinator(near_target=False, ready_latched=False, _schedule_triggered=False)
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0, "heater": "off"}
        c.computed_heat_rate = None
        self.assertIsNone(_compute_ready_at_value(c))


# ─────────────────────────────────────────────────────────────────────────────
class TestComputeScheduleValue(unittest.TestCase):

    def test_none_returns_not_scheduled(self):
        self.assertEqual(_compute_schedule_value(None), "Not scheduled")

    def test_ready_sentinel(self):
        self.assertEqual(_compute_schedule_value("ready"), "Ready")

    @patch("custom_components.mspa.sensor.dt_util")
    def test_start_now(self, mock_dt):
        past = _NOW_UTC - timedelta(hours=1)
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.now.return_value = _NOW_LOCAL
        result = _compute_schedule_value((past, 40.0, past))
        self.assertEqual(result, "Start now")

    @patch("custom_components.mspa.sensor.dt_util")
    def test_start_at_future(self, mock_dt):
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.now.return_value = _NOW_LOCAL
        mock_dt.as_local.side_effect = lambda dt: dt  # treat UTC == local in tests
        target = _NOW_UTC + timedelta(hours=2)
        start  = _NOW_UTC + timedelta(hours=1)
        result = _compute_schedule_value((target, 40.0, start))
        self.assertIn("Start at", result)
        self.assertIn(":", result)


# ─────────────────────────────────────────────────────────────────────────────
class TestSegmentedHeatingMinutes(unittest.TestCase):

    def test_single_bucket(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [2.0, None, None]
        c._session_scalar = 1.0
        c._session_fresh_buckets = {0}
        minutes = _segmented_heating_minutes(28.0, 29.0, c)
        self.assertIsNotNone(minutes)
        self.assertGreater(minutes, 0.0)

    def test_cross_bucket(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [2.0, 1.5, 1.0]
        c._session_scalar = 1.0
        c._session_fresh_buckets = {0, 1, 2}
        minutes = _segmented_heating_minutes(28.0, 38.0, c)
        self.assertIsNotNone(minutes)
        self.assertGreater(minutes, 0.0)

    def test_no_rate_returns_none(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [None, None, None]
        c.computed_heat_rate = None
        c._last_data = {"device_heat_perhour": 0}
        self.assertIsNone(_segmented_heating_minutes(35.0, 40.0, c))

    def test_same_temp_returns_zero(self):
        c = _make_coordinator()
        self.assertEqual(_segmented_heating_minutes(40.0, 40.0, c), 0.0)

    def test_cooling_direction_returns_zero(self):
        c = _make_coordinator()
        self.assertEqual(_segmented_heating_minutes(40.0, 35.0, c), 0.0)


# ─────────────────────────────────────────────────────────────────────────────
class TestHeatBucketRate(unittest.TestCase):

    def test_direct_bucket(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [2.0, 1.5, 1.0]
        c._session_fresh_buckets = {0}
        self.assertAlmostEqual(_heat_bucket_rate(c, 25.0), 2.0)

    def test_adjacent_fallback(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [None, 1.5, 1.0]
        c._session_fresh_buckets = set()
        self.assertAlmostEqual(_heat_bucket_rate(c, 25.0), 1.5)

    def test_device_rate_fallback(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [None, None, None]
        c.computed_heat_rate = None
        c._last_data = {"device_heat_perhour": 15}
        self.assertAlmostEqual(_heat_bucket_rate(c, 25.0), 1.5)


# ─────────────────────────────────────────────────────────────────────────────
class TestMinutesToTarget(unittest.TestCase):

    def test_near_target_returns_zero(self):
        c = _make_coordinator(near_target=True)
        self.assertEqual(_minutes_to_target(c), 0)

    def test_no_anchor_returns_none(self):
        c = _make_coordinator()
        c.temp_anchor_time = None
        self.assertIsNone(_minutes_to_target(c))

    def test_heating_with_rate_returns_positive(self):
        c = _make_coordinator()
        c.temp_anchor_time = datetime.now(timezone.utc)
        c.temp_anchor_temp = 35.0
        c.temp_anchor_target = 40.0
        c.heat_rate_buckets = [2.0, 1.5, 1.0]
        c._session_fresh_buckets = {0, 1, 2}
        c._session_scalar = 1.0
        result = _minutes_to_target(c)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_equal_anchor_temps_returns_zero(self):
        c = _make_coordinator()
        c.temp_anchor_temp = 40.0
        c.temp_anchor_target = 40.0
        self.assertEqual(_minutes_to_target(c), 0)


# ─────────────────────────────────────────────────────────────────────────────
class TestAmbientRateFactor(unittest.TestCase):

    def test_no_data_returns_one(self):
        """Without weather data factor is always 1.0 (no correction)."""
        self.assertEqual(ambient_rate_factor(2, None, None), 1.0)
        self.assertEqual(ambient_rate_factor(2, None, 15.0), 1.0)
        self.assertEqual(ambient_rate_factor(2, 5.0, None), 1.0)

    def test_at_baseline_returns_one(self):
        """When ambient equals baseline, factor is exactly 1.0."""
        self.assertAlmostEqual(ambient_rate_factor(2, 15.0, 15.0), 1.0)

    def test_cold_bucket_insensitive(self):
        """Cold bucket (idx 0) has zero sensitivity — factor always 1.0."""
        self.assertAlmostEqual(ambient_rate_factor(0, 0.0, 15.0), 1.0)
        self.assertAlmostEqual(ambient_rate_factor(0, 30.0, 15.0), 1.0)

    def test_hot_bucket_cold_night_slows(self):
        """Hot bucket (idx 2) slows on cold night: sensitivity=0.06/°C.
        Baseline 15°C, outdoor 5°C → delta=-10 → factor=1+0.06*(-10)=0.4."""
        self.assertAlmostEqual(ambient_rate_factor(2, 5.0, 15.0), 0.4)

    def test_hot_bucket_warm_night_speeds(self):
        """Hot bucket speeds on warm night: 25°C vs baseline 15°C → factor=1+0.06*10=1.6 → clamped to 1.5."""
        self.assertAlmostEqual(ambient_rate_factor(2, 25.0, 15.0), AMBIENT_FACTOR_MAX)

    def test_extreme_cold_clamped(self):
        """Extreme cold is clamped at AMBIENT_FACTOR_MIN."""
        # Would be 1 + 0.06 * (-100) = -5, clamped to 0.3
        self.assertAlmostEqual(ambient_rate_factor(2, -85.0, 15.0), AMBIENT_FACTOR_MIN)

    def test_mid_bucket_moderate_sensitivity(self):
        """Mid bucket (idx 1) has 0.02/°C sensitivity.
        Baseline 15°C, outdoor 5°C → factor=1+0.02*(-10)=0.8."""
        self.assertAlmostEqual(ambient_rate_factor(1, 5.0, 15.0), 0.8)

    def test_invalid_bucket_returns_one(self):
        self.assertEqual(ambient_rate_factor(-1, 5.0, 15.0), 1.0)
        self.assertEqual(ambient_rate_factor(3, 5.0, 15.0), 1.0)

    def test_ambient_correction_applied_in_heat_bucket_rate(self):
        """_heat_bucket_rate applies ambient factor when no fresh session data."""
        c = _make_coordinator()
        c.heat_rate_buckets = [None, None, 1.0]  # only hot bucket set
        c._session_fresh_buckets = set()          # no fresh data
        c._session_scalar = 1.0
        c.ambient_temp = 5.0      # cold night
        c.ambient_baseline = 15.0  # 10°C below baseline
        # hot bucket rate = 1.0 * (1 + 0.06 * -10) = 0.4
        rate = _heat_bucket_rate(c, 38.0)
        self.assertAlmostEqual(rate, 0.4)

    def test_fresh_session_data_bypasses_ambient(self):
        """Fresh session observations are used verbatim — ambient model not applied."""
        c = _make_coordinator()
        c.heat_rate_buckets = [None, None, 1.0]
        c._session_fresh_buckets = {2}   # hot bucket observed this session
        c._session_scalar = 1.0
        c.ambient_temp = 5.0
        c.ambient_baseline = 15.0
        rate = _heat_bucket_rate(c, 38.0)
        self.assertAlmostEqual(rate, 1.0)  # no correction applied


if __name__ == "__main__":
    unittest.main()
