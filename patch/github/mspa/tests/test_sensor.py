"""Unit tests for MSpa sensor.py pure functions.

Uses unittest.mock to fake the coordinator — no Home Assistant runtime required.
Run with:  python -m pytest tests/test_sensor.py -v
        or: python -m unittest tests.test_sensor -v
"""
import sys
import os
from datetime import datetime, timezone, timedelta  # stdlib must be imported BEFORE path hack
from unittest.mock import MagicMock

# Patch homeassistant modules BEFORE adding mspa to path (avoids datetime.py shadowing)
ha_mock = MagicMock()
sys.modules["homeassistant"] = ha_mock
sys.modules["homeassistant.components"] = ha_mock.components
sys.modules["homeassistant.components.sensor"] = ha_mock.components.sensor
sys.modules["homeassistant.helpers"] = ha_mock.helpers
sys.modules["homeassistant.helpers.entity"] = ha_mock.helpers.entity
sys.modules["homeassistant.helpers.restore_state"] = ha_mock.helpers.restore_state
sys.modules["homeassistant.helpers.update_coordinator"] = ha_mock.helpers.update_coordinator
sys.modules["homeassistant.const"] = ha_mock.const
sys.modules["homeassistant.util"] = ha_mock.util
sys.modules["homeassistant.util.dt"] = ha_mock.util.dt
sys.modules["homeassistant.core"] = ha_mock.core

# Provide fake constants so import doesn't crash
ha_mock.const.UnitOfPower = type("U", (), {"WATT": "W"})()
ha_mock.const.UnitOfEnergy = type("U", (), {"KILO_WATT_HOUR": "kWh"})()
ha_mock.const.UnitOfTemperature = type("U", (), {"CELSIUS": "°C"})()

# Stub out SensorStateClass / SensorDeviceClass / EntityCategory enums
ha_mock.components.sensor.SensorStateClass = type("SSC", (), {"MEASUREMENT": "measurement"})()
ha_mock.components.sensor.SensorDeviceClass = type("SDC", (), {"TEMPERATURE": "temperature", "POWER": "power", "ENERGY": "energy"})()
ha_mock.helpers.entity.EntityCategory = type("EC", (), {"DIAGNOSTIC": "diagnostic"})()
ha_mock.helpers.restore_state.RestoreEntity = type("RE", (), {})()

# dt_util stubs
ha_mock.util.dt.utcnow = MagicMock(return_value=datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc))
ha_mock.util.dt.as_utc = lambda dt_obj: dt_obj.astimezone(timezone.utc) if dt_obj.tzinfo else dt_obj.replace(tzinfo=timezone.utc)
ha_mock.util.dt.now = MagicMock(return_value=datetime(2026, 7, 22, 14, 0, 0))
ha_mock.util.dt.as_local = lambda dt_obj: dt_obj.astimezone()

# Now safe to add mspa dir to path — stdlib datetime already loaded
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from sensor import (
    _compute_ready_at_value,
    _compute_schedule_value,
    _spa_direction,
    _effective_heat_rate,
    _effective_cool_rate,
    _fmt_local,
    _sra_utc,
    _segmented_heating_minutes,
    _heat_bucket_rate,
    _minutes_to_target,
)


def _make_coordinator(**overrides):
    """Build a minimal mock coordinator with sensible defaults."""
    c = MagicMock()
    c.near_target = False
    c.ready_latched = False
    c._schedule_triggered = False
    c.scheduled_ready_at = None
    c.schedule_target_temp = 40.0
    c.temp_anchor_time = datetime.now(timezone.utc)
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


class TestEffectiveCoolRate(unittest.TestCase):
    def test_uses_computed_rate(self):
        c = _make_coordinator(computed_cool_rate=0.4)
        self.assertAlmostEqual(_effective_cool_rate(c), 0.4)

    def test_no_rate(self):
        c = _make_coordinator(computed_cool_rate=None)
        self.assertIsNone(_effective_cool_rate(c))


class TestFmtLocal(unittest.TestCase):
    def test_same_day(self):
        dt = datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc)
        result = _fmt_local(dt)
        self.assertIn(":", result)
        self.assertNotIn("+", result)

    def test_next_day(self):
        dt = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
        result = _fmt_local(dt)
        self.assertIn(":", result)


