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
    _anchor_eta_utc,
    _segmented_heating_minutes,
    MSpaReadinessSensor,
    MSpaHeatScheduleSensor,
)
from custom_components.mspa.const import (
    CONF_SCHEDULE_LOOKAHEAD_DAYS,
    DEFAULT_SCHEDULE_LOOKAHEAD_DAYS,
)
from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.predictor import HeatPredictor, extrapolate_within_band

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
        shadow_revisions: "int | None" = None,
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
        # None means no session, matching the real coordinator outside one.
        self._shadow_revisions = shadow_revisions
        # Target as the readiness latch last saw it. None on the first poll, which is
        # why a spa that simply reaches its setpoint still latches.
        self._latch_target = None
        self._settled_target = None
        self._pending_target = None
        # No session in flight by default, so the ETA falls through to the live rates
        # exactly as the existing scenarios were written against.  TestFrozenSessionPlan
        # populates this to exercise the frozen plan.
        self._prediction = None

        self.temp_anchor_time = datetime.now(timezone.utc) + timedelta(minutes=anchor_offset_minutes)
        self.temp_anchor_temp = water_temp
        self.temp_anchor_target = target_temp
        # Default to the fallback path so existing scenarios see the reported reading,
        # which is what they were written against.  TestSchedulingTemp sets these up to
        # exercise the extrapolation.
        self.temp_anchor_rising = None
        self.circulating_since = None
        self.heating_since = None
        self.heat_rate_buckets = [heat_rate, heat_rate, heat_rate] if heat_rate else [None, None, None]

        _heater = heater if heater is not None else ("on" if target_temp > water_temp else "off")
        self._last_data = {
            "filter": "on",
            "water_temperature": str(water_temp),
            "target_temperature": str(target_temp),
            "device_heat_perhour": device_heat_perhour,
            "is_online": is_online,
            "heater": _heater,
        }

    def shadow_revisions(self):
        """Matches the real coordinator: revision count, or None outside a session."""
        return self._shadow_revisions


    # Borrowed, not reimplemented: a mock that restates the logic under test proves
    # only that the mock agrees with itself.
    circulating = MSpaUpdateCoordinator.circulating
    scheduling_temp = MSpaUpdateCoordinator.scheduling_temp
    session_plan = MSpaUpdateCoordinator.session_plan
    session_settled = MSpaUpdateCoordinator.session_settled
    session_opening_eta = MSpaUpdateCoordinator.session_opening_eta
    session_progress_deviation = MSpaUpdateCoordinator.session_progress_deviation
    shadow_eta = MSpaUpdateCoordinator.shadow_eta

    def _predictor(self):
        return HeatPredictor(
            buckets=tuple(self.heat_rate_buckets),
            prediction_bias=self.prediction_bias,
        )


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
    """Apply a temperature reading through the coordinator's own near-target logic.

    Borrowed, not reimplemented. This helper used to be a hand-written copy of the
    block, and it silently diverged: when the 2026-08-14 overshoot bug was fixed in the
    coordinator, the copy here still used abs() and three tests failed against correct
    code.
    """
    c.temp_anchor_temp = new_temp
    c.temp_anchor_target = new_target
    c._last_data["water_temperature"] = str(new_temp)
    c._last_data["target_temperature"] = str(new_target)
    MSpaUpdateCoordinator._update_near_target(c, new_temp, new_target)


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
        """D: Thermostat lowered while spa is at temp — Ready must persist.

        `near_target` now stays True here, where it used to go False. That is the
        overshoot fix: the test of readiness is the *shortfall*, and water at 40.0
        against a 38.0 setpoint is not short of anything. The assertion that it went
        False was describing the old absolute-gap mechanism rather than the
        requirement, which is the last line and is unchanged.
        """
        c = MockCoordinator(ready_latched=True, near_target=True,
                            water_temp=40.0, target_temp=40.0)
        _apply_temp_update(c, new_temp=40.0, new_target=38.0)
        assert c.near_target is True, "water above setpoint is not short of target"
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

    def test_shadow_revision_snaps_rather_than_ramps(self):
        """A plan revision is adopted at once, not crawled toward.

        ShadowPlan revises about six times in a session, after measuring a third of the
        remaining climb. Slewing that at a minute per minute meant a three-hour
        correction took three hours to show, and the next revision always overtook it:
        on 2026-08-19 the display sat 122 minutes behind a plan that had been right for
        an hour. The churn the cap exists to suppress is already suppressed here.
        """
        c = MockCoordinator(shadow_revisions=1)
        e = _readiness_sensor(c)
        eta = self._BASE + timedelta(hours=12)
        e._slew_eta(eta, now_utc=self._BASE)

        revised = eta - timedelta(minutes=180)
        c._shadow_revisions = 2
        shown = e._slew_eta(revised, now_utc=self._BASE + timedelta(minutes=1))
        assert e._eta_display == revised, "a revision should be adopted, not ramped"
        assert shown == revised

    def test_drift_without_a_revision_still_ramps(self):
        """The cap still applies to everything that is not a revision."""
        c = MockCoordinator(shadow_revisions=1)
        e = _readiness_sensor(c)
        eta = self._BASE + timedelta(hours=12)
        e._slew_eta(eta, now_utc=self._BASE)

        drift = eta - timedelta(minutes=180)
        shown = e._slew_eta(drift, now_utc=self._BASE + timedelta(minutes=1))
        assert e._eta_display == eta - timedelta(minutes=1), (
            "without a revision the gap must close at the capped rate"
        )
        assert shown != drift

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
            self._prediction = None
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


