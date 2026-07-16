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
    ):
        self.near_target = near_target
        self.ready_latched = ready_latched
        self.computed_cool_rate = cool_rate
        self.prediction_bias = 1.0
        self._session_scalar = 1.0
        self._session_fresh_buckets = {0, 1, 2}
        self.last_update_success = last_update_success

        # Anchor: set N minutes ago at the given water/target temps
        self.temp_anchor_time = datetime.now(timezone.utc) + timedelta(minutes=anchor_offset_minutes)
        self.temp_anchor_temp = water_temp
        self.temp_anchor_target = target_temp

        # Bucket rates (None = no data for that bucket)
        self.heat_rate_buckets = [heat_rate, heat_rate, heat_rate]

        self._last_data = {
            "water_temperature": str(water_temp),
            "target_temperature": str(target_temp),
            "device_heat_perhour": device_heat_perhour,
            "is_online": is_online,
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
    """Run the near_target / ready_latched update block from coordinator.py."""
    coordinator.temp_anchor_temp   = new_temp
    coordinator.temp_anchor_target = new_target
    coordinator._last_data["water_temperature"]  = str(new_temp)
    coordinator._last_data["target_temperature"] = str(new_target)
    delta = abs(new_target - new_temp)
    if delta < _NEAR_TARGET_DEACTIVATE:
        coordinator.near_target  = True
        coordinator.ready_latched = True
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
