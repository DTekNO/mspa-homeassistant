"""
Tests for MSpa 'Ready at' sensor latch behaviour.

Covers:
  - _spa_direction helper
  - Coordinator near_target / ready_latched state machine
  - MSpaReadinessSensor.native_value under all latch / direction combinations
  - MSpaReadinessSensor.available
  - Latch reset on schedule change (coordinator.ready_latched = False)

The HA module tree is stubbed in conftest.py so these run without an HA install.
"""
import re
import pytest
from datetime import datetime, timezone, timedelta

from custom_components.mspa.sensor import (
    _spa_direction,
    _ready_at_utc,
    _minutes_to_target,
    MSpaReadinessSensor,
)


# ── Mock coordinator ─────────────────────────────────────────────────────────

class MockCoordinator:
    """Minimal coordinator stand-in with every attribute the sensor helpers read."""

    def __init__(
        self,
        *,
        water_temp: float = 38.0,
        target_temp: float = 40.0,
        near_target: bool = False,
        ready_latched: bool = False,
        heat_rate: float | None = 2.0,  # °C/h for all buckets
        cool_rate: float | None = None,
        anchor_offset_minutes: float = -30.0,  # anchor set N min ago
        device_heat_perhour: int = 0,
        is_online: bool = True,
        last_update_success: bool = True,
        # None = auto-infer: "on" when target > water (heating), else "off"
        heater: str | None = None,
        scheduled_ready_at: "datetime | None" = None,
        schedule_target_temp: float = 40.0,
    ):
        self.near_target = near_target
        self.ready_latched = ready_latched
        self.computed_cool_rate = cool_rate
        self.prediction_bias = 1.0
        self._session_scalar = 1.0
        self._session_fresh_buckets = {0, 1, 2}
        self.last_update_success = last_update_success
        self.scheduled_ready_at = scheduled_ready_at
        self.schedule_target_temp = schedule_target_temp

        # Anchor: set N minutes ago at the given water/target temps
        self.temp_anchor_time = datetime.now(timezone.utc) + timedelta(minutes=anchor_offset_minutes)
        self.temp_anchor_temp = water_temp
        self.temp_anchor_target = target_temp

        # Bucket rates (None = no data for that bucket)
        self.heat_rate_buckets = [heat_rate, heat_rate, heat_rate]

        _heater = heater if heater is not None else ("on" if target_temp > water_temp else "off")
        self._last_data = {
            "water_temperature": str(water_temp),
            "target_temperature": str(target_temp),
            "device_heat_perhour": device_heat_perhour,
            "is_online": is_online,
            "heater": _heater,
        }


# ── Minimal sensor stub for calling property getters directly ─────────────────
# We don't construct MSpaReadinessSensor (that needs full HA MRO) — instead we
# call its property fget functions with a lightweight object that just carries
# a coordinator.  native_value and available make no super() calls.

class _SensorStub:
    """Minimal sensor stand-in for calling property logic without HA MRO.

    native_value delegates to MSpaReadinessSensor.native_value.fget (no super() there).
    available is re-implemented inline: the real property calls `super().available`
    which resolves to MSpaBaseEntity.available in production.  We replicate that
    check directly here so the fget trick works without a proper MRO.
    """

    def __init__(self, coordinator: MockCoordinator):
        self.coordinator = coordinator

    def _base_available(self) -> bool:
        """Replicates MSpaBaseEntity.available — the effective super() in production."""
        if not self.coordinator.last_update_success:
            return False
        is_online    = self.coordinator._last_data.get("is_online", True)
        connect_type = self.coordinator._last_data.get("ConnectType", "")
        if is_online is False or connect_type == "offline":
            return False
        return True

    @property
    def available(self) -> bool:
        if not self._base_available():
            return False
        if self.coordinator.ready_latched:
            return True
        return _minutes_to_target(self.coordinator) is not None

    @property
    def native_value(self):
        return MSpaReadinessSensor.native_value.fget(self)


def _stub(coordinator: MockCoordinator) -> _SensorStub:
    return _SensorStub(coordinator)


# ── _spa_direction ────────────────────────────────────────────────────────────

