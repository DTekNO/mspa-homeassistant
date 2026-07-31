"""Scenario-based tests for MSpa Ready at and Heat Schedule sensors.

Each test covers a named use pattern (A–H) or a critical latch-state invariant.
Ready at tests call _compute_ready_at_value directly; Heat Schedule tests go
through the sensor stub so the freeze_time fixture's dt_util patch applies.

Run with: python -m pytest tests/test_schedule_scenarios.py -v
"""
import re
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from custom_components.mspa.sensor import (
    _compute_ready_at_value,
    MSpaReadinessSensor,
    MSpaHeatScheduleSensor,
)
from custom_components.mspa.const import (
    CONF_SCHEDULE_LOOKAHEAD_DAYS,
    DEFAULT_SCHEDULE_LOOKAHEAD_DAYS,
)

_NOW_UTC   = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
_NOW_LOCAL = _NOW_UTC

_NEAR_TARGET_DEACTIVATE = 0.25
_NEAR_TARGET_ACTIVATE   = 0.5


# ── Shared mock coordinator ───────────────────────────────────────────────────

class MockCoordinator:
    def __init__(
        self,
        *,
        water_temp: float = 38.0,
        target_temp: float = 40.0,
        near_target: bool = False,
        ready_latched: bool = False,
        heat_rate: "float | None" = 2.0,
        cool_rate: "float | None" = None,
        anchor_offset_minutes: float = -30.0,
        device_heat_perhour: int = 0,
        is_online: bool = True,
        last_update_success: bool = True,
        heater: "str | None" = None,
        scheduled_ready_at: "datetime | None" = None,
        schedule_target_temp: float = 40.0,
        schedule_triggered: bool = False,
    ):
        self.near_target = near_target
        self.ready_latched = ready_latched
        self._schedule_triggered = schedule_triggered
        self.computed_heat_rate = heat_rate
        self.computed_cool_rate = cool_rate
        self.prediction_bias = 1.0
        self._session_scalar = 1.0
        self._session_fresh_buckets = {0, 1, 2}
        self.ambient_temp = None
        self.ambient_baseline = None
        self.last_update_success = last_update_success
        self.scheduled_ready_at = scheduled_ready_at
        self.schedule_target_temp = schedule_target_temp

        self.temp_anchor_time = datetime.now(timezone.utc) + timedelta(minutes=anchor_offset_minutes)
        self.temp_anchor_temp = water_temp
        self.temp_anchor_target = target_temp
        self.heat_rate_buckets = [heat_rate, heat_rate, heat_rate] if heat_rate else [None, None, None]

        _heater = heater if heater is not None else ("on" if target_temp > water_temp else "off")
        self._last_data = {
            "water_temperature": str(water_temp),
            "target_temperature": str(target_temp),
            "device_heat_perhour": device_heat_perhour,
            "is_online": is_online,
            "heater": _heater,
        }


class MockConfigEntry:
    def __init__(self, lookahead_days: int = DEFAULT_SCHEDULE_LOOKAHEAD_DAYS):
        self.options = {CONF_SCHEDULE_LOOKAHEAD_DAYS: lookahead_days}


# ── Sensor stubs ──────────────────────────────────────────────────────────────

class _HeatScheduleStub:
    def __init__(self, coordinator, config_entry):
        self.coordinator = coordinator
        self._config_entry = config_entry

    _schedule_data = MSpaHeatScheduleSensor._schedule_data
    extra_state_attributes = MSpaHeatScheduleSensor.extra_state_attributes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ready_at(c) -> "str | None":
    return _compute_ready_at_value(c)


def _heat_schedule(c, entry=None) -> str:
    stub = _HeatScheduleStub(c, entry or MockConfigEntry())
    return MSpaHeatScheduleSensor.native_value.fget(stub)


def _heat_schedule_attrs(c, entry=None) -> dict:
    stub = _HeatScheduleStub(c, entry or MockConfigEntry())
    return MSpaHeatScheduleSensor.extra_state_attributes.fget(stub)


def _apply_temp_update(c, new_temp: float, new_target: float) -> None:
    """Replicate coordinator._async_update_data near_target / latch block."""
    c.temp_anchor_temp = new_temp
    c.temp_anchor_target = new_target
    c._last_data["water_temperature"] = str(new_temp)
    c._last_data["target_temperature"] = str(new_target)
    delta = abs(new_target - new_temp)
    if delta < _NEAR_TARGET_DEACTIVATE:
        if not c.near_target:
            c.ready_latched = True
        c.near_target = True
    elif delta >= _NEAR_TARGET_ACTIVATE:
        c.near_target = False


# ── dt_util fixture (required by Heat Schedule sensor internals) ──────────────

