"""Tests for MSpaHeatScheduleSensor state machine.

Covers all five display states:
  - Not scheduled  (no schedule, past, beyond lookahead, no data, no rate)
  - Ready          (water already within 1°C of target)
  - Start at HH:MM (computed start time in the future)
  - Start at HH:MM +Nd (start time on a future date)
  - Start now      (now >= computed start time)

Uses the same MockCoordinator / _SensorStub pattern as test_ready_at_sensor.py.
"""
import re
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from custom_components.mspa.sensor import MSpaHeatScheduleSensor
from custom_components.mspa.const import (
    CONF_SCHEDULE_LOOKAHEAD_DAYS,
    DEFAULT_SCHEDULE_LOOKAHEAD_DAYS,
)

# ── Fixed "now" used across all tests ────────────────────────────────────────
# Saturday 2026-07-19 12:00:00 UTC
_NOW_UTC = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
_NOW_LOCAL = _NOW_UTC  # tests treat UTC == local for simplicity


# ── Mock coordinator ──────────────────────────────────────────────────────────

class MockCoordinator:
    """Minimal coordinator with every attribute the heat-schedule sensor reads."""

    def __init__(
        self,
        *,
        water_temp: float = 38.0,
        target_temp: float = 38.0,
        scheduled_ready_at: "datetime | None" = None,
        schedule_target_temp: float = 39.0,
        heat_rate: "float | None" = 2.0,
        cool_rate: "float | None" = None,
        device_heat_perhour: int = 0,
        schedule_triggered: bool = False,
    ):
        self.scheduled_ready_at = scheduled_ready_at
        self.schedule_target_temp = schedule_target_temp
        self._schedule_triggered = schedule_triggered
        self.computed_heat_rate = heat_rate
        self.computed_cool_rate = cool_rate
        self.heat_rate_buckets = [heat_rate, heat_rate, heat_rate] if heat_rate else [None, None, None]
        self._session_scalar = 1.0
        self._session_fresh_buckets = {0, 1, 2}
        self.prediction_bias = 1.0
        self._last_data = {
            "water_temperature": str(water_temp),
            "target_temperature": str(target_temp),
            "device_heat_perhour": device_heat_perhour,
        }


class MockConfigEntry:
    """Minimal config entry that carries schedule options."""

    def __init__(self, lookahead_days: int = DEFAULT_SCHEDULE_LOOKAHEAD_DAYS):
        self.options = {CONF_SCHEDULE_LOOKAHEAD_DAYS: lookahead_days}


# ── Minimal sensor stub ───────────────────────────────────────────────────────

class _HeatScheduleStub:
    """Stand-in that lets us call MSpaHeatScheduleSensor methods without HA MRO.

    native_value calls self._schedule_data(), so we delegate that to the real
    class method (which only reads self.coordinator and self._config_entry).
    """

    def __init__(self, coordinator: MockCoordinator, config_entry: MockConfigEntry):
        self.coordinator = coordinator
        self._config_entry = config_entry

    _schedule_data = MSpaHeatScheduleSensor._schedule_data


# ── dt_util fixture ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def freeze_time():
    """Patch dt_util inside sensor.py to a fixed point in time."""
    with patch("custom_components.mspa.sensor.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.as_utc.side_effect = (
            lambda dt: dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        )
        mock_dt.as_local.side_effect = lambda dt: dt  # treat UTC == local in tests
        mock_dt.now.return_value = _NOW_LOCAL
        yield mock_dt


# ── Helpers ───────────────────────────────────────────────────────────────────

def _val(
    coordinator: MockCoordinator,
    config_entry: "MockConfigEntry | None" = None,
) -> str:
    """Return MSpaHeatScheduleSensor.native_value for the given coordinator."""
    entry = config_entry or MockConfigEntry()
    stub = _HeatScheduleStub(coordinator, entry)
    return MSpaHeatScheduleSensor.native_value.fget(stub)


def _schedule_data(
    coordinator: MockCoordinator,
    config_entry: "MockConfigEntry | None" = None,
):
    """Return MSpaHeatScheduleSensor._schedule_data() result directly."""
    entry = config_entry or MockConfigEntry()
    stub = _HeatScheduleStub(coordinator, entry)
    return MSpaHeatScheduleSensor._schedule_data(stub)