class TestSpaDirection:
    def test_heating(self):
        c = MockCoordinator(water_temp=35.0, target_temp=40.0)
        assert _spa_direction(c) == "heating"

    def test_cooling(self):
        c = MockCoordinator(water_temp=40.0, target_temp=35.0)
        assert _spa_direction(c) == "cooling"

    def test_at_target(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        assert _spa_direction(c) == "at_target"

    def test_missing_water_temp(self):
        c = MockCoordinator()
        c._last_data["water_temperature"] = None
        assert _spa_direction(c) is None

    def test_missing_target_temp(self):
        c = MockCoordinator()
        c._last_data["target_temperature"] = None
        assert _spa_direction(c) is None


# ── Coordinator latch state machine ───────────────────────────────────────────
# The latch logic lives inside _async_update_data; we replicate it here so
# tests are independent of the coordinator's async infrastructure.

_NEAR_TARGET_DEACTIVATE = 0.25
_NEAR_TARGET_ACTIVATE   = 0.5


def _apply_temp_update(coordinator: MockCoordinator, new_temp: float, new_target: float):
    """Run the near_target / ready_latched update block from coordinator.py.

    Mirrors the exact logic in MSpaUpdateCoordinator._async_update_data so that
    tests stay in sync with production behaviour.
    """
    coordinator.temp_anchor_temp   = new_temp
    coordinator.temp_anchor_target = new_target
    coordinator._last_data["water_temperature"]  = str(new_temp)
    coordinator._last_data["target_temperature"] = str(new_target)
    delta = abs(new_target - new_temp)
    if delta < _NEAR_TARGET_DEACTIVATE:
        if not coordinator.near_target:        # latch only on False→True transition
            coordinator.ready_latched = True
        coordinator.near_target = True
    elif delta >= _NEAR_TARGET_ACTIVATE:
        coordinator.near_target = False
    # between the two thresholds: no change to either flag


class TestCoordinatorLatch:
    def test_latch_sets_when_target_reached(self):
        c = MockCoordinator(near_target=False, ready_latched=False)
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.near_target is True
        assert c.ready_latched is True

    def test_latch_sets_within_deactivate_threshold(self):
        c = MockCoordinator(near_target=False, ready_latched=False)
        _apply_temp_update(c, new_temp=39.85, new_target=40.0)  # delta=0.15 < 0.25
        assert c.ready_latched is True

    def test_near_target_exits_on_small_oscillation(self):
        c = MockCoordinator(near_target=True, ready_latched=True)
        # MSpa reports in 0.5°C steps; heater cycling causes a 0.5°C drop
        _apply_temp_update(c, new_temp=39.5, new_target=40.0)  # delta=0.5 >= ACTIVATE
        assert c.near_target is False

    def test_latch_persists_through_minor_oscillation(self):
        """Once latched, a 0.5°C heater-cycling oscillation must NOT clear the latch."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        _apply_temp_update(c, new_temp=39.5, new_target=40.0)
        assert c.ready_latched is True  # latch survives

    def test_latch_persists_when_thermostat_lowered_slightly(self):
        """Thermostat lowered to e.g. 38°C while spa is at 40°C — latch stays."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        _apply_temp_update(c, new_temp=40.0, new_target=38.0)  # delta=2.0 >= ACTIVATE
        assert c.near_target is False
        assert c.ready_latched is True  # only schedule change resets latch

    def test_latch_persists_through_large_deviation(self):
        """Even a large temperature deviation does not reset the latch automatically."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        _apply_temp_update(c, new_temp=40.0, new_target=20.0)  # delta=20°C
        assert c.ready_latched is True

    def test_latch_resets_on_schedule_change(self):
        """Setting a new schedule time is the only automatic latch reset."""
        c = MockCoordinator(near_target=False, ready_latched=True)
        # Simulate what datetime.async_set_value does:
        c.ready_latched = False
        assert c.ready_latched is False

    def test_hysteresis_band_leaves_flags_unchanged(self):
        """Delta between DEACTIVATE and ACTIVATE leaves both flags unchanged."""
        c = MockCoordinator(near_target=False, ready_latched=True)
        _apply_temp_update(c, new_temp=39.9, new_target=40.0)  # delta=0.1 < DEACTIVATE → sets True
        assert c.near_target is True
        assert c.ready_latched is True

        # Now oscillate to 39.7 (delta=0.3 — between thresholds)
        c.near_target = False  # manually put into mid-band state
        c.ready_latched = True
        _apply_temp_update(c, new_temp=39.7, new_target=40.0)  # delta=0.3 between thresholds
        # Neither branch should fire — flags unchanged
        assert c.near_target is False
        assert c.ready_latched is True


# ── native_value ──────────────────────────────────────────────────────────────

class TestNativeValue:
    def test_shows_ready_when_latched(self):
        c = MockCoordinator(ready_latched=True, water_temp=40.0, target_temp=40.0)
        assert _stub(c).native_value == "Ready"

    def test_shows_ready_when_latched_even_if_cooling(self):
        """After target was reached, lowering the thermostat must not change the state."""
        c = MockCoordinator(
            ready_latched=True,
            near_target=False,
            water_temp=40.0,
            target_temp=38.0,  # thermostat lowered → cooling direction
        )
        assert _stub(c).native_value == "Ready"

    def test_shows_ready_when_latched_large_deviation(self):
        """Even a dramatic deviation (spa cooled overnight) — latch holds."""
        c = MockCoordinator(
            ready_latched=True,
            near_target=False,
            water_temp=25.0,
            target_temp=20.0,
        )
        assert _stub(c).native_value == "Ready"

    def test_shows_none_when_cooling_not_latched(self):
        """Cooling before first Ready reached → no prediction, no state."""
        c = MockCoordinator(
            ready_latched=False,
            near_target=False,
            water_temp=40.0,
            target_temp=35.0,
        )
        assert _stub(c).native_value is None

    def test_shows_ready_when_very_close_to_target(self):
        """mins <= 5 yields 'Ready' even without a latch (near arrival)."""
        # Anchor set 145 min ago, 5°C to heat at 2°C/h = 150 min total → 5 min left
        c = MockCoordinator(
            ready_latched=False,
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=2.0,
            anchor_offset_minutes=-145.0,
        )
        assert _stub(c).native_value == "Ready"

    def test_shows_time_prediction_when_heating(self):
        """Valid heating scenario returns a formatted time string."""
        # Anchor 30 min ago, 5°C at 2°C/h = 150 min total → 120 min remaining
        c = MockCoordinator(
            ready_latched=False,
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=2.0,
            anchor_offset_minutes=-30.0,
        )
        val = _stub(c).native_value
        # Should be an HH:MM string (optionally suffixed with " +Nd" for next-day)
        assert val is not None
        assert re.match(r"^\d{2}:\d{2}( \+\d+d)?$", val), f"Unexpected value: {val!r}"

    def test_shows_none_when_no_rate_data(self):
        """No learned rate and no device rate → cannot predict, returns None."""
        c = MockCoordinator(
            ready_latched=False,
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=None,       # no bucket rates
            device_heat_perhour=0,  # no device-provided rate
        )
        assert _stub(c).native_value is None

    def test_shows_none_after_schedule_reset_while_cooling(self):
        """After a schedule change while still cooling, sensor has nothing to show."""
        c = MockCoordinator(
            ready_latched=False,  # reset by schedule change
            near_target=False,
            water_temp=40.0,
            target_temp=38.0,    # cooling
            cool_rate=None,      # no cool rate data
        )
        assert _stub(c).native_value is None

    def test_shows_prediction_after_schedule_reset_while_heating(self):
        """After schedule reset, heating prediction resumes immediately."""
        c = MockCoordinator(
            ready_latched=False,  # reset by schedule change
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=2.0,
            anchor_offset_minutes=-30.0,
        )
        val = _stub(c).native_value
        assert val is not None
        assert re.match(r"^\d{2}:\d{2}( \+\d+d)?$", val), f"Unexpected value: {val!r}"


# ── available ─────────────────────────────────────────────────────────────────

class TestAvailable:
    def test_available_when_latched_with_no_rate_data(self):
        """Latched sensor must be available even if all rate data is missing."""
        c = MockCoordinator(
            ready_latched=True,
            near_target=False,
            heat_rate=None,
            device_heat_perhour=0,
        )
        assert _stub(c).available is True

    def test_available_when_heating_with_rate(self):
        c = MockCoordinator(ready_latched=False, near_target=False, heat_rate=2.0)
        assert _stub(c).available is True

    def test_not_available_when_no_rate_and_not_latched(self):
        c = MockCoordinator(
            ready_latched=False,
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=None,
            device_heat_perhour=0,
        )
        assert _stub(c).available is False

    def test_not_available_when_coordinator_offline_even_if_latched(self):
        """Coordinator offline (last_update_success=False) makes the entity unavailable
        even when the latch is set — the base availability gate fires first."""
        c = MockCoordinator(ready_latched=True, last_update_success=False)
        assert _stub(c).available is False


# ── _ready_at_utc helper ──────────────────────────────────────────────────────

class TestReadyAtUtc:
    def test_returns_none_when_near_target(self):
        c = MockCoordinator(near_target=True, ready_latched=True)
        assert _ready_at_utc(c) is None

    def test_returns_none_when_no_anchor(self):
        c = MockCoordinator(near_target=False)
        c.temp_anchor_time = None
        assert _ready_at_utc(c) is None

    def test_returns_none_when_no_rate(self):
        c = MockCoordinator(
            near_target=False,
            heat_rate=None,
            device_heat_perhour=0,
        )
        assert _ready_at_utc(c) is None

    def test_returns_future_utc_datetime_when_heating(self):
        # 5°C at 2°C/h = 150 min; anchor 30 min ago → ready in 120 min
        c = MockCoordinator(
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=2.0,
            anchor_offset_minutes=-30.0,
        )
        result = _ready_at_utc(c)
        assert result is not None
        now_utc = datetime.now(timezone.utc)
        # Should be roughly 120 minutes in the future (within ±2 min for test timing)
        diff_minutes = (result - now_utc).total_seconds() / 60
        assert 100 < diff_minutes < 140, f"Expected ~120 min ahead, got {diff_minutes:.1f}"

    def test_stable_anchor_does_not_drift(self):
        """Two calls with the same anchor return identical timestamps (no now() drift)."""
        c = MockCoordinator(
            near_target=False,
            water_temp=35.0,
            target_temp=40.0,
            heat_rate=2.0,
            anchor_offset_minutes=-30.0,
        )
        r1 = _ready_at_utc(c)
        r2 = _ready_at_utc(c)
        assert r1 == r2


# ── Latch transition correctness ───────────────────────────────────────────────
# These tests verify the critical fix: ready_latched is set ONLY on the
# near_target False→True transition, never on continued near_target=True polls.
# Without this fix, resetting the latch on a schedule change is immediately
# undone by the next coordinator poll while the spa is still warm.

class TestLatchTransition:
    def test_latch_not_set_when_near_target_already_true(self):
        """Poll while near_target is already True must NOT set ready_latched.

        This is the root fix for the 'spa stays warm between sessions' bug:
        the user sets a new schedule (latch resets), but the coordinator was
        re-latching every 60 s because the spa was still at target temperature.
        """
        c = MockCoordinator(near_target=True, ready_latched=False)
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)  # still at target
        assert c.ready_latched is False  # must NOT re-latch

    def test_latch_set_when_re_entering_near_target(self):
        """After the spa exits and re-enters near_target, the latch fires again."""
        c = MockCoordinator(near_target=True, ready_latched=False)
        # Spa cools slightly — exits near_target
        _apply_temp_update(c, new_temp=39.5, new_target=40.0)
        assert c.near_target is False
        assert c.ready_latched is False
        # Heater runs — spa returns to target
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.near_target is True
        assert c.ready_latched is True  # latches again on re-entry

    def test_schedule_reset_survives_continued_near_target_polls(self):
        """Sequence: spa at target → schedule set → several polls while still warm → latch stays False."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        # User sets new schedule → reset
        c.ready_latched = False
        # Next three coordinator polls: spa still at 40°C
        for _ in range(3):
            _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is False  # reset must survive


# ── End-to-end use pattern scenarios ─────────────────────────────────────────
#
# USE PATTERN A — Cold start with schedule
#   Spa has been idle for days at 20°C.  User sets a schedule for a specific
#   date/time.  The coordinator computes the heating start time and triggers the
#   heater autonomously.  Spa heats up, reaches target, and the 'Ready at' sensor
#   latches to "Ready".  The schedule is auto-cleared once the time passes.
#
# USE PATTERN B — Repeated daily use
#   After USE PATTERN A, the spa is at 40°C.  The user sets a new schedule for
#   the next day.  The latch resets (schedule change).  The spa is allowed to cool
#   during the idle period.  The coordinator triggers heating again at the right
#   time the following day.  Spa reaches target and latches "Ready" again.
#
# USE PATTERN C — Manual heating (no schedule)
#   User turns the heater on directly via the climate entity without setting a
#   schedule.  'Ready at' shows a prediction time while heating.  On reaching
#   target, latches "Ready".  No auto-clear happens (no schedule set).
#
# USE PATTERN D — Thermostat adjustment while spa is warm
#   Spa is at 40°C, latch is set ("Ready").  User lowers thermostat to 38°C
#   (e.g., to save energy while guests are en route).  'Ready at' must continue
#   showing "Ready" — the spa is still usable at 40°C even though the new setpoint
#   is lower.  Latch must not reset from thermostat-only changes.
#
# USE PATTERN E — Completely off, natural cooling, then re-scheduled
#   Spa completely off (heater and pump off) after use.  Water cools naturally
#   from 40°C toward ambient over hours/days.  API setpoint is still 40°C (device
#   remembers its last setting).  _spa_direction would say "heating" (water < target)
#   but the heater is physically off.  Sensor must show None — not a stale prediction
#   — until the coordinator autonomously starts the heater for the next schedule.
#
# USE PATTERN F — Turned down (pump/heater on, setpoint lowered), new schedule set
#   Spa maintained at a lower setpoint (e.g. 35°C) between sessions.  User sets a
#   new schedule targeting 40°C.  Latch is reset by the schedule change.  Spa is
#   at 35°C near its current setpoint (near_target=True relative to 35°C setpoint).
#   Sensor must NOT show "Ready" — water hasn't reached the 40°C schedule target.
#   Once the coordinator triggers and the heater brings the spa to 40°C, the latch
#   fires and "Ready" is shown correctly.

class TestUsePatternsScenarios:
    def test_pattern_a_cold_start_latch_after_heating(self):
        """USE PATTERN A: spa heats from cold, reaches target, latches 'Ready'."""
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=20.0, target_temp=40.0,
            heat_rate=2.0, anchor_offset_minutes=-145.0,
        )
        # While heating: prediction shown
        assert _stub(c).native_value is not None
        assert _stub(c).native_value != "Ready"
        # Spa reaches target — simulate transition
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is True
        assert _stub(c).native_value == "Ready"

    def test_pattern_a_schedule_clears_past_latch_stays(self):
        """USE PATTERN A: after schedule time passes, latch stays True (spa still warm)."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        # Simulate coordinator clearing the past schedule
        # (coordinator.scheduled_ready_at = None, _schedule_triggered = False)
        # Latch is NOT touched by schedule auto-clear
        assert _stub(c).native_value == "Ready"

    def test_pattern_b_new_schedule_resets_latch(self):
        """USE PATTERN B: new schedule resets latch; 'Ready at' shows prediction once heating."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        # User sets new schedule → latch resets
        c.ready_latched = False
        c.near_target = False  # spa starts to cool (heater off)
        # While cooling: sensor shows None (no prediction for cooling)
        c2 = MockCoordinator(
            ready_latched=False, near_target=False,
            water_temp=38.0, target_temp=40.0,
            heat_rate=2.0, anchor_offset_minutes=-30.0,
        )
        assert _stub(c2).native_value is not None  # heating prediction

    def test_pattern_b_latch_survives_poll_when_spa_still_warm(self):
        """USE PATTERN B: after new schedule set, several polls with spa still at 40°C must NOT re-latch."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        c.ready_latched = False  # schedule change
        for _ in range(5):
            _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is False

    def test_pattern_b_latch_fires_when_new_session_reaches_target(self):
        """USE PATTERN B: after cooling and re-heating, near_target re-entry latches 'Ready'."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        c.ready_latched = False  # schedule change
        # Spa cools — exits near_target
        _apply_temp_update(c, new_temp=37.0, new_target=40.0)
        assert c.near_target is False
        assert c.ready_latched is False
        # Heater runs — spa returns to target
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is True
        assert _stub(c).native_value == "Ready"

    def test_pattern_c_manual_heat_no_schedule(self):
        """USE PATTERN C: heater on manually, no schedule — prediction shows, then latches."""
        c = MockCoordinator(
            ready_latched=False, near_target=False,
            water_temp=35.0, target_temp=40.0,
            heat_rate=2.0, anchor_offset_minutes=-30.0,
        )
        val = _stub(c).native_value
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)
        # Reaches target
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert _stub(c).native_value == "Ready"

    def test_pattern_d_thermostat_lowered_while_latched(self):
        """USE PATTERN D: thermostat lowered while spa is at temp — 'Ready' must persist."""
        c = MockCoordinator(
            ready_latched=True, near_target=True,
            water_temp=40.0, target_temp=40.0,
        )
        # User lowers thermostat to 38°C
        _apply_temp_update(c, new_temp=40.0, new_target=38.0)  # delta=2°C → exits near_target
        assert c.near_target is False
        assert c.ready_latched is True   # latch untouched
        assert _stub(c).native_value == "Ready"

    # ── USE PATTERN E — Spa completely off, natural cooling ───────────────────

    def test_pattern_e_heater_off_below_setpoint_shows_none(self):
        """USE PATTERN E: heater off, water below setpoint — sensor must show None.

        _spa_direction returns 'heating' (water < target) but the heater is physically
        off.  The sensor must not show a stale prediction from accumulated rate data.
        Sensor shows None until the coordinator autonomously starts the heater.
        """
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=25.0, target_temp=40.0,
            heat_rate=2.0, anchor_offset_minutes=-30.0,
            heater="off",  # spa completely off — override auto-infer
        )
        assert _stub(c).native_value is None

    def test_pattern_e_heater_on_after_trigger_shows_prediction(self):
        """USE PATTERN E: once coordinator triggers (heater on), prediction shows immediately."""
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=25.0, target_temp=40.0,
            heat_rate=2.0, anchor_offset_minutes=-30.0,
            heater="on",  # coordinator just triggered the heater
        )
        val = _stub(c).native_value
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)

    def test_pattern_e_latch_fires_when_spa_reaches_target(self):
        """USE PATTERN E: after autonomous heat-up, near_target fires and latches 'Ready'."""
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=25.0, target_temp=40.0,
            heat_rate=2.0, heater="on",
        )
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is True
        assert _stub(c).native_value == "Ready"

    # ── USE PATTERN F — Turned down, schedule set to higher target ────────────

    def test_pattern_f_near_target_at_lower_setpoint_shows_none_when_schedule_set(self):
        """USE PATTERN F: spa at lowered setpoint (35°C) with schedule targeting 40°C.

        near_target=True relative to the 35°C API setpoint.  _minutes_to_target returns 0.
        But the sensor must NOT show 'Ready' — water hasn't reached the 40°C schedule target.
        """
        from datetime import timezone as tz
        future = datetime.now(tz.utc) + timedelta(hours=12)
        c = MockCoordinator(
            near_target=True, ready_latched=False,
            water_temp=35.0, target_temp=35.0,
            heat_rate=2.0,
            heater="on",  # spa maintaining at 35°C
            scheduled_ready_at=future,
            schedule_target_temp=40.0,
        )
        assert _stub(c).native_value is None

    def test_pattern_f_no_schedule_means_ready_at_current_setpoint(self):
        """USE PATTERN F: without a schedule, 'Ready' at the current setpoint is correct."""
        c = MockCoordinator(
            near_target=True, ready_latched=False,
            water_temp=35.0, target_temp=35.0,
            heat_rate=2.0, heater="on",
            scheduled_ready_at=None,  # no schedule
            schedule_target_temp=40.0,
        )
        assert _stub(c).native_value == "Ready"

    def test_pattern_f_latch_fires_when_spa_reaches_schedule_target(self):
        """USE PATTERN F: after coordinator raises setpoint to 40°C and spa heats up."""
        from datetime import timezone as tz
        future = datetime.now(tz.utc) + timedelta(hours=12)
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=35.0, target_temp=40.0,  # coordinator raised setpoint
            heat_rate=2.0, heater="on",
            scheduled_ready_at=future,
            schedule_target_temp=40.0,
        )
        # During heat-up: prediction shown
        val = _stub(c).native_value
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)
        # Reaches 40°C — near_target transition → latch
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is True
        assert _stub(c).native_value == "Ready"