class TestExtrapolateWithinBand:
    """The pure in-band extrapolation: clamp, continuity, neutrality."""

    def test_clamped_to_one_band_however_long_it_runs(self):
        """The reading has not changed, so the next threshold cannot be crossed.

        A band takes 0.5/0.36 = 1.39 h at this rate, so 1 h does *not* saturate — the
        clamp is a bound on the deduction, not a cap applied early.
        """
        assert extrapolate_within_band(33.25, 1.0, 0.36, cooling=True) == pytest.approx(32.89)
        for hours in (2, 10, 1000):
            est = extrapolate_within_band(33.25, hours, 0.36, cooling=True)
            assert est == pytest.approx(33.25 - 0.5)

    def test_clamped_in_the_warming_direction_too(self):
        est = extrapolate_within_band(33.25, 1000, 0.9, cooling=False)
        assert est == pytest.approx(33.25 + 0.5)

    def test_a_full_band_of_drift_lands_on_the_next_anchor(self):
        """Continuity: handing over at a crossing introduces no step.

        Cooling out of reading 33.0 (anchor 33.25), the next crossing is to 32.5, whose
        band-centre anchor is 32.75 — exactly where a full band of drift arrives.
        """
        assert extrapolate_within_band(33.25, 10, 0.36, cooling=True) == pytest.approx(32.75)

    def test_neutral_against_holding_the_reading_at_the_dwell_midpoint(self):
        """Half a band warm at the start, half a band cold at the end, reading in the middle."""
        rate = 0.36
        dwell_h = 0.5 / rate
        mid = extrapolate_within_band(33.25, dwell_h / 2, rate, cooling=True)
        assert mid == pytest.approx(33.0), "midpoint should equal the reported reading"

    def test_no_rate_or_bad_input_returns_none(self):
        assert extrapolate_within_band(33.25, 1.0, None, cooling=True) is None
        assert extrapolate_within_band(33.25, 1.0, 0.0, cooling=True) is None
        assert extrapolate_within_band(None, 1.0, 0.36, cooling=True) is None
        assert extrapolate_within_band(33.25, -1.0, 0.36, cooling=True) is None


class TestSchedulingTemp:
    """Every guard falls back to the reported reading, i.e. to today's behaviour."""

    def _coord(self, **kw):
        anchor_min = kw.pop("anchor_offset_minutes", -40.0)
        c = MockCoordinator(
            water_temp=33.0, target_temp=39.5, cool_rate=0.36,
            anchor_offset_minutes=anchor_min,
            scheduled_ready_at=_NOW_UTC + timedelta(hours=9),
            schedule_target_temp=39.5,
        )
        # a cooling crossing into the 33.0 band, recorded while circulating
        c.temp_anchor_temp = 33.25
        c.temp_anchor_rising = False
        c.circulating_since = c.temp_anchor_time - timedelta(hours=1)
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_extrapolates_when_everything_is_sound(self):
        c = self._coord()
        est = c.scheduling_temp()
        assert est is not None and est < 33.25, "should have drifted below the anchor"
        assert est > 32.75 - 1e-9

    def test_not_circulating_falls_back_to_the_reading(self):
        """Stagnant housing water is not the tub, so there is nothing to extrapolate."""
        c = self._coord()
        c._last_data["filter"] = "off"
        assert c.scheduling_temp() == pytest.approx(33.0)

    def test_anchor_predating_circulation_falls_back(self):
        """That crossing was recorded on housing water."""
        c = self._coord()
        c.circulating_since = c.temp_anchor_time + timedelta(minutes=1)
        assert c.scheduling_temp() == pytest.approx(33.0)

    def test_heater_started_after_the_anchor_falls_back(self):
        """Direction may have reversed since the crossing."""
        c = self._coord()
        c.heating_since = c.temp_anchor_time + timedelta(minutes=1)
        assert c.scheduling_temp() == pytest.approx(33.0)

    def test_unknown_direction_falls_back(self):
        """A restart, or a jump of more than one band — not an observed crossing."""
        c = self._coord(temp_anchor_rising=None)
        assert c.scheduling_temp() == pytest.approx(33.0)

    def test_no_cool_rate_falls_back(self):
        c = self._coord(computed_cool_rate=None)
        assert c.scheduling_temp() == pytest.approx(33.0)

    def test_unreadable_reading_returns_none(self):
        c = self._coord()
        c._last_data["water_temperature"] = "unavailable"
        assert c.scheduling_temp() is None

    def test_never_leaves_the_band_even_with_an_absurd_rate(self):
        """Belt and braces: a corrupt rate must not move the estimate out of the band."""
        c = self._coord(computed_cool_rate=99.0)
        assert c.scheduling_temp() == pytest.approx(32.75)