class TestReadyAtValue(unittest.TestCase):
    """Tests for the 7-rule priority table in _compute_ready_at_value."""

    def test_rule1_near_target(self):
        c = _make_coordinator(near_target=True)
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_rule1_at_target(self):
        c = _make_coordinator()
        c._last_data = {"water_temperature": 40.0, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_rule2_latched_valid(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator(ready_latched=True)
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {"water_temperature": 39.5, "target_temperature": 35.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_rule2_latched_but_water_far(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator(ready_latched=True)
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {"water_temperature": 35.0, "target_temperature": 35.0, "heater": "off"}
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)

    def test_rule3_cooling_no_schedule(self):
        c = _make_coordinator()
        c._last_data = {"water_temperature": 42.0, "target_temperature": 40.0, "heater": "off"}
        self.assertEqual(_compute_ready_at_value(c), "Ready")

    def test_rule4_schedule_pending_heating(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator()
        c.scheduled_ready_at = future
        c.schedule_target_temp = 40.0
        c._last_data = {
            "water_temperature": 35.0,
            "target_temperature": 40.0,
            "heater": "on",
        }
        c.temp_anchor_target = 40.0
        c.computed_heat_rate = 1.5
        c.heat_rate_buckets = [1.8, 1.5, 1.0]
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)

    def test_rule5_no_schedule_heater_on_heating(self):
        c = _make_coordinator()
        c.scheduled_ready_at = None
        c._last_data = {
            "water_temperature": 35.0,
            "target_temperature": 40.0,
            "heater": "on",
        }
        c.temp_anchor_time = datetime.now(timezone.utc)
        c.temp_anchor_temp = 35.0
        c.temp_anchor_target = 40.0
        c.computed_heat_rate = 1.5
        c.heat_rate_buckets = [1.8, 1.5, 1.0]
        result = _compute_ready_at_value(c)
        self.assertIsNotNone(result)

    def test_rule6_future_schedule_waiting(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_coordinator()
        c.scheduled_ready_at = future
        c._last_data = {"water_temperature": 35.0, "target_temperature": 35.0, "heater": "off"}
        result = _compute_ready_at_value(c)
        self.assertIn(":", result)

    def test_rule7_nothing(self):
        c = _make_coordinator()
        c.scheduled_ready_at = None
        c._last_data = {"water_temperature": 35.0, "target_temperature": 40.0, "heater": "off"}
        c.computed_heat_rate = None
        self.assertIsNone(_compute_ready_at_value(c))


class TestComputeScheduleValue(unittest.TestCase):
    def test_none_returns_not_scheduled(self):
        self.assertEqual(_compute_schedule_value(None), "Not scheduled")

    def test_ready_sentinel(self):
        self.assertEqual(_compute_schedule_value("ready"), "Ready")

    def test_triggered_sentinel(self):
        self.assertEqual(_compute_schedule_value("triggered"), "Heating")

    def test_start_now(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        result = _compute_schedule_value((past, 40.0, past))
        self.assertEqual(result, "Start now")

    def test_start_at_future(self):
        target = datetime.now(timezone.utc) + timedelta(hours=2)
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        result = _compute_schedule_value((target, 40.0, start))
        self.assertIn("Start at", result)
        self.assertIn(":", result)


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

    def test_no_rate(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [None, None, None]
        c.computed_heat_rate = None
        c._last_data = {"device_heat_perhour": 0}
        self.assertIsNone(_segmented_heating_minutes(35.0, 40.0, c))

    def test_same_temp(self):
        c = _make_coordinator()
        self.assertEqual(_segmented_heating_minutes(40.0, 40.0, c), 0.0)

    def test_cooling_returns_zero(self):
        c = _make_coordinator()
        self.assertEqual(_segmented_heating_minutes(40.0, 35.0, c), 0.0)


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

    def test_device_fallback(self):
        c = _make_coordinator()
        c.heat_rate_buckets = [None, None, None]
        c.computed_heat_rate = None
        c._last_data = {"device_heat_perhour": 15}
        self.assertAlmostEqual(_heat_bucket_rate(c, 25.0), 1.5)


class TestMinutesToTarget(unittest.TestCase):
    def test_near_target_returns_zero(self):
        c = _make_coordinator(near_target=True)
        self.assertEqual(_minutes_to_target(c), 0)

    def test_no_anchor(self):
        c = _make_coordinator()
        c.temp_anchor_time = None
        self.assertIsNone(_minutes_to_target(c))

    def test_heating_with_rate(self):
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

    def test_equal_temps(self):
        c = _make_coordinator()
        c.temp_anchor_temp = 40.0
        c.temp_anchor_target = 40.0
        self.assertEqual(_minutes_to_target(c), 0)


class TestSraUtc(unittest.TestCase):
    def test_none(self):
        c = _make_coordinator(scheduled_ready_at=None)
        self.assertIsNone(_sra_utc(c))

    def test_aware_datetime(self):
        future = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
        c = _make_coordinator(scheduled_ready_at=future)
        result = _sra_utc(c)
        self.assertEqual(result, future)


if __name__ == "__main__":
    unittest.main()