@pytest.fixture(autouse=True)
def freeze_time():
    with patch("custom_components.mspa.sensor.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.as_utc.side_effect = (
            lambda dt: dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        )
        mock_dt.as_local.side_effect = lambda dt: dt
        mock_dt.now.return_value = _NOW_LOCAL
        yield mock_dt


# ═══════════════════════════════════════════════════════════════════════════════
# LATCH STATE MACHINE — three critical invariants
# ═══════════════════════════════════════════════════════════════════════════════

class TestLatchMachine:

    def test_latch_not_re_set_when_already_near_target(self):
        """Poll while near_target=True must NOT re-set ready_latched.

        Root fix for 'spa stays warm between sessions': user sets new schedule
        (latch resets), but coordinator was re-latching every 60 s because the
        spa was still at target temperature.
        """
        c = MockCoordinator(near_target=True, ready_latched=False)
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is False

    def test_latch_fires_on_re_entry_after_cooling(self):
        """After the spa cools and re-heats, latch fires again on the False→True transition."""
        c = MockCoordinator(near_target=True, ready_latched=False)
        _apply_temp_update(c, new_temp=39.5, new_target=40.0)  # 0.5°C gap → exit near_target
        assert c.near_target is False
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)  # return
        assert c.near_target is True
        assert c.ready_latched is True

    def test_schedule_reset_survives_continued_near_target_polls(self):
        """New schedule resets latch; several polls with spa still warm must not re-latch."""
        c = MockCoordinator(near_target=True, ready_latched=True)
        c.ready_latched = False   # schedule change resets latch
        for _ in range(5):
            _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert c.ready_latched is False


# ═══════════════════════════════════════════════════════════════════════════════
# USE PATTERNS A–H — Ready at sensor
# ═══════════════════════════════════════════════════════════════════════════════

class TestReadyAtPatterns:
    """One test per use-pattern row.  _compute_ready_at_value uses datetime.now()
    internally so 'future' schedules are set relative to the actual current time.
    """

    def test_a_cold_start_shows_eta_then_latches_ready(self):
        """A: Spa at 20°C, heater on, no schedule → ETA shown → reaches target → Ready."""
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=20.0, target_temp=40.0,
            heat_rate=2.0, heater="on",
        )
        val = _ready_at(c)
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert _ready_at(c) == "Ready"

    def test_b_new_schedule_resets_latch_cooling_shows_none(self):
        """B: After new schedule, latch resets; spa still warm (cooling direction) → None."""
        c = MockCoordinator(
            ready_latched=False, near_target=False,
            water_temp=40.0, target_temp=35.0,
            heater="off",
        )
        assert _ready_at(c) is None

    def test_c_manual_heat_no_schedule(self):
        """C: Manual heater on, no schedule → ETA → latches Ready on arrival."""
        c = MockCoordinator(
            ready_latched=False, near_target=False,
            water_temp=35.0, target_temp=40.0,
            heat_rate=2.0, heater="on",
        )
        val = _ready_at(c)
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)
        _apply_temp_update(c, new_temp=40.0, new_target=40.0)
        assert _ready_at(c) == "Ready"

    def test_d_thermostat_lowered_while_latched_keeps_ready(self):
        """D: Thermostat lowered while spa is at temp — Ready must persist."""
        c = MockCoordinator(ready_latched=True, near_target=True,
                            water_temp=40.0, target_temp=40.0)
        _apply_temp_update(c, new_temp=40.0, new_target=38.0)   # exits near_target (2°C gap)
        assert c.near_target is False
        assert c.ready_latched is True
        assert _ready_at(c) == "Ready"

    def test_e_heater_physically_off_below_setpoint_shows_none(self):
        """E: Heater off (spa idle), water below setpoint — no stale prediction shown."""
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=25.0, target_temp=40.0,
            heat_rate=2.0, heater="off",
        )
        assert _ready_at(c) is None

    def test_e_heater_on_after_trigger_shows_prediction(self):
        """E: Once coordinator triggers heater on, ETA appears immediately."""
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=25.0, target_temp=40.0,
            heat_rate=2.0, heater="on",
        )
        val = _ready_at(c)
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)

    def test_f_schedule_target_higher_than_thermostat_shows_schedule_time(self):
        """F: Spa at lower setpoint (35°C), schedule targeting 40°C — shows scheduled time."""
        future = datetime.now(timezone.utc) + timedelta(hours=12)
        c = MockCoordinator(
            near_target=True, ready_latched=False,
            water_temp=35.0, target_temp=35.0,
            heat_rate=2.0, heater="on",
            scheduled_ready_at=future, schedule_target_temp=40.0,
        )
        val = _ready_at(c)
        # Schedule pending context drives display (not thermostat-relative near_target)
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)

    def test_g_warm_day_spa_at_sched_temp_shows_ready(self):
        """G: Spa at sched_temp on warm day (setpoint 36°C, sched target 38°C) → Ready."""
        future = datetime.now(timezone.utc) + timedelta(hours=20)
        c = MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=38.0, target_temp=36.0,
            heater="off",
            scheduled_ready_at=future, schedule_target_temp=38.0,
        )
        assert _ready_at(c) == "Ready"

    def test_h_stale_latch_with_higher_schedule_shows_schedule_time(self):
        """H: Stale latch from interim setpoint, higher schedule pending — time shown.

        Production bug: water=35.5°C, sched_target=39°C, ready_latched=True.
        Sensor incorrectly showed 'Ready' while the scheduler showed 'Start at 12:50'.
        """
        future = datetime.now(timezone.utc) + timedelta(hours=8)
        c = MockCoordinator(
            near_target=False, ready_latched=True,   # stale latch from 36°C session
            water_temp=35.5, target_temp=36.0,
            heat_rate=2.0, heater="on",
            scheduled_ready_at=future, schedule_target_temp=39.0,
        )
        val = _ready_at(c)
        # Schedule-pending context fires first; water 35.5 is not near sched_temp 39.0
        assert val is not None and re.match(r"^\d{2}:\d{2}", val)
        assert val != "Ready"


