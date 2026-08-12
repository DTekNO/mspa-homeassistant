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
        self.ready_latched_temp = water_temp if ready_latched else None
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
        # A fresh stub has nothing held, so _slew_start adopts the live value and
        # every existing scenario sees the unsmoothed start.  Tests that exercise
        # the smoothing reuse one stub across updates.
        self._start_shown = None
        self._start_key = None

    _schedule_data = MSpaHeatScheduleSensor._schedule_data
    extra_state_attributes = MSpaHeatScheduleSensor.extra_state_attributes
    _slew_start = MSpaHeatScheduleSensor._slew_start
    _plan_key = MSpaHeatScheduleSensor._plan_key


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
    e._eta_plan_key = None
    e._eta_closing = False
    return e


class TestEtaSlew:
    """Rate cap on the displayed ETA.

    Cap and snap semantics were both revised after the 2026-08-06 session (see the
    comment above _ETA_SLEW_MIN_PER_MIN): the cap dropped 3 -> 1 min per wall
    minute, and snapping is now decided by _replan_key rather than by magnitude.
    Cap assertions read _eta_display, the unrounded internal position, so display
    rounding does not obscure what the cap did.
    """

    _BASE = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    def test_first_eta_is_taken_verbatim(self):
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        assert e._slew_eta(eta, now_utc=self._BASE) == eta

    def test_lump_is_ramped_at_capped_rate(self):
        """A 13 min correction moves the estimate by at most 1 min per wall minute."""
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        e._slew_eta(eta + timedelta(minutes=13), now_utc=self._BASE + timedelta(minutes=1))
        assert e._eta_display == eta + timedelta(minutes=1)

    def test_lump_fully_repaid_over_successive_polls(self):
        """13 min at 1 min/min takes 13 polls, then stops — no overshoot."""
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        raw = eta + timedelta(minutes=13)
        now = self._BASE
        for _ in range(20):
            now += timedelta(minutes=1)
            e._slew_eta(raw, now_utc=now)
        assert e._eta_display == raw

    def test_earlier_corrections_also_capped(self):
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        e._slew_eta(eta - timedelta(minutes=10), now_utc=self._BASE + timedelta(minutes=1))
        assert e._eta_display == eta - timedelta(minutes=1)

    def test_magnitude_alone_does_not_snap(self):
        """The old rule adopted anything over 30 min wholesale.  Both large jumps in
        the 2026-08-06 session were rate-learning revisions, not user replans, so
        size on its own must no longer bypass the cap."""
        e = _readiness_sensor(MockCoordinator())
        eta = self._BASE + timedelta(hours=3)
        e._slew_eta(eta, now_utc=self._BASE)
        e._slew_eta(eta + timedelta(minutes=45), now_utc=self._BASE + timedelta(seconds=30))
        moved = (e._eta_display - eta).total_seconds() / 60
        assert moved <= 0.5 + 1e-9, f"moved {moved:.1f} min in 30 s"

    def test_non_eta_state_resets_slew(self):
        """Reaching Ready (or any non-ETA regime) clears the slew state so the
        next heating session starts fresh instead of ramping from stale data."""
        c = MockCoordinator(near_target=True, ready_latched=True,
                            water_temp=40.0, target_temp=40.0)
        e = _readiness_sensor(c)
        e._eta_display = self._BASE            # stale leftover
        e._eta_wall = self._BASE
        e._eta_plan_key = ("stale",)
        e._eta_closing = True
        val = MSpaReadinessSensor.native_value.fget(e)
        assert val == "Ready"
        assert e._eta_display is None
        assert e._eta_wall is None
        assert e._eta_plan_key is None
        assert e._eta_closing is False


# ═══════════════════════════════════════════════════════════════════════════════
# LATCH RELEASE ON SETPOINT RAISE
# ═══════════════════════════════════════════════════════════════════════════════

_LATCH_COOL_OFF = 3.0