class TestStartTimeRampsInsteadOfLumping:
    """End to end: the planned start moves smoothly through a dwell."""

    def _start_minutes(self, elapsed_min):
        c = MockCoordinator(
            water_temp=33.0, target_temp=39.5, cool_rate=0.36,
            anchor_offset_minutes=-elapsed_min,
            scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
            schedule_target_temp=39.5,
        )
        c.temp_anchor_temp = 33.25
        c.temp_anchor_rising = False
        c.circulating_since = c.temp_anchor_time - timedelta(hours=1)
        attrs = _heat_schedule_attrs(c)
        start = datetime.fromisoformat(attrs["start_at"])
        return (start - _NOW_UTC).total_seconds() / 60.0

    def test_start_moves_monotonically_earlier_through_the_dwell(self):
        starts = [self._start_minutes(m) for m in (0, 20, 40, 60, 83)]
        assert starts == sorted(starts, reverse=True), f"not monotonic: {starts}"

    def test_no_single_step_is_a_whole_band(self):
        """The lump this replaces was 32 min; sampled every 20 min it must be smaller."""
        starts = [self._start_minutes(m) for m in range(0, 84, 20)]
        steps = [abs(b - a) for a, b in zip(starts, starts[1:])]
        assert max(steps) < 20, f"still lumpy: {steps}"


class TestWarmingWithTheHeaterOff:
    """Solar gain must not be extrapolated at the heater's rate.

    Raised 2026-08-13: the spa occasionally warms from sun or conducted heat with the
    heater off. `rising` is then True from an observed crossing, but the only upward
    rate available is the heater's — two to five times the real gain — so the clamp
    saturated early and pinned the estimate a quarter-band above the reading. That is
    ~19 min of optimism in the direction that starts a session late, and it reversed
    sign at a band boundary.
    """

    def _coord(self, *, rising, heating):
        c = MockCoordinator(
            water_temp=30.0, target_temp=39.5, cool_rate=0.30,
            anchor_offset_minutes=-45.0,
            scheduled_ready_at=_NOW_UTC + timedelta(hours=9),
            schedule_target_temp=39.5,
        )
        c.temp_anchor_temp = 30.25 if rising else 29.75
        c.temp_anchor_rising = rising
        c.circulating_since = c.temp_anchor_time - timedelta(hours=2)
        c.heating_since = (c.temp_anchor_time - timedelta(hours=1)) if heating else None
        return c

    def test_warming_with_the_heater_off_falls_back_to_the_reading(self):
        c = self._coord(rising=True, heating=False)
        assert c.scheduling_temp() == pytest.approx(30.0)

    def test_warming_while_heating_still_extrapolates(self):
        """The heater's rate is the right model when the heater is what is doing it."""
        c = self._coord(rising=True, heating=True)
        est = c.scheduling_temp()
        assert est > 30.25, "should have climbed above the anchor"

    def test_cooling_with_the_heater_off_still_extrapolates(self):
        """The ordinary case, and the one the feature exists for."""
        c = self._coord(rising=False, heating=False)
        est = c.scheduling_temp()
        assert est < 29.75, "should have drifted below the anchor"

    def test_no_optimism_beyond_the_reading_when_warming_unheated(self):
        """The estimate must never claim the water is warmer than reported."""
        for minutes in (5, 30, 120, 600):
            c = self._coord(rising=True, heating=False)
            c.temp_anchor_time = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            assert c.scheduling_temp() <= 30.0 + 1e-9, f"optimistic after {minutes} min"


