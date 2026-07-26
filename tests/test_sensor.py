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
    """Priority logic in _compute_ready_at_value."""

    def test_near_target_returns_ready(self):
        c = _make_coordinator(near_target=True)
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_at_target_returns_ready(self):
        """When water == target the coordinator sets near_target=True."""
        c = _make_coordinator(near_target=True)
        c._last_data = {"water_temperature": 40.0, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_latched_valid_water_at_schedule_target_returns_ready(self):
        """Latch honored when water is within 1°C of schedule target, with future SRA."""
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator(ready_latched=True)
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {"water_temperature": 39.5, "target_temperature": 35.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_latched_valid_after_sra_cleared(self):
        """Latch works even after scheduled_ready_at is cleared (schedule just completed)."""
        c = _make_coordinator(ready_latched=True)
        c.scheduled_ready_at = None   # coordinator already cleared it at the scheduled time
        c.schedule_target_temp = 40.0
        # water=39.5 — within 1°C of sched_temp → latch fires
        c._last_data = {"water_temperature": 39.5, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_latched_stale_water_below_schedule_target_shows_none(self):
        """Stale latch with water far below schedule target — at_sched guard blocks Rule 2."""
        c = _make_coordinator(ready_latched=True)
        c.scheduled_ready_at = None
        c.schedule_target_temp = 40.0
        # water=30, thermostat=35, heater off, no rate data → no rule matches → None
        c._last_data = {"water_temperature": 30.0, "target_temperature": 35.0, "heater": "off"}
        c.computed_heat_rate = None
        self.assertIsNone(_compute_ready_at_value(c))

    def test_at_target_direction_returns_ready_without_near_target(self):
        """direction=='at_target' → 'Ready' even when near_target flag is False."""
        c = _make_coordinator(near_target=False)
        c._last_data = {"water_temperature": 40.0, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_cooling_no_schedule_returns_ready(self):
        """Spa cooling above setpoint with no schedule — warm enough for use."""
        c = _make_coordinator()
        c._last_data = {"water_temperature": 42.0, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_future_schedule_pending_shows_time(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator()
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0, "heater": "on"}
        c.temp_anchor_target = 40.0
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)

    def test_no_schedule_heater_on_shows_eta(self):
        c = _make_coordinator()
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0, "heater": "on"}
        c.temp_anchor_time = datetime.now(timezone.utc)
        c.temp_anchor_temp = 35.0
        c.temp_anchor_target = 40.0
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)

    def test_future_schedule_no_latch_shows_scheduled_time(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator()
        c.scheduled_ready_at = future
        c._last_data = {"water_temperature": 35.0, "target_temperature": 35.0, "heater": "off"}
        result = _compute_ready_at_value(c)
        self.assertIn(":", result)

    def test_no_schedule_heater_off_returns_none(self):
        c = _make_coordinator()
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


if __name__ == "__main__":
    unittest.main()