# ═══════════════════════════════════════════════════════════════════════════════
# "Not scheduled" — all paths that produce this state
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotScheduled:

    def test_no_scheduled_ready_at(self):
        coord = MockCoordinator(scheduled_ready_at=None)
        assert _val(coord) == "Not scheduled"

    def test_scheduled_time_in_the_past(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC - timedelta(hours=1),
            schedule_target_temp=39.0,
            water_temp=35.0,
        )
        assert _val(coord) == "Not scheduled"

    def test_scheduled_time_exactly_now_is_past(self):
        # target_utc <= now_utc is the guard; "at exactly now" counts as past
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC,
            schedule_target_temp=39.0,
            water_temp=35.0,
        )
        assert _val(coord) == "Not scheduled"

    def test_beyond_default_lookahead(self):
        # Default lookahead is DEFAULT_SCHEDULE_LOOKAHEAD_DAYS (5).
        # 6 days ahead exceeds it.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=6),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        assert _val(coord) == "Not scheduled"

    def test_beyond_custom_lookahead(self):
        # Custom lookahead of 2 days; target 3 days away → beyond.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=3),
            schedule_target_temp=39.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        entry = MockConfigEntry(lookahead_days=2)
        assert _val(coord, entry) == "Not scheduled"

    def test_within_custom_lookahead_not_not_scheduled(self):
        # Same target but larger lookahead → should proceed past the guard.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=3),
            schedule_target_temp=39.0,
            water_temp=37.0,  # 2°C gap → needs heating
            heat_rate=2.0,
        )
        entry = MockConfigEntry(lookahead_days=5)
        assert _val(coord, entry) != "Not scheduled"

    def test_no_water_temperature(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
        )
        coord._last_data["water_temperature"] = None
        assert _val(coord) == "Not scheduled"

    def test_invalid_water_temperature(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
        )
        coord._last_data["water_temperature"] = "bad_value"
        assert _val(coord) == "Not scheduled"

    def test_no_rate_data_when_heating_needed(self):
        # Water far from target; no rate data → _segmented_heating_minutes returns None.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=6),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=None,
            device_heat_perhour=0,
        )
        assert _val(coord) == "Not scheduled"

    def test_no_rate_data_when_cooling_needed(self):
        # Water above target; no cool_rate → returns None.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=6),
            schedule_target_temp=35.0,
            water_temp=40.0,
            cool_rate=None,
        )
        assert _val(coord) == "Not scheduled"


# ═══════════════════════════════════════════════════════════════════════════════
# "Ready" — water already within 1°C of the schedule target
# ═══════════════════════════════════════════════════════════════════════════════

class TestReady:

    def test_water_exactly_at_target(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=39.0,
        )
        assert _val(coord) == "Ready"

    def test_water_1_degree_below_target_boundary(self):
        # |39.0 - 38.0| == 1.0 → exactly on the boundary (≤ 1.0) → Ready
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=38.0,
        )
        assert _val(coord) == "Ready"

    def test_water_just_below_target(self):
        # |39.0 - 38.5| = 0.5 ≤ 1.0 → Ready
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=38.5,
        )
        assert _val(coord) == "Ready"

    def test_water_just_above_target(self):
        # Water slightly above target (cooling direction, small delta) → Ready
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=39.8,
        )
        assert _val(coord) == "Ready"

    def test_water_1_degree_above_target_boundary(self):
        # |39.0 - 40.0| == 1.0 → exactly on boundary → Ready
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=40.0,
        )
        assert _val(coord) == "Ready"

    def test_ready_does_not_need_rate_data(self):
        # Ready path is reached before the rate-data check, so no rate needed.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=38.5,
            heat_rate=None,
        )
        assert _val(coord) == "Ready"

    def test_water_beyond_1_degree_not_ready(self):
        # |39.0 - 37.9| = 1.1 > 1.0 → NOT Ready
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=37.9,
            heat_rate=2.0,
        )
        val = _val(coord)
        assert val != "Ready"
        assert val != "Not scheduled"