class TestOvershootIsStillReady:
    """Water above target is ready, not un-ready.

    Reported 2026-08-14: the spa reached 40.0 against a 39.5 target — ordinary
    thermostat overshoot — and Ready at fell to unknown. The near-target test used
    the absolute gap, so 0.5 °C *above* target read the same as 0.5 °C short. The
    schedule expired in the same window and released the latch, so both routes to
    "Ready" went at once.
    """

    def _apply(self, water, target, near_before=True, latched=False):
        c = MockCoordinator(water_temp=water, target_temp=target,
                            near_target=near_before, ready_latched=latched)
        _apply_temp_update(c, new_temp=water, new_target=target)
        return c

    def test_overshoot_stays_near_target(self):
        assert self._apply(40.0, 39.5).near_target is True

    def test_large_overshoot_stays_near_target(self):
        """Nothing about being warmer than asked makes the spa less ready."""
        assert self._apply(41.0, 39.5).near_target is True

    def test_falling_short_still_clears_it(self):
        """The hysteresis must still work in the direction that means 'not ready'."""
        assert self._apply(39.0, 39.5).near_target is False

    def test_inside_the_dead_band_is_unchanged(self):
        assert self._apply(39.2, 39.5, near_before=True).near_target is True

    def test_reaching_target_still_latches(self):
        c = MockCoordinator(water_temp=39.0, target_temp=39.5, near_target=False)
        _apply_temp_update(c, new_temp=39.5, new_target=39.5)
        assert c.ready_latched is True

    def test_overshoot_arriving_from_below_latches_too(self):
        """Crossing straight past the target must not skip the latch."""
        c = MockCoordinator(water_temp=39.0, target_temp=39.5, near_target=False)
        _apply_temp_update(c, new_temp=40.0, new_target=39.5)
        assert c.near_target is True and c.ready_latched is True


class TestFrozenSessionPlan:
    """Within a session the ETA integrates rates frozen at its start, after a settle.

    Measured over four recorded heat-ups in analysis/settle_time.py. Replanning at
    each 0.5 °C crossing converges to 0 min at the finish, where holding the opening
    estimate carries its error all the way in — 35 min on the worst session, still
    35 min wrong at the moment the spa was ready. Before the settle point the
    opposite holds, so each is used where it wins.
    """

    _START = datetime(2026, 8, 14, 3, 46, tzinfo=timezone.utc)

    def _coord(self, *, water, elapsed_min, start_temp=33.0):
        c = MockCoordinator(water_temp=water, target_temp=39.5, heat_rate=1.0,
                            anchor_offset_minutes=0.0)
        c.temp_anchor_temp = water
        c.temp_anchor_time = datetime.now(timezone.utc)
        c.heat_rate_buckets = [2.0, 2.0, 2.0]      # live rates, deliberately different
        c._prediction = {
            "start_time": (datetime.now(timezone.utc)
                           - timedelta(minutes=elapsed_min)).isoformat(),
            "start_temp": start_temp,
            "target_temp": 39.5,
            "estimated_minutes": 426.0,
            "estimated_minutes_biased": 426.0,
            "plan_rates": [1.10, 1.00, 0.80],
        }
        return c

    def test_the_plan_uses_frozen_rates_not_live_ones(self):
        """Live buckets are 2.0; the plan's are 1.0/0.8. The plan must win."""
        c = self._coord(water=37.0, elapsed_min=200)
        plan = c.session_plan()
        assert plan is not None
        frozen = plan.heating_minutes(37.0, 39.5)
        live = _segmented_heating_minutes(37.0, 39.5, c)
        assert frozen > live * 1.5, f"frozen {frozen} looks like the live {live}"

    def test_the_eta_itself_uses_the_frozen_rates(self):
        """Through the sensor, not just the plan object.

        Live buckets are 2.0 °C/h and the frozen hot rate is 0.80, so 37.0 → 39.5 is
        75 min live against 187 frozen. An ETA near the live figure would mean the
        freeze is not reaching the sensor — which an earlier version of this suite
        failed to catch, because it only exercised the plan directly.
        """
        c = self._coord(water=37.0, elapsed_min=300)
        now = datetime.now(timezone.utc)
        eta = _anchor_eta_utc(c, 39.5, now)
        minutes = (eta - c.temp_anchor_time).total_seconds() / 60.0
        assert minutes > 150, f"{minutes:.0f} min looks like the live rate, not frozen"

    def test_no_session_means_no_plan(self):
        c = MockCoordinator(water_temp=35.0, target_temp=39.5)
        assert c.session_plan() is None

    def test_not_settled_before_ninety_minutes(self):
        c = self._coord(water=35.0, elapsed_min=60)      # 2.0 °C but only 60 min
        assert c.session_settled(35.0) is False

    def test_not_settled_before_one_and_a_half_degrees(self):
        c = self._coord(water=34.0, elapsed_min=200)     # 200 min but only 1.0 °C
        assert c.session_settled(34.0) is False

    def test_settled_once_both_are_met(self):
        c = self._coord(water=34.5, elapsed_min=95)
        assert c.session_settled(34.5) is True

    def test_before_settling_the_eta_is_the_opening_estimate(self):
        c = self._coord(water=34.0, elapsed_min=60)
        eta = _anchor_eta_utc(c, 39.5, datetime.now(timezone.utc))
        opening = c.session_opening_eta()
        assert eta == opening

    def test_after_settling_the_eta_follows_the_water(self):
        """Two temperatures, same session: the later one must finish sooner."""
        warm = _anchor_eta_utc(self._coord(water=38.0, elapsed_min=300), 39.5,
                               datetime.now(timezone.utc))
        cool = _anchor_eta_utc(self._coord(water=35.0, elapsed_min=300), 39.5,
                               datetime.now(timezone.utc))
        assert warm < cool

    def test_a_plan_with_no_rates_falls_back_to_live(self):
        """A session started before this shipped, restored from storage."""
        c = self._coord(water=37.0, elapsed_min=200)
        c._prediction.pop("plan_rates")
        assert c.session_plan() is None
        assert _anchor_eta_utc(c, 39.5, datetime.now(timezone.utc)) is not None

    def test_a_corrupt_start_time_does_not_raise(self):
        c = self._coord(water=37.0, elapsed_min=200)
        c._prediction["start_time"] = "not a timestamp"
        assert c.session_settled(37.0) is False
        assert c.session_opening_eta() is None