def _apply_latch_rules(c, new_temp, new_target):
    """Replicate the coordinator's near_target / ready_latched block."""
    c.temp_anchor_temp = new_temp
    c.temp_anchor_target = new_target
    c._last_data["water_temperature"] = str(new_temp)
    c._last_data["target_temperature"] = str(new_target)
    c._last_data["heater"] = "on" if new_target > new_temp else "off"
    delta = abs(new_target - new_temp)
    if delta < _NEAR_TARGET_DEACTIVATE:
        if not c.near_target:
            c.ready_latched = True
            c.ready_latched_temp = new_temp
        c.near_target = True
    elif delta >= _NEAR_TARGET_ACTIVATE:
        c.near_target = False
        if c.ready_latched and (new_target - new_temp) > 2.0:
            c.ready_latched = False
            c.ready_latched_temp = None
    # peak tracking runs regardless of the hysteresis branches
    if c.ready_latched and (c.ready_latched_temp is None
                            or new_temp > c.ready_latched_temp):
        c.ready_latched_temp = new_temp
    if (c.ready_latched and c.ready_latched_temp is not None
            and (c.ready_latched_temp - new_temp) >= _LATCH_COOL_OFF):
        c.ready_latched = False
        c.ready_latched_temp = None


class TestLatchReleaseOnSetpointRaise:
    """Reported 2026-08-03: setpoint moved 40 → 31 → 40 on the climate card.
    At 31/31 the spa latched Ready; raising the setpoint back to 40 left the
    latch set, so Ready at reported 'Ready' with 9 °C still to heat instead of
    recalculating."""

    def test_raising_setpoint_releases_latch_and_shows_eta(self):
        c = MockCoordinator(water_temp=31.0, target_temp=31.0, heat_rate=1.0)
        _apply_latch_rules(c, 31.0, 31.0)          # arrives at setpoint
        assert c.ready_latched is True
        assert _ready_at(c) == "Ready"

        _apply_latch_rules(c, 31.0, 40.0)          # user raises setpoint to 40
        assert c.ready_latched is False, "latch must release for a real heating gap"
        assert c.near_target is False
        val = _ready_at(c)
        assert val != "Ready", "Ready at must recalculate, not stay pinned"
        assert re.match(r"^\d{2}:\d{2}", val), f"expected an ETA, got {val!r}"

    def test_lowering_setpoint_while_warm_keeps_ready(self):
        """The after-a-soak case — deliberate, do not "fix" it.

        You have used the tub, turned the thermostat down to save energy, and
        the water is still hot.  Ready at must keep saying "Ready" so you can
        see that a late-night second dip needs no waiting.  This is why the
        latch is released only for HEATING gaps, never for a lowered setpoint,
        and it is why the display legitimately depends on latch history.

        Reviewed and kept 2026-08-03 after being re-reported as a bug.  See the
        long comment above the FREE CONTEXT block in sensor.py.
        """
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        assert c.ready_latched is True
        _apply_latch_rules(c, 40.0, 36.0)          # thermostat lowered 4 °C
        assert c.ready_latched is True, "water above setpoint is still ready"
        assert _ready_at(c) == "Ready"

    def test_lowering_setpoint_a_long_way_still_keeps_ready(self):
        """Even a large drop keeps Ready — the water is what matters, not the
        size of the setpoint change.  (Reported 2026-08-03: "it doesn't make a
        difference if I lower the temperature by a bigger margin than 2
        degrees" — correct, by design.)"""
        c = MockCoordinator(water_temp=31.5, target_temp=31.5)
        _apply_latch_rules(c, 31.5, 31.5)
        for setpoint in (29.5, 25.0, 20.0):
            _apply_latch_rules(c, 31.5, setpoint)
            assert c.ready_latched is True, f"latch lost at setpoint={setpoint}"
            assert _ready_at(c) == "Ready"

    def test_thermostat_cycling_does_not_flicker_the_latch(self):
        """±0.5–1 °C swings must not release the latch, or Ready would blink
        off and on during normal maintenance cycling."""
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        for water in (39.5, 39.0, 39.4, 39.8, 40.0):
            _apply_latch_rules(c, water, 40.0)
            assert c.ready_latched is True, f"latch lost at water={water}"

    def test_small_setpoint_raise_within_session_threshold_keeps_latch(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        _apply_latch_rules(c, 40.0, 41.5)          # +1.5 °C, under the threshold
        assert c.ready_latched is True

    def test_large_setpoint_raise_releases_even_from_at_target(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        _apply_latch_rules(c, 40.0, 43.0)          # +3 °C, a real session
        assert c.ready_latched is False


class TestLatchCoolOffRelease:
    """The latch advertises "still warm enough to use without waiting".  Without
    a cool-off release it outlives that claim: drop the thermostat to 20 °C with
    the water at 40 °C and two days later the water is 24 °C — still above
    setpoint, so the heating-gap release never fires — and the sensor would
    happily report Ready for a tub nobody wants to get into."""

    def test_ready_withdrawn_once_the_water_has_cooled(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        assert c.ready_latched is True
        _apply_latch_rules(c, 40.0, 20.0)          # after the soak: thermostat down
        assert c.ready_latched is True, "still hot — still ready"
        for water in (39.0, 38.0):                 # cooling, under the threshold
            _apply_latch_rules(c, water, 20.0)
            assert c.ready_latched is True, f"released too early at {water}"
        _apply_latch_rules(c, 37.0, 20.0)          # 3 °C down from the latch temp
        assert c.ready_latched is False, "cooled 3 °C — Ready must be withdrawn"
        assert _ready_at(c) != "Ready"

    def test_the_two_day_scenario_no_longer_claims_ready(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        _apply_latch_rules(c, 40.0, 20.0)
        for water in (38.0, 34.0, 30.0, 26.0, 24.0):   # two days of cooling
            _apply_latch_rules(c, water, 20.0)
        assert c.ready_latched is False
        assert _ready_at(c) != "Ready"

    def test_cool_off_measured_from_the_warmest_point(self):
        """Thermostat cycling nudges the water above the setpoint; the cool-off
        must be measured from the peak reached, not from whatever the reading
        happened to be when it latched.

        The thermostat is dropped before cooling so the heating-gap release
        cannot fire — this isolates the cool-off rule.
        """
        c = MockCoordinator(water_temp=39.8, target_temp=40.0)
        _apply_latch_rules(c, 39.8, 40.0)          # latches at 39.8
        _apply_latch_rules(c, 40.4, 40.0)          # cycling peak (hysteresis dead band)
        assert c.ready_latched_temp == 40.4, "peak must be tracked in the dead band"
        _apply_latch_rules(c, 40.4, 20.0)          # after the soak: thermostat down
        _apply_latch_rules(c, 37.6, 20.0)          # 2.8 °C below the peak
        assert c.ready_latched is True, "not yet 3 °C from the peak"
        _apply_latch_rules(c, 37.3, 20.0)          # 3.1 °C below the peak
        assert c.ready_latched is False

    def test_heating_gap_release_still_takes_precedence(self):
        """A real heating gap releases via the setpoint rule before the cool-off
        threshold is reached — the two rules cover different situations."""
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        _apply_latch_rules(c, 37.6, 40.0)          # only 2.4 °C cooled, but a
        assert c.ready_latched is False             # 2.4 °C heating gap exists

    def test_normal_cycling_does_not_withdraw_ready(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        for water in (39.5, 39.0, 39.4, 39.8, 40.0, 39.2):
            _apply_latch_rules(c, water, 40.0)
            assert c.ready_latched is True, f"cycling withdrew Ready at {water}"

    def test_relatching_resets_the_reference_temperature(self):
        """After a release and a fresh arrival, the cool-off measures from the
        new arrival temperature — not the stale one."""
        c = MockCoordinator(water_temp=40.0, target_temp=40.0)
        _apply_latch_rules(c, 40.0, 40.0)
        _apply_latch_rules(c, 36.0, 40.0)          # cooled 4 °C → released
        assert c.ready_latched is False
        _apply_latch_rules(c, 30.0, 30.0)          # arrives at a new, lower target
        assert c.ready_latched is True
        assert c.ready_latched_temp == 30.0
        _apply_latch_rules(c, 28.0, 30.0)          # only 2 °C down from 30
        assert c.ready_latched is True


# ═══════════════════════════════════════════════════════════════════════════════
# READY AT — the ready_at attribute must never disagree with the state
# ═══════════════════════════════════════════════════════════════════════════════

class TestReadyAtAttributeMatchesState:
    """`ready_at` is the machine-readable form of the state, so the two must be
    derived from the same decision.

    They used not to be: extra_state_attributes re-derived the time and bailed out
    whenever the spa was cooling, so a pending schedule displayed "13:00 +1d" while
    ready_at reported nothing — the spa was cooling toward a maintenance setpoint
    below the schedule target.  That is the common overnight case, and it is the
    attribute anyone restoring a timestamp after the state became a display string
    will reach for.
    """

    def _attrs(self, c):
        e = _readiness_sensor(c)
        return MSpaReadinessSensor.extra_state_attributes.fget(e)

    def test_schedule_pending_while_cooling_exposes_the_scheduled_time(self):
        """The real 6 Aug case: water 23, thermostat 20 (cooling), schedule 39.5."""
        sched = datetime.now(timezone.utc) + timedelta(hours=14, minutes=31)
        c = MockCoordinator(
            water_temp=23.0, target_temp=20.0,          # cooling toward maintenance
            scheduled_ready_at=sched, schedule_target_temp=39.5,
            heat_rate=1.05, cool_rate=0.15,
        )
        attrs = self._attrs(c)
        assert attrs["direction"] == "cooling"
        assert attrs["ready_at_kind"] == "sched"
        assert attrs["ready_at"] == sched.isoformat(), "cooling must not blank the scheduled time"
        # and it agrees with what the state shows
        assert _ready_at(c) is not None

    def test_ready_state_reports_no_timestamp(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0, near_target=True)
        attrs = self._attrs(c)
        assert _ready_at(c) == "Ready"
        assert attrs["ready_at"] is None
        assert attrs["ready_at_kind"] == "ready"

    def test_free_heating_exposes_the_live_eta(self):
        c = MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on")
        attrs = self._attrs(c)
        assert attrs["ready_at_kind"] == "eta"
        assert attrs["ready_at"] is not None
        # ~5 h at 2 °C/h; generous bounds so the anchor logic isn't over-constrained
        eta = datetime.fromisoformat(attrs["ready_at"])
        hours = (eta - datetime.now(timezone.utc)).total_seconds() / 3600
        assert 3.0 < hours < 7.0, f"implausible ETA: {hours:.1f} h"

    def test_a_timestamp_is_present_whenever_the_state_shows_a_time(self):
        """The invariant: state shows a clock time  <=>  ready_at is populated."""
        sched = datetime.now(timezone.utc) + timedelta(hours=10)
        cases = [
            MockCoordinator(water_temp=23.0, target_temp=20.0,
                            scheduled_ready_at=sched, schedule_target_temp=39.5,
                            heat_rate=1.05, cool_rate=0.15),
            MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on"),
            MockCoordinator(water_temp=40.0, target_temp=40.0, near_target=True),
            MockCoordinator(water_temp=25.0, target_temp=20.0, cool_rate=0.15),
        ]
        for c in cases:
            state = _ready_at(c)
            attrs = self._attrs(c)
            shows_time = bool(state and re.match(r"^\d{1,2}:\d{2}", state))
            assert shows_time == (attrs["ready_at"] is not None), (
                f"state={state!r} but ready_at={attrs['ready_at']!r}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# READY AT ETA — deadband, coarse display, and snapping on cause not size
# ═══════════════════════════════════════════════════════════════════════════════

class TestEtaSmoothing:
    """The first slew attempt capped movement at 3 min/min and snapped anything
    over 30 min.  Measured over an 11 h session that gave 166 display changes and
    14 reversals: the cap rarely bound on the jitter, and both large corrections
    came from rate learning rather than the user, so they bypassed smoothing.
    """

    _BASE = datetime(2026, 8, 6, 21, 30, tzinfo=timezone.utc)

    def _sensor(self, c):
        return _readiness_sensor(c)

    def test_small_movement_is_ignored(self):
        """±1-2 min jitter produced most of the churn and must not reach the display."""
        c = MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on")
        e = self._sensor(c)
        first = e._slew_eta(self._BASE + timedelta(minutes=300), self._BASE)
        for i, drift in enumerate([1, 2, -1, 2, -2, 1], start=1):
            got = e._slew_eta(self._BASE + timedelta(minutes=300 + drift),
                              self._BASE + timedelta(seconds=30 * i))
            assert got == first, f"jitter of {drift} min moved the display"

    def test_display_is_rounded_to_five_minutes(self):
        c = MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on")
        e = self._sensor(c)
        got = e._slew_eta(self._BASE.replace(minute=37, second=0), self._BASE)
        assert got.minute % 5 == 0, f"{got} is not on a 5-minute boundary"
        assert got.second == 0

    def test_large_model_correction_ramps_instead_of_snapping(self):
        """The -68 min case: a rate sample revises the estimate, so it must slew."""
        c = MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on")
        e = self._sensor(c)
        e._slew_eta(self._BASE + timedelta(minutes=300), self._BASE)
        # 68 min earlier, one poll later — must NOT be adopted wholesale
        got = e._slew_eta(self._BASE + timedelta(minutes=232),
                          self._BASE + timedelta(seconds=30))
        moved = abs((got - (self._BASE + timedelta(minutes=300))).total_seconds() / 60)
        assert moved <= 5, f"moved {moved:.0f} min in 30 s — snapped instead of ramping"

    def test_a_real_replan_snaps(self):
        """Moving the thermostat is the user changing the question — jump to it."""
        c = MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on")
        e = self._sensor(c)
        e._slew_eta(self._BASE + timedelta(minutes=300), self._BASE)
        c._last_data["target_temperature"] = "38.0"          # user lowers the setpoint
        target = self._BASE + timedelta(minutes=232)
        got = e._slew_eta(target, self._BASE + timedelta(seconds=30))
        assert got == e._round_eta(target), "a genuine replan should be adopted at once"

    def test_sustained_drift_still_gets_through(self):
        """Smoothing must not mean ignoring a real trend."""
        c = MockCoordinator(water_temp=30.0, target_temp=40.0, heat_rate=2.0, heater="on")
        e = self._sensor(c)
        e._slew_eta(self._BASE + timedelta(minutes=300), self._BASE)
        raw = self._BASE + timedelta(minutes=360)            # 60 min later, held
        last = None
        for i in range(1, 121):                              # two hours of polls
            last = e._slew_eta(raw, self._BASE + timedelta(minutes=i))
        assert abs((last - raw).total_seconds() / 60) <= 5, (
            f"display stalled at {last}, raw was {raw}")


class TestScheduleStartSlew:
    """The displayed schedule start holds steady against uninformative drift.

    Recomputed every poll from a quantized reading, the start moves two ways: a
    lump of roughly a band's heating when the reading crosses, and a percentage
    drift as the ambient factor rescales the estimate.  Only the first is worth
    showing.  Measured on the 2026-08-11/12 cool-down, this takes the displayed
    value from 31 changes with 5 direction reversals to 12 changes with 1.
    """

    _BASE = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)

    class _Coord:
        """Only what _plan_key reads, so the real key function is exercised."""

        def __init__(self, water):
            self.scheduled_ready_at = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)
            self.schedule_target_temp = 39.5
            self._last_data = {"water_temperature": str(water)}

    def _stub(self):
        return _HeatScheduleStub(self._Coord(33.0), MockConfigEntry())

    def _slew(self, stub, start_min, now_min, water):
        """Slew a start `start_min` min ahead of now, with the reading at `water`."""
        stub.coordinator._last_data["water_temperature"] = str(water)
        now = self._BASE + timedelta(minutes=now_min)
        raw = now + timedelta(minutes=start_min)
        return stub._slew_start(raw, now), raw

    def test_first_value_is_adopted(self):
        stub = self._stub()
        shown, raw = self._slew(stub, 600, 0, 33.0)
        assert shown == raw

    def test_ambient_drift_within_one_reading_is_held(self):
        """A 28 min push-back with the reading unchanged is drift, and is hidden."""
        stub = self._stub()
        first, _ = self._slew(stub, 600, 0, 33.0)
        shown, raw = self._slew(stub, 628, 0, 33.0)
        assert shown == first, "drift should not move the display"
        assert shown != raw

    def test_reading_change_is_adopted_immediately(self):
        """A crossing is real movement however small, so cause beats size."""
        stub = self._stub()
        self._slew(stub, 600, 0, 33.0)
        shown, raw = self._slew(stub, 610, 0, 32.5)
        assert shown == raw

    def test_small_crossing_step_still_shows(self):
        """At a fast bucket rate a band is worth ~15 min — under any size deadband."""
        stub = self._stub()
        first, _ = self._slew(stub, 600, 0, 33.0)
        shown, raw = self._slew(stub, 585, 0, 32.5)
        assert shown == raw and shown != first

    def test_large_earlier_drift_is_eventually_believed(self):
        stub = self._stub()
        self._slew(stub, 600, 0, 33.0)
        shown, raw = self._slew(stub, 600 - 31, 0, 33.0)
        assert shown == raw

    def test_later_drift_needs_a_full_band_before_it_is_believed(self):
        """Asymmetric: later-drift runs against the cool-down trend, so it waits.

        On 2026-08-12 07:29 a +40 min ambient drift was followed two minutes later
        by a crossing that took the start back to within 7 min of the held value.
        Adopting the 40 would have shown a swing that never happened.
        """
        stub = self._stub()
        first, _ = self._slew(stub, 600, 0, 33.0)
        shown, _ = self._slew(stub, 640, 0, 33.0)
        assert shown == first, "+40 min of later-drift should still be held"
        shown, raw = self._slew(stub, 661, 0, 33.0)
        assert shown == raw, "beyond a band's worth it is believed"

    def test_live_value_shown_once_the_start_is_close(self):
        """Inside the tracking window accuracy beats stability."""
        stub = self._stub()
        first, _ = self._slew(stub, 600, 0, 33.0)
        shown, raw = self._slew(stub, 40, 10, 33.0)
        assert shown == raw and shown != first

    def test_start_now_still_fires_on_time(self):
        """A start in the past is inside the tracking window, so it is never held."""
        stub = self._stub()
        self._slew(stub, 600, 0, 33.0)
        shown, raw = self._slew(stub, -1, 10, 33.0)
        assert shown == raw

    def test_plan_change_snaps(self):
        """Moving the schedule is the user replanning, not the model drifting."""
        stub = self._stub()
        first, _ = self._slew(stub, 600, 0, 33.0)
        stub.coordinator.scheduled_ready_at = datetime(
            2026, 8, 13, 21, 0, tzinfo=timezone.utc)
        shown, raw = self._slew(stub, 610, 0, 33.0)
        assert shown == raw and shown != first

    def test_idempotent(self):
        """native_value and extra_state_attributes both evaluate on one update."""
        stub = self._stub()
        self._slew(stub, 600, 0, 33.0)
        a, _ = self._slew(stub, 628, 0, 33.0)
        b, _ = self._slew(stub, 628, 0, 33.0)
        c, _ = self._slew(stub, 628, 0, 33.0)
        assert a == b == c

    def test_unreadable_temperature_does_not_raise(self):
        stub = self._stub()
        stub.coordinator._last_data["water_temperature"] = "unavailable"
        shown, raw = self._slew(stub, 600, 0, "unavailable")
        assert shown == raw

    def test_held_value_never_exceeds_a_band_of_staleness_while_cooling(self):
        """Replay of the measured cool-down: hold, but never by more than a band."""
        stub = self._stub()
        water = 33.0
        for i in range(0, 240, 10):
            if i and i % 70 == 0:                 # a crossing every ~70 min
                water -= 0.5
            drift = 20 if (i // 10) % 2 else -8   # ambient wobbling either way
            held, raw = self._slew(stub, 600 - i + drift, i, water)
            gap = abs((held - raw).total_seconds() / 60)
            assert gap < 60, f"held {gap:.0f} min from live at step {i}"


class TestTemperatureBasis:
    """The probe reads the pump housing, not the tub, whenever circulation stops.

    Reported rather than corrected: a stagnant reading runs at or below tub
    temperature, so the estimate over-states the work remaining, which is the safe
    direction.  The attribute tells a template to treat it as a lower bound.
    """

    def _coord(self, filter_state):
        c = MockCoordinator(
            water_temp=33.0, target_temp=39.5,
            scheduled_ready_at=_NOW_UTC + timedelta(hours=9),
            schedule_target_temp=39.5,
        )
        c._last_data["filter"] = filter_state
        return c

    def test_pump_running_reports_tub_water(self):
        c = self._coord("on")
        attrs = _heat_schedule_attrs(c)
        assert attrs["circulating"] is True
        assert attrs["temperature_basis"] == "tub water"

    def test_pump_stopped_reports_housing(self):
        c = self._coord("off")
        attrs = _heat_schedule_attrs(c)
        assert attrs["circulating"] is False
        assert "pump housing" in attrs["temperature_basis"]

    def test_missing_filter_key_is_treated_as_not_circulating(self):
        """Absent is not the same as running — default to declaring it unknown."""
        c = self._coord("on")
        del c._last_data["filter"]
        attrs = _heat_schedule_attrs(c)
        assert attrs["circulating"] is False

    def test_state_is_unaffected(self):
        """Reported, not corrected: the estimate itself must not move."""
        on = _heat_schedule(self._coord("on"))
        off = _heat_schedule(self._coord("off"))
        assert on == off, "pump state must not change the prediction"

    def test_sensor_stays_available_when_not_circulating(self):
        """An attribute, not unavailability — dropping the state breaks history."""
        c = self._coord("off")
        assert _heat_schedule(c) not in (None, "unavailable", "unknown")