# ═══════════════════════════════════════════════════════════════════════════════
# "Start at HH:MM [+Nd]" — start time in the future
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartAt:

    def test_start_time_same_day(self):
        # Cold start: 20°C → 40°C at 2°C/h = 600 min = 10h
        # Target 12h from now → start 2h from now → same day → "Start at HH:MM"
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        val = _val(coord)
        assert re.match(r"^Start at \d{2}:\d{2}$", val), f"Expected 'Start at HH:MM', got {val!r}"

    def test_start_time_next_day(self):
        # Cold start: 600 min needed, target 3 days away → start ~2.5 days away → "+2d"
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=3),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        val = _val(coord)
        assert re.match(r"^Start at \d{2}:\d{2} \+\d+d$", val), (
            f"Expected 'Start at HH:MM +Nd', got {val!r}"
        )

    def test_start_time_format_no_suffix_when_same_day(self):
        # Target 3h away, small heating need (37.5°C → 39°C, 1.5°C at 2°C/h = 45 min)
        # Start = target - 45min = NOW+3h-45min = NOW+2h15min → same day → no suffix
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=3),
            schedule_target_temp=39.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        val = _val(coord)
        assert val.startswith("Start at ")
        assert "+d" not in val

    def test_start_time_computed_correctly(self):
        # 37.5°C → 39.0°C: delta = 1.5°C, rate = 2.0°C/h → 45 min
        # Target = NOW + 4h.  start = NOW + 4h - 45min = NOW + 3h15min
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        result = _schedule_data(coord)
        assert isinstance(result, tuple)
        target_utc, target_temp, start_at_utc = result
        expected_start = _NOW_UTC + timedelta(hours=4) - timedelta(minutes=45)
        assert abs((start_at_utc - expected_start).total_seconds()) < 1, (
            f"start_at {start_at_utc} != expected {expected_start}"
        )

    def test_prediction_bias_applied(self):
        # bias=1.2 means 20% longer than raw rate estimate
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        coord.prediction_bias = 1.2
        result = _schedule_data(coord)
        assert isinstance(result, tuple)
        _, _, start_at_biased = result

        # Unbased: 45 min, with bias=1.2: 54 min
        coord.prediction_bias = 1.0
        result_unbased = _schedule_data(coord)
        _, _, start_at_unbased = result_unbased

        # Biased start is earlier (more lead time)
        assert start_at_biased < start_at_unbased

    def test_segmented_rates_cold_start(self):
        # 20°C → 40°C crosses all three buckets: [20→30], [30→37], [37→40]
        # Each bucket rate = 2.0°C/h
        # Times: 10/2*60=300, 7/2*60=210, 3/2*60=90 → total 600 min
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=15),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        result = _schedule_data(coord)
        assert isinstance(result, tuple)
        _, _, start_at = result
        expected_start = _NOW_UTC + timedelta(hours=15) - timedelta(minutes=600)
        assert abs((start_at - expected_start).total_seconds()) < 1


# ═══════════════════════════════════════════════════════════════════════════════
# "Start now" — scheduler should be running
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartNow:

    def test_now_past_start_time(self):
        # Water 37.5°C → 39.0°C: 45 min needed.
        # Target = NOW + 20min → start = NOW - 25min → now > start → "Start now"
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=20),
            schedule_target_temp=39.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        assert _val(coord) == "Start now"

    def test_now_exactly_at_start_time(self):
        # 20°C → 40°C: 600 min at 2°C/h.  Target exactly 600 min away.
        # start_at = target - 600 min = NOW → now >= start_at → "Start now"
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=600),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        assert _val(coord) == "Start now"

    def test_target_very_soon_with_no_lead_time_needed(self):
        # Water within 0.5°C of target → _compute_heating_minutes returns 0
        # → start_at == target_utc.  Target is 1 second in the future.
        # In sensor code: _segmented_heating_minutes(38.8, 39.0) → from_temp < to_temp
        # delta = 0.2°C < 0.5°C → … actually sensor uses _segmented_heating_minutes,
        # not coordinator's _compute_heating_minutes.  |38.8 - 39.0| = 0.2 ≤ 1.0 → "Ready".
        # So this scenario gives "Ready", not "Start now".
        # Test that: |water - target| ≤ 1.0 → "Ready".
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(seconds=1),
            schedule_target_temp=39.0,
            water_temp=38.8,
        )
        assert _val(coord) == "Ready"

    def test_start_now_when_very_close_to_target_requires_heating(self):
        # Water 37.5°C, target 40°C: |delta|=2.5 > 1.0 → needs heating.
        # 2.5°C at 2°C/h = 75 min.  Target = NOW + 30min → start = NOW - 45min → "Start now".
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=30),
            schedule_target_temp=40.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        assert _val(coord) == "Start now"

    def test_schedule_data_returns_tuple_when_start_now(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=20),
            schedule_target_temp=39.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        result = _schedule_data(coord)
        assert isinstance(result, tuple), "Expected tuple for Start now scenario"
        target_utc, target_temp, start_at_utc = result
        assert start_at_utc <= _NOW_UTC  # start is already in the past
        assert target_utc > _NOW_UTC     # target is still in the future