class TestProgressDeviation:
    """How the session is running against its own opening plan, in minutes.

    Deliberately a separate attribute rather than folded into the ETA: the estimate
    answers "when", this answers "how is it going", and conflating them is what made
    the shipped ETA chase every sample.
    """

    def _coord(self, *, water, elapsed_min, start_temp=33.0):
        c = MockCoordinator(water_temp=water, target_temp=39.5, heat_rate=1.0,
                            anchor_offset_minutes=0.0)
        c.temp_anchor_temp = water
        c._prediction = {
            "start_time": (datetime.now(timezone.utc)
                           - timedelta(minutes=elapsed_min)).isoformat(),
            "start_temp": start_temp,
            "target_temp": 39.5,
            "estimated_minutes": 426.0,
            "estimated_minutes_biased": 426.0,
            "plan_rates": [1.10, 1.00, 0.80],   # mid 1.0 °C/h → 1 °C per 60 min
        }
        return c

    def test_none_outside_a_session(self):
        c = MockCoordinator(water_temp=35.0, target_temp=39.5)
        assert c.session_progress_deviation(35.0) is None

    def test_none_before_the_settle_point(self):
        """The opening crossings measure band position, so a deviation then is noise."""
        c = self._coord(water=34.0, elapsed_min=45)
        assert c.session_progress_deviation(34.0) is None

    def test_behind_schedule_is_positive(self):
        """2.0 °C at 1.0 °C/h is allowed 120 min; taking 150 is 30 behind."""
        c = self._coord(water=35.0, elapsed_min=150)
        assert c.session_progress_deviation(35.0) == pytest.approx(30.0, abs=0.5)

    def test_ahead_of_schedule_is_negative(self):
        c = self._coord(water=35.0, elapsed_min=100)
        assert c.session_progress_deviation(35.0) == pytest.approx(-20.0, abs=0.5)

    def test_on_plan_is_about_zero(self):
        c = self._coord(water=35.0, elapsed_min=120)
        assert abs(c.session_progress_deviation(35.0)) < 0.5

    def test_it_does_not_move_the_eta(self):
        """The whole point of a separate attribute."""
        behind = self._coord(water=35.0, elapsed_min=150)
        on_plan = self._coord(water=35.0, elapsed_min=120)
        now = datetime.now(timezone.utc)
        a = _anchor_eta_utc(behind, 39.5, now) - behind.temp_anchor_time
        b = _anchor_eta_utc(on_plan, 39.5, now) - on_plan.temp_anchor_time
        assert a == b, "the remaining estimate must not depend on the deviation"

    def test_a_corrupt_record_does_not_raise(self):
        c = self._coord(water=35.0, elapsed_min=150)
        c._prediction["start_temp"] = "nonsense"
        assert c.session_progress_deviation(35.0) is None

    def test_exposed_on_the_ready_at_sensor(self):
        c = self._coord(water=35.0, elapsed_min=150)
        attrs = MSpaReadinessSensor.extra_state_attributes.fget(
            _readiness_sensor(c))
        assert attrs["progress_deviation"] == pytest.approx(30.0, abs=0.5)
        assert attrs["plan_settled"] is True


