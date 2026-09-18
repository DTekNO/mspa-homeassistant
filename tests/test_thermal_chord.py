"""The chord the thermal model measures its rate over, and the hold on the opening plan.

Both exist because a 0.5 °C crossing is not a measurement. The reading is quantised to
half a degree, so one crossing is a rise known to +/- 0.25 over 25-30 minutes — a 50%
error on the rate, blended straight into `A` and republished. Replaying 11.09.2026 the
first such chord read A = 1.46 against the run's settled 1.25 and opened the estimate
206 minutes fast.

Scored over the two recorded September runs, holding the anchor to 1.5 °C takes mean
absolute error from 38/41 minutes to 25/28 and the worst single step from 169/137 to
22/27. Holding the *displayed* estimate until that first chord completes takes 03.09's
worst step from 123 minutes to 27.

Run with: python -m pytest tests/test_thermal_chord.py -v
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.thermal import (
    DEFAULT_A, THERMAL_CHORD_MIN_C, THERMAL_CHORD_SKIP_CROSSINGS,
)


def _coord(**over):
    c = object.__new__(MSpaUpdateCoordinator)
    c.ambient_temp = 12.0
    c.config_entry = type("E", (), {"options": {"heater_power_heat": 2200}})()
    c._window_amb_n = 0
    c._window_amb_sum = 0.0
    c._thermal_points = []
    c._thermal_chord = []
    c._thermal_crossings_seen = 0
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _walk(c, temps, *, minutes=27.0, start=0.0):
    """Feed crossings `minutes` apart, as the coordinator's monotonic clock would."""
    t = start
    c.anchor_thermal_chord(t, temps[0])
    for temp in temps[1:]:
        t += minutes * 60.0
        c.record_thermal_crossing(t, temp)
    return c


class TestTheChordIsHeldUntilItIsWorthLearningFrom:

    def test_one_crossing_teaches_nothing(self):
        c = _walk(_coord(), [20.0, 20.5])
        assert c.thermal_a is None and c._thermal_points == []

    def test_two_crossings_still_teach_nothing(self):
        c = _walk(_coord(), [20.0, 20.5, 21.0])
        assert c.thermal_a is None and c._thermal_points == []

    def test_the_chord_completes_at_the_third(self):
        """20.0 -> 21.5 is 1.5 °C, which is THERMAL_CHORD_MIN_C."""
        c = _walk(_coord(), [20.0, 20.5, 21.0, 21.5])
        assert len(c._thermal_points) == 1
        assert c.thermal_a is not None

    def test_the_gap_between_crossings_is_used_not_skipped(self):
        """The whole point: one rate measured over 1.5 °C, not three over 0.5 each."""
        c = _walk(_coord(), [20.0, 20.5, 21.0, 21.5], minutes=30.0)
        gap, rate = c._thermal_points[0]
        assert rate == pytest.approx(1.5 / 1.5)          # 1.5 °C over 90 minutes
        assert gap == pytest.approx(20.75 - 12.0)        # midpoint of the whole chord

    def test_chords_do_not_overlap(self):
        """Each chord starts where the last one ended, so no data is counted twice."""
        c = _walk(_coord(), [20.0, 20.5, 21.0, 21.5, 22.0, 22.5, 23.0])
        assert len(c._thermal_points) == 2
        assert c._thermal_chord[0][1] == 23.0

    def test_the_anchor_carries_across_a_fit(self):
        c = _walk(_coord(), [20.0, 20.5, 21.0, 21.5, 22.0])
        assert c._thermal_chord[0][1] == 21.5, "the completed chord's end is the next anchor"