# ═══════════════════════════════════════════════════════════════════════════════
# HEAT SCHEDULE SENSOR — key display states
# (uses _NOW_UTC + timedelta because dt_util is patched by freeze_time)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeatScheduleDisplay:

    def test_no_schedule_shows_not_scheduled(self):
        c = MockCoordinator(scheduled_ready_at=None)
        assert _heat_schedule(c) == "Not scheduled"

    def test_heat_schedule_and_ready_at_both_show_ready_when_triggered(self):
        """Heat Schedule now uses _compute_ready_at_value as single source of truth,
        so both sensors show 'Ready' at the same time — never one hour apart.

        The SCHEDULE_PENDING near_sched path (spa already at target temperature
        before the trigger fires) is covered by test_g_warm_day_spa_at_sched_temp_shows_ready
        in TestReadyAtPatterns; it cannot be exercised through _heat_schedule because
        _compute_ready_at_value uses datetime.now() while _schedule_data uses dt_util.utcnow().
        """
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=1),
            schedule_target_temp=39.0, water_temp=38.8,
            schedule_triggered=True,
        )
        assert _heat_schedule(c) == "Ready"
        assert _ready_at(c) == "Ready"

    def test_heat_schedule_no_longer_has_1c_ready_shortcut(self):
        """Previously Heat Schedule had its own abs(water - sched_temp) <= 1.0 shortcut.
        After the fix it delegates to _compute_ready_at_value, so a 1°C gap before
        the trigger fires shows a start time, not Ready."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
            schedule_target_temp=39.0, water_temp=38.0,  # 1.0°C gap — old threshold
            schedule_triggered=False,
        )
        val = _heat_schedule(c)
        assert val != "Ready"
        assert re.match(r"^Start at \d{2}:\d{2}", val), f"expected 'Start at HH:MM', got {val!r}"

    def test_cold_start_shows_start_at(self):
        """20°C → 40°C at 2°C/h = 600 min; target 12h away → 'Start at HH:MM +Nd'."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0,
        )
        val = _heat_schedule(c)
        assert re.match(r"^Start at \d{2}:\d{2}", val), f"got {val!r}"

    def test_start_time_past_shows_start_now(self):
        """37.5°C → 39°C = 45 min; target in 20 min → start was 25 min ago → 'Start now'."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=20),
            schedule_target_temp=39.0, water_temp=37.5, heat_rate=2.0,
        )
        assert _heat_schedule(c) == "Start now"

    def test_triggered_shows_heating(self):
        """After trigger fires, sensor holds 'Heating' while water is below target."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=3),
            schedule_target_temp=39.0, water_temp=36.0,
            heat_rate=2.0, schedule_triggered=True,
        )
        assert _heat_schedule(c) == "Heating"

    def test_triggered_releases_to_ready_at_target(self):
        """Water within 1°C of target — triggered latch releases and shows Ready."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=1),
            schedule_target_temp=39.0, water_temp=38.8,
            schedule_triggered=True,
        )
        assert _heat_schedule(c) == "Ready"

    def test_beyond_lookahead_confirms_schedule_exists(self):
        """A far-out schedule must not read 'Not scheduled'.

        'Not scheduled' means "you forgot to set one" — the opposite situation.
        Beyond the horizon the sensor confirms a schedule exists without
        projecting a start time that far ahead.
        """
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=6),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0,
        )
        val = _heat_schedule(c)
        assert val != "Not scheduled"
        assert re.match(r"^Scheduled( \+\d+d)?$", val), f"got {val!r}"

    def test_weeks_ahead_still_confirms_schedule(self):
        """Planning two or three weeks out is normal and must be visible."""
        for days in (14, 21):
            c = MockCoordinator(
                scheduled_ready_at=_NOW_UTC + timedelta(days=days),
                schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0,
            )
            assert _heat_schedule(c).startswith("Scheduled")

    def test_no_schedule_still_reads_not_scheduled(self):
        """The distinction that matters: nothing set is different from far off."""
        assert _heat_schedule(MockCoordinator(scheduled_ready_at=None)) == "Not scheduled"

    def test_beyond_lookahead_attributes_expose_target_without_start(self):
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=21),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0,
        )
        attrs = _heat_schedule_attrs(c)
        assert attrs["start_at"] is None
        assert attrs["target_temperature"] == 40.0
        assert attrs["target_time"] is not None

    def test_beyond_lookahead_does_not_raise_on_state_logging(self):
        """_log_schedule_change unpacks the result tuple — the 2-tuple must not break it."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=21),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0,
        )
        stub = _HeatScheduleStub(c, MockConfigEntry())
        # native_value calls _log_schedule_change on every state transition.
        assert MSpaHeatScheduleSensor.native_value.fget(stub).startswith("Scheduled")

    def test_at_target_beats_lookahead_so_sensors_agree(self):
        """Readiness is evaluated before the horizon check, so a far-off schedule
        with the spa already at temperature does not disagree with Ready at."""
        c = MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(days=21),
            schedule_target_temp=39.0, water_temp=39.0,
            near_target=True, ready_latched=True,
        )
        assert _heat_schedule(c) == "Ready"