class TestIntegrationVersionAttribute:
    """Which build is actually running, readable without trawling the log.

    A hot deploy copies source over a live install and stamps the manifest with the
    commit it came from, but HACS's update entity keeps reporting whatever HACS itself
    installed — on 2026-08-17 that was v2026.8.1 against a running 2026.8.2-beta+hot.
    The setup log line answers it too, until it scrolls out of the retained window.
    """

    def _attrs(self, c):
        e = _readiness_sensor(c)
        return MSpaReadinessSensor.extra_state_attributes.fget(e)

    def test_the_running_build_is_exposed(self):
        c = MockCoordinator(water_temp=29.5, target_temp=39.5)
        c.integration_version = "2026.8.2-beta+hot.f6c1d54"
        assert self._attrs(c)["integration_version"] == "2026.8.2-beta+hot.f6c1d54"

    def test_a_coordinator_without_one_reports_none_rather_than_raising(self):
        """Older coordinators, and any path that set up before the version was read."""
        c = MockCoordinator(water_temp=29.5, target_temp=39.5)
        assert self._attrs(c)["integration_version"] is None

    def test_the_value_survives_json_serialisation(self):
        """The loader hands back an AwesomeVersion; unconverted it breaks the state
        machine, so __init__ str()s it. Guard the shape the attribute must have."""
        import json
        c = MockCoordinator(water_temp=29.5, target_temp=39.5)
        c.integration_version = "2026.8.2-beta+hot.f6c1d54"
        json.dumps(self._attrs(c)["integration_version"])


class TestAbandonedSessionIsNotRecorded:
    """Stopping a heat-up must not be recorded as having finished it.

    Observed 2026-08-20: a run from 28.5 °C toward 39.5 was aborted at 30.0 °C by
    moving the setpoint to 20. near_target is measured against whatever the setpoint
    is now, so that satisfied the completion check without a degree of progress, and
    the log reported "estimated 669 min, actual 42 min | error -1505.3%".

    The settle timer does cancel a plan whose target has moved, but it waits a minute
    so a dial sweep cannot destroy the session, and the completion check runs first.
    Completion therefore asks its own question: is this still the setpoint the plan
    was made for.
    """

    @staticmethod
    def _plan(target=39.5):
        return {"target_temp": target, "start_temp": 28.5, "estimated_minutes": 669.0}

    def test_the_reported_abort(self):
        """39.5 planned, setpoint dropped to 20."""
        assert MSpaUpdateCoordinator._plan_abandoned(self._plan(), 20.0) is True

    def test_reaching_the_planned_target_is_not_abandonment(self):
        assert MSpaUpdateCoordinator._plan_abandoned(self._plan(), 39.5) is False

    def test_a_setpoint_nudged_within_the_quantisation_band_still_counts(self):
        """Readings are quantised to 0.5 °C, so less than that is not a real move."""
        assert MSpaUpdateCoordinator._plan_abandoned(self._plan(), 39.3) is False

    def test_raising_the_target_also_abandons_the_plan(self):
        """The plan was for 39.5; 41 is a different session, not this one finishing."""
        assert MSpaUpdateCoordinator._plan_abandoned(self._plan(), 41.0) is True

    def test_no_plan_is_not_abandonment(self):
        assert MSpaUpdateCoordinator._plan_abandoned(None, 20.0) is False

    def test_unknowns_never_discard_a_measurement(self):
        """A missing reading must not be read as an abort."""
        assert MSpaUpdateCoordinator._plan_abandoned(self._plan(), None) is False
        assert MSpaUpdateCoordinator._plan_abandoned({"target_temp": None}, 20.0) is False
        assert MSpaUpdateCoordinator._plan_abandoned(self._plan(), "nonsense") is False

    def test_the_bias_was_never_the_exposure(self):
        """Belt and braces: the ratio from the reported abort is rejected anyway.

        Recorded so that if the guard above is ever relaxed, the second line of
        defence is known to be there rather than assumed.
        """
        record = {"target_temp": 39.5, "start_temp": 28.5,
                  "estimated_minutes": 669.0, "actual_minutes": 42.0}
        assert MSpaUpdateCoordinator._bias_ratio(record) is None