class TestTheFirstBandsAreDiscarded:
    """The probe sits in the pump housing and sees heated water before the tub has
    mixed. On 17.09.2026 the first band after heater-on ran at 1.82 °C/h against a
    settled ~1.1, the chord that included it read A = 1.56, and the first learned
    estimate was four hours early. Replayed with the hold working: anchor at the 1st
    crossing and the first snap is 259 minutes; at the 3rd it is 25."""

    def _note(self, c, temps, minutes=27.0):
        t = 0.0
        for temp in temps:
            c.note_thermal_crossing(t, temp); t += minutes * 60.0

    def test_the_first_two_crossings_anchor_nothing(self):
        c = _coord()
        self._note(c, [18.0, 18.5])
        assert c._thermal_chord == [] and c._thermal_points == []

    def test_the_third_crossing_anchors(self):
        c = _coord()
        self._note(c, [18.0, 18.5, 19.0])
        assert [w for _, w, _ in c._thermal_chord] == [19.0]

    def test_the_first_chord_runs_from_the_third_crossing(self):
        """19.0 -> 20.5 is the first 1.5 °C measured; 18.0 -> 19.0 never enters it."""
        c = _coord()
        self._note(c, [18.0, 18.5, 19.0, 19.5, 20.0, 20.5])
        assert len(c._thermal_points) == 1
        gap, rate = c._thermal_points[0]
        assert rate == pytest.approx(1.5 / (3 * 27 / 60))
        assert gap == pytest.approx(19.75 - 12.0)

    def test_a_fast_first_band_does_not_reach_the_fit(self):
        """The 17.09.2026 shape: 16.5 min for the first band, then ~25."""
        c = _coord()
        t = 0.0
        for temp, mins in ((18.0, 0), (18.5, 16.5), (19.0, 21.5), (19.5, 25.0),
                           (20.0, 24.5), (20.5, 27.0)):
            t += mins * 60.0
            c.note_thermal_crossing(t, temp)
        gap, rate = c._thermal_points[0]
        assert rate == pytest.approx(1.5 / ((25.0 + 24.5 + 27.0) / 60), rel=1e-6)

    def test_the_count_resets_with_the_run(self):
        c = _coord()
        self._note(c, [18.0, 18.5, 19.0])
        c.reset_thermal_run()
        assert c._thermal_crossings_seen == 0 and c._thermal_chord == []

    def test_it_is_two(self):
        assert THERMAL_CHORD_SKIP_CROSSINGS == 2

    def test_a_run_still_yields_enough_points_for_tau(self):
        from custom_components.mspa.thermal import TAU_MIN_POINTS
        c = _coord()
        self._note(c, [19.0 + 0.5 * i for i in range(41)])      # 11.09.2026
        assert len(c._thermal_points) >= TAU_MIN_POINTS


class TestTheChordIsMeasuredFromAnObservedPosition:

    def test_anchoring_discards_a_part_measured_chord(self):
        """The run opens somewhere inside a band and the position is never observed, so
        a rate measured from it spans an unknown distance."""
        c = _walk(_coord(), [20.0, 20.5, 21.0])
        c.anchor_thermal_chord(9999.0, 21.0)
        assert len(c._thermal_chord) == 1 and c._thermal_chord[0][1] == 21.0

    def test_a_chord_with_no_elapsed_time_is_dropped_not_divided_by(self):
        c = _coord()
        c.anchor_thermal_chord(100.0, 20.0)
        c.record_thermal_crossing(100.0, 21.5)
        assert c._thermal_points == []

    def test_an_unreadable_crossing_is_ignored(self):
        c = _walk(_coord(), [20.0, 20.5])
        c.record_thermal_crossing(5000.0, None)
        assert len(c._thermal_chord) == 2


class TestTheOpeningEstimateIsHeld:

    def _heating(self, **over):
        c = _coord(**over)
        c.heating_since = datetime.now(timezone.utc)
        return c

    def test_nothing_is_held_before_the_run_starts(self):
        assert _coord(heating_since=None).thermal_hold_finish(39.0) is None

    def test_the_hold_survives_while_nothing_has_been_measured(self):
        c = self._heating()
        c._thermal_hold_finish = datetime.now(timezone.utc) + timedelta(hours=20)
        c._thermal_hold_target = 39.0
        assert c.thermal_hold_finish(39.0) is not None

    def test_it_is_released_the_moment_a_rate_is_learned(self):
        c = self._heating()
        c._thermal_hold_finish = datetime.now(timezone.utc) + timedelta(hours=20)
        c._thermal_hold_target = 39.0
        c.learn_from_crossing(rate=1.0, water_mean=25.0, ambient=12.0)
        assert c.thermal_a is not None
        assert c.thermal_hold_finish(39.0) is None

    def test_a_completed_chord_releases_it(self):
        """End to end: the hold lasts exactly as long as the first chord."""
        c = self._heating()
        c._thermal_hold_finish = datetime.now(timezone.utc) + timedelta(hours=20)
        c._thermal_hold_target = 39.0
        _walk(c, [20.0, 20.5, 21.0])
        assert c.thermal_hold_finish(39.0) is not None, "released before it learned"
        c.record_thermal_crossing(3 * 27 * 60.0, 21.5)
        assert c.thermal_hold_finish(39.0) is None

    def test_moving_the_setpoint_releases_it(self):
        """A held plan is a plan to somewhere else once the target moves."""
        c = self._heating()
        c._thermal_hold_finish = datetime.now(timezone.utc) + timedelta(hours=20)
        c._thermal_hold_target = 39.0
        assert c.thermal_hold_finish(40.0) is None

    def test_the_heater_stopping_releases_it(self):
        c = self._heating()
        c._thermal_hold_finish = datetime.now(timezone.utc) + timedelta(hours=20)
        c._thermal_hold_target = 39.0
        c.heating_since = None
        assert c.thermal_hold_finish(39.0) is None

    def test_a_spa_that_has_heated_before_never_holds(self):
        """Inert on every run after the first — there is a measured `A` to plan with."""
        c = self._heating(thermal_a=1.25)
        c.begin_thermal_hold(18.5, 39.0)
        assert c._thermal_hold_finish is None