# ═══════════════════════════════════════════════════════════════════════════════
# READY AT ETA SLEW — corrections land as bounded ramps, not jumps
# ═══════════════════════════════════════════════════════════════════════════════

def _readiness_sensor(coordinator) -> MSpaReadinessSensor:
    e = object.__new__(MSpaReadinessSensor)
    e.coordinator = coordinator
    e._eta_display = None
    e._eta_wall = None
    return e


class TestEtaSlew:
    """The raw ETA corrects in lumps at each temperature crossing and creeps
    +1 min/min while stale.  The displayed ETA must follow at a bounded rate
    (3 min per wall minute), snapping only for genuine replans (>30 min)."""

    _BASE = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    def test_first_eta_is_taken_verbatim(self):
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        assert e._slew_eta(eta, now_utc=self._BASE) == eta

    def test_small_lump_is_ramped_at_capped_rate(self):
        """A +13 min correction (this morning's typical lump) over one poll
        minute moves the display by at most 3 minutes."""
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        raw = eta + timedelta(minutes=13)
        shown = e._slew_eta(raw, now_utc=self._BASE + timedelta(minutes=1))
        assert shown == eta + timedelta(minutes=3)

    def test_lump_fully_repaid_over_successive_polls(self):
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        raw = eta + timedelta(minutes=13)
        now = self._BASE
        for _ in range(5):                      # 5 polls, 1 min apart
            now += timedelta(minutes=1)
            shown = e._slew_eta(raw, now_utc=now)
        assert shown == raw                     # 3+3+3+3+1 = 13

    def test_stale_creep_passes_through_unchanged(self):
        """+1 min of ETA per 1 min of wall clock is under the cap — the
        honest 'heating slower than predicted' drift is not distorted."""
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        shown = e._slew_eta(eta + timedelta(minutes=1),
                            now_utc=self._BASE + timedelta(minutes=1))
        assert shown == eta + timedelta(minutes=1)

    def test_replan_snaps_immediately(self):
        """A correction beyond 30 min is a schedule/setpoint change."""
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        raw = eta + timedelta(minutes=45)
        shown = e._slew_eta(raw, now_utc=self._BASE + timedelta(seconds=30))
        assert shown == raw

    def test_earlier_corrections_also_capped(self):
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        raw = eta - timedelta(minutes=10)
        shown = e._slew_eta(raw, now_utc=self._BASE + timedelta(minutes=1))
        assert shown == eta - timedelta(minutes=3)

    def test_non_eta_state_resets_slew(self):
        """Reaching Ready (or any non-ETA regime) clears the slew state so the
        next heating session starts fresh instead of ramping from stale data."""
        c = MockCoordinator(near_target=True, ready_latched=True,
                            water_temp=40.0, target_temp=40.0)
        e = _readiness_sensor(c)
        e._eta_display = self._BASE            # stale leftover
        e._eta_wall = self._BASE
        val = MSpaReadinessSensor.native_value.fget(e)
        assert val == "Ready"
        assert e._eta_display is None
        assert e._eta_wall is None