# ═══════════════════════════════════════════════════════════════════════════════
# Extra_state_attributes — only returned when a start time exists
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtraStateAttributes:

    def _attrs(self, coordinator, config_entry=None):
        entry = config_entry or MockConfigEntry()
        stub = _HeatScheduleStub(coordinator, entry)
        return MSpaHeatScheduleSensor.extra_state_attributes.fget(stub)

    def test_attrs_empty_when_not_scheduled(self):
        coord = MockCoordinator(scheduled_ready_at=None)
        assert self._attrs(coord) == {}

    def test_attrs_empty_when_ready(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=38.5,
        )
        assert self._attrs(coord) == {}

    def test_attrs_present_when_start_at(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        attrs = self._attrs(coord)
        assert "target_time" in attrs
        assert "start_at" in attrs
        assert "target_temperature" in attrs
        assert attrs["target_temperature"] == 40.0

    def test_attrs_present_when_start_now(self):
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=20),
            schedule_target_temp=39.0,
            water_temp=37.5,
            heat_rate=2.0,
        )
        attrs = self._attrs(coord)
        assert "target_time" in attrs
        assert "start_at" in attrs


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario-level walkthroughs (state transitions over time)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenarios:

    def test_cold_start_early_phase(self):
        """6 days before target: shows 'Start at HH:MM +Nd' (within 5-day lookahead)."""
        # With default 5-day lookahead, 4 days out is in-window.
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=4),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        val = _val(coord)
        # 600 min = 10h lead needed.  target is 4 days away.  start is 4d-10h out.
        assert re.match(r"^Start at \d{2}:\d{2} \+\d+d$", val), f"got {val!r}"

    def test_cold_start_beyond_lookahead_is_not_scheduled(self):
        """6 days before target with 5-day lookahead: 'Not scheduled'."""
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=6),
            schedule_target_temp=40.0,
            water_temp=20.0,
            heat_rate=2.0,
        )
        assert _val(coord) == "Not scheduled"

    def test_energy_saving_when_spa_is_warm_initially(self):
        """Schedule set while spa is at 39°C — target is also 39°C → 'Ready'."""
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=20),
            schedule_target_temp=39.0,
            water_temp=39.0,
        )
        assert _val(coord) == "Ready"

    def test_energy_saving_after_overnight_cooling(self):
        """Water cooled to 35°C, target 39°C, 4°C gap at 2°C/h = 120 min.
        Target in 8h → start = 6h from now → 'Start at HH:MM'."""
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=8),
            schedule_target_temp=39.0,
            water_temp=35.0,
            heat_rate=2.0,
        )
        val = _val(coord)
        assert re.match(r"^Start at \d{2}:\d{2}$", val), f"got {val!r}"

    def test_energy_saving_heater_running(self):
        """Now is past computed start time: 'Start now'."""
        # 35°C → 39°C = 4°C, 2°C/h = 120 min.  Target in 1h → start 1h ago → "Start now"
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=1),
            schedule_target_temp=39.0,
            water_temp=35.0,
            heat_rate=2.0,
        )
        assert _val(coord) == "Start now"

    def test_warm_day_epsilon_near_target(self):
        """Water 38.8°C vs 39°C target: |delta|=0.2 ≤ 1.0 → 'Ready'."""
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0,
            water_temp=38.8,
        )
        assert _val(coord) == "Ready"

    def test_triggered_latch_holds_start_now_while_heating(self):
        """After trigger fires, sensor holds 'Start now' even if recalculated start drifts future.

        Production scenario: trigger fired at 12:08 (ETA=X min).  Heater turns on,
        spa starts warming, rate data refreshes → shorter ETA → start_at moves to
        12:33 (now in the future) → without the latch the sensor shows 'Start at 12:33'
        even though conditioning is already running.  With _schedule_triggered=True the
        sensor must hold 'Start now' until water reaches schedule_target_temp.
        """
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=3),  # ready at 15:00
            schedule_target_temp=39.0,
            water_temp=36.0,        # well below target — still heating
            heat_rate=2.0,
            schedule_triggered=True,
        )
        assert _val(coord) == "Heating", (
            "Triggered latch must show 'Heating' while water is below schedule target"
        )

    def test_triggered_latch_releases_when_spa_reaches_target(self):
        """Once water reaches schedule_target_temp, the latch releases and 'Ready' shows."""
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=1),
            schedule_target_temp=39.0,
            water_temp=38.8,        # within 0.5°C of target — latch releases
            heat_rate=2.0,
            schedule_triggered=True,
        )
        # _schedule_data returns "ready" (|delta| ≤ 1.0), so _compute_schedule_value → "Ready"
        assert _val(coord) == "Ready"

    def test_triggered_false_still_recalculates_normally(self):
        """Without a trigger, the sensor recalculates as normal (no latch)."""
        coord = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=3),
            schedule_target_temp=39.0,
            water_temp=36.0,
            heat_rate=2.0,
            schedule_triggered=False,
        )
        val = _val(coord)
        # 3°C gap at 2°C/h = 90 min.  Target in 180 min → start 90 min from now → future
        assert re.match(r"^Start at \d{2}:\d{2}$", val), f"got {val!r}"