class TestTheHoldSurvivesTheTransition:
    """17.09.2026: the hold was logged at 14:43:53 and the display had left it by
    14:44:55. The heater transition begins the hold and then, a few lines later on the
    same poll, resets the run for the new session — and reset released the hold."""

    def _held(self):
        c = _coord()
        c.heating_since = datetime.now(timezone.utc)
        c._thermal_hold_finish = datetime.now(timezone.utc) + timedelta(hours=20)
        c._thermal_hold_target = 39.0
        return c

    def test_resetting_the_run_does_not_release_it(self):
        c = self._held()
        c.reset_thermal_run()
        assert c.thermal_hold_finish(39.0) is not None

    def test_the_transition_does_not_overwrite_the_schedulers_hold(self):
        """The scheduler's finish is the plan it committed to; a recomputation at the
        transition — from a position the setpoint change may just have discarded — is
        a second opinion and must lose."""
        c = self._held()
        c.config_entry = type("E", (), {"options": {
            "heater_power_heat": 2200, "prediction_model": "thermal"}})()
        theirs = c._thermal_hold_finish
        c.begin_thermal_hold(17.5, 39.0)
        assert c._thermal_hold_finish is theirs

    def test_adopting_sets_exactly_the_schedulers_finish(self):
        c = _coord()
        c.config_entry = type("E", (), {"options": {
            "heater_power_heat": 2200, "prediction_model": "thermal"}})()
        c.heating_since = datetime.now(timezone.utc)
        due = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
        c.adopt_thermal_hold(due, 39.0)
        assert c.thermal_hold_finish(39.0) == due

    def test_adopting_with_a_learned_rate_is_a_no_op(self):
        c = _coord(thermal_a=1.25)
        c.config_entry = type("E", (), {"options": {
            "heater_power_heat": 2200, "prediction_model": "thermal"}})()
        c.adopt_thermal_hold(datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc), 39.0)
        assert c._thermal_hold_finish is None


class TestBeginningTheHold:

    def _fresh(self):
        c = _coord()
        c.heating_since = datetime.now(timezone.utc)
        c.config_entry = type("E", (), {"options": {
            "heater_power_heat": 2200, "prediction_model": "thermal"}})()
        return c

    def test_it_prices_the_hold_from_the_seed(self):
        c = self._fresh()
        c.begin_thermal_hold(18.5, 39.0)
        held = c.thermal_hold_finish(39.0)
        assert held is not None
        minutes = (held - datetime.now(timezone.utc)).total_seconds() / 60.0
        assert minutes == pytest.approx(
            c.thermal_minutes(18.5, 39.0, ambient=12.0), abs=1.0)

    def test_a_target_at_or_below_the_water_holds_nothing(self):
        c = self._fresh()
        c.begin_thermal_hold(39.0, 39.0)
        assert c._thermal_hold_finish is None

    def test_missing_inputs_hold_nothing(self):
        c = self._fresh()
        c.begin_thermal_hold(None, 39.0)
        c.begin_thermal_hold(18.5, None)
        assert c._thermal_hold_finish is None


class TestTheChordLengthIsTheOneThatWasScored:

    def test_it_is_one_and_a_half_degrees(self):
        """2.0 and 3.0 °C score better on error and were rejected: they leave 10/9 and
        6/6 points for the end-of-run `tau` fit, and TAU_MIN_POINTS is 10."""
        assert THERMAL_CHORD_MIN_C == 1.5

    def test_a_run_still_yields_enough_points_for_tau(self):
        from custom_components.mspa.thermal import TAU_MIN_POINTS
        c = _coord()
        temps = [19.0 + 0.5 * i for i in range(41)]      # the 11.09.2026 run
        _walk(c, temps)
        assert len(c._thermal_points) >= TAU_MIN_POINTS