class TestRestartDoesNotInventAnArrival:
    """A restart must not latch Ready just because the water is above the setpoint.

    Reported 2026-08-20: Ready at said "Ready" the moment Home Assistant came back,
    with the session still climbing. The latch is set on the False→True edge of
    near_target, and on a fresh coordinator both guards on that edge are vacuously
    satisfied — near_target starts False, and _latch_target is None so nothing looks
    like a setpoint that moved. The first poll of a warm tub sitting above a parked
    setpoint therefore took the "heated there, not dialled there" branch.

    The first sample is now judged on the only evidence it has: how far above the
    setpoint the water sits.
    """

    def test_restart_with_water_far_above_a_parked_setpoint_does_not_latch(self):
        """The reported case: 28.5 °C in the tub, thermostat left at 20."""
        c = MockCoordinator(water_temp=28.5, target_temp=20.0, near_target=False)
        assert c._latch_target is None, "a restart starts with no remembered setpoint"
        _apply_temp_update(c, new_temp=28.5, new_target=20.0)
        assert c.ready_latched is False, "a restart must not invent an arrival"

    def test_and_ready_at_does_not_claim_ready(self):
        """End to end — the latch was the only thing making this read Ready."""
        c = MockCoordinator(water_temp=28.5, target_temp=20.0, near_target=False)
        _apply_temp_update(c, new_temp=28.5, new_target=20.0)
        assert _ready_at(c) != "Ready"

    def test_restart_at_the_setpoint_still_latches(self):
        """The after-a-soak latch is meant to survive a restart, and does."""
        c = MockCoordinator(water_temp=39.6, target_temp=39.5, near_target=False)
        assert c._latch_target is None
        _apply_temp_update(c, new_temp=39.6, new_target=39.5)
        assert c.ready_latched is True, "ordinary overshoot is the spa having got there"
        assert _ready_at(c) == "Ready"

    def test_the_boundary_is_the_new_session_delta(self):
        """Two degrees over is the last reading that still reads as overshoot."""
        at_edge = MockCoordinator(water_temp=41.5, target_temp=39.5, near_target=False)
        _apply_temp_update(at_edge, new_temp=41.5, new_target=39.5)
        assert at_edge.ready_latched is True

        past_edge = MockCoordinator(water_temp=41.6, target_temp=39.5, near_target=False)
        _apply_temp_update(past_edge, new_temp=41.6, new_target=39.5)
        assert past_edge.ready_latched is False

    def test_a_restart_mid_heat_is_untouched(self):
        """Water below the setpoint never went near this branch, and still does not."""
        c = MockCoordinator(water_temp=28.5, target_temp=39.5, near_target=False)
        _apply_temp_update(c, new_temp=28.5, new_target=39.5)
        assert c.ready_latched is False
        assert c.near_target is False

    def test_the_second_poll_can_still_latch_normally(self):
        """Withholding applies to the first sample only, not to the session after it."""
        c = MockCoordinator(water_temp=28.5, target_temp=39.5, near_target=False)
        _apply_temp_update(c, new_temp=28.5, new_target=39.5)   # first sample, heating
        _apply_temp_update(c, new_temp=39.5, new_target=39.5)   # arrives for real
        assert c.ready_latched is True


class TestLoweredThermostatIsNotReady:
    """Turning the setpoint below the water is not the spa becoming ready.

    Observed 2026-08-20: a session was stopped by dropping the target from 39.5 to 20
    with the water at 32. That closes the gap without a watt of heating, and the spa
    latched Ready — on a tub that had never reached the target it was set to. The latch
    then held, because it only releases once the water falls _LATCH_COOL_OFF from the
    warmest point seen while latched.

    The latch means "it heated to target and is still dip-warm". Only heating earns it.
    """

    def test_lowering_the_setpoint_onto_the_water_does_not_latch(self):
        c = MockCoordinator(water_temp=32.0, target_temp=39.5, near_target=False)
        # The latch has seen the old target at least once, as it would in a live session.
        c._latch_target = 39.5
        _apply_temp_update(c, new_temp=32.0, new_target=20.0)
        assert c.ready_latched is False, "a lowered thermostat must not latch Ready"

    def test_it_is_still_near_target_though(self):
        """Warmer than asked is still warmer than asked — only the latch is withheld."""
        c = MockCoordinator(water_temp=32.0, target_temp=39.5, near_target=False)
        c._latch_target = 39.5
        _apply_temp_update(c, new_temp=32.0, new_target=20.0)
        assert c.near_target is True

    def test_heating_to_the_target_still_latches(self):
        """The case the latch exists for, with the target steady throughout."""
        c = MockCoordinator(water_temp=39.0, target_temp=39.5, near_target=False)
        c._latch_target = 39.5
        _apply_temp_update(c, new_temp=39.5, new_target=39.5)
        assert c.ready_latched is True

    def test_first_ever_poll_still_latches(self):
        """No previous target recorded must not be mistaken for one that moved."""
        c = MockCoordinator(water_temp=39.0, target_temp=39.5, near_target=False)
        assert c._latch_target is None
        _apply_temp_update(c, new_temp=39.5, new_target=39.5)
        assert c.ready_latched is True


class TestLoweredSetpointIsNotReady:
    """Stopping a session by turning the thermostat down must not read as Ready.

    Observed 2026-08-20: a session was stopped by dropping the target from 39.5 to 20
    with the water at 28.5, and Ready at said "Ready". The spa never reached the target
    it had been asked for — the target was moved onto water that happened to be warm.

    near_target alone cannot tell the two apart: it says only that the water is at or
    above the setpoint, which is true either way. How far above separates them, since
    thermostat overshoot is a fraction of a degree. The latch would also separate them,
    but it does not survive a restart, so it cannot be relied on alone — that is what
    made the 2026-08-14 overshoot bug possible.
    """

    def test_target_dropped_far_below_the_water_is_not_ready(self):
        c = MockCoordinator(water_temp=28.5, target_temp=20.0, near_target=True)
        assert _ready_at(c) != "Ready"

    def test_ordinary_overshoot_is_still_ready(self):
        """Half a degree past the setpoint is the spa having arrived, not a moved dial."""
        c = MockCoordinator(water_temp=40.0, target_temp=39.5, near_target=True)
        assert _ready_at(c) == "Ready"

    def test_overshoot_is_ready_even_without_the_latch(self):
        """The latch is lost on restart; overshoot must still read Ready without it."""
        c = MockCoordinator(water_temp=40.0, target_temp=39.5,
                            near_target=True, ready_latched=False)
        assert _ready_at(c) == "Ready"

    def test_exactly_at_target_is_ready(self):
        c = MockCoordinator(water_temp=39.5, target_temp=39.5, near_target=True)
        assert _ready_at(c) == "Ready"

    def test_a_latched_spa_stays_ready_however_far_the_dial_moved(self):
        """Having earned it, the latch governs — its own cool-off releases it later."""
        c = MockCoordinator(water_temp=28.5, target_temp=20.0,
                            near_target=True, ready_latched=True)
        assert _ready_at(c) == "Ready"


class TestManualHeatUnderAPendingSchedule:
    """Raising the thermostat while a schedule is pending must show *that* heat-up.

    Reported 2026-08-25: a schedule was set for several days out, the setpoint was
    raised by hand to heat the tub now, and Ready at went on reporting the scheduled
    day. It looked as though the setpoint change had been ignored, and the only way to
    see when the water would actually be warm was to cancel the schedule.

    Nothing was wrong underneath — the coordinator opens a real session for that change
    like any other, builds a plan and measures it. The schedule-pending branch of the
    display simply returned before the free-heating branch could be reached.

    A heat-up happening now outranks one scheduled for later, and the schedule is
    neither cancelled nor altered by being outranked.
    """

    def _pending(self, **kw):
        """A schedule three days out, far enough that its display is unmistakable."""
        return MockCoordinator(
            near_target=False, ready_latched=False,
            scheduled_ready_at=datetime.now(timezone.utc) + timedelta(days=3),
            schedule_target_temp=39.5,
            heat_rate=2.0,
            **kw,
        )

    def _session(self, c):
        """Mark a session open, the way the coordinator does when the setpoint jumps."""
        c._prediction = {
            "start_time": datetime.now(timezone.utc).isoformat(),
            "start_temp": 24.0,
            "target_temp": 39.5,
            "estimated_minutes": 480.0,
            "plan_rates": [1.21, 1.04, 0.86],
        }
        return c

    def test_the_scheduled_day_is_shown_while_nothing_is_heating(self):
        """The behaviour that must not change: a pending schedule with the thermostat
        parked low is still a pending schedule."""
        c = self._pending(water_temp=24.0, target_temp=20.0, heater="off")
        val = _ready_at(c)
        assert val is not None and "+3d" in val, val

    def test_maintenance_cycling_does_not_take_over_the_display(self):
        """The heater running to hold a low setpoint is not a heat-up.

        This is the case that keeps the branch quiet for the days a schedule usually
        spends pending: no session is open, so there is nothing to report but the
        schedule.
        """
        c = self._pending(water_temp=19.6, target_temp=20.0, heater="on")
        val = _ready_at(c)
        assert val is not None and "+3d" in val, val

    def test_a_manual_heat_up_replaces_the_scheduled_time(self):
        """The report: setpoint raised by hand, heater on, session open."""
        c = self._session(
            self._pending(water_temp=24.0, target_temp=39.5, heater="on"))
        val = _ready_at(c)
        assert val is not None, "a heat-up in progress must show a time"
        assert "+3d" not in val, f"still showing the schedule: {val}"
        assert re.match(r"^\d{2}:\d{2}", val), val

    def test_the_schedule_is_left_alone(self):
        """Outranked, not cancelled: the Heat Schedule sensor is unaffected, and the
        scheduled time returns once the manual session ends."""
        c = self._session(
            self._pending(water_temp=24.0, target_temp=39.5, heater="on"))
        before = c.scheduled_ready_at
        _ready_at(c)
        assert c.scheduled_ready_at == before
        assert c.schedule_target_temp == 39.5
        # Session over and the water long since cooled: the schedule drives the
        # display again. Cooled deliberately — at 39.5 the honest answer is "Ready",
        # because the tub is already at the temperature the schedule is aiming for,
        # and that would prove nothing about which branch produced it.
        c._prediction = None
        _apply_temp_update(c, new_temp=24.0, new_target=20.0)
        c._last_data["heater"] = "off"
        c.ready_latched = False
        assert "+3d" in (_ready_at(c) or ""), _ready_at(c)

    def test_almost_there_reads_ready_rather_than_a_time(self):
        """Within five minutes of the manual target, the same rule as free heating."""
        c = self._session(
            self._pending(water_temp=39.4, target_temp=39.5, heater="on"))
        assert _ready_at(c) == "Ready"
