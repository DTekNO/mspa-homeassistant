"""The thermal model wired into the coordinator.

test_thermal.py covers the arithmetic in isolation. This covers the wiring: that a
heating crossing teaches `A`, that a cooling crossing teaches `tau`, that both survive a
restart, and that a spa with no history behaves like a new one.

The coordinator is built with `object.__new__` and populated by hand, as the other
learning tests do, so the learning can be exercised without an event loop.

Run with: python -m pytest tests/test_thermal_learning.py -v
"""
import pytest

from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.thermal import (
    A_ALPHA, DEFAULT_A, DEFAULT_TAU_H, TAU_ALPHA,
)


def _coord(**over):
    c = object.__new__(MSpaUpdateCoordinator)
    c.ambient_temp = 12.0
    c.config_entry = type("E", (), {"options": {"heater_power_heat": 2200}})()
    for k, v in over.items():
        setattr(c, k, v)
    return c


class TestANewSpaStartsFromTheSeeds:
    """Deleting .storage must give a spa that predicts sensibly from the first poll."""

    def test_the_model_is_seeded_not_empty(self):
        m = _coord().thermal_model()
        assert m.a == DEFAULT_A and m.tau_h == DEFAULT_TAU_H

    def test_it_can_already_answer(self):
        """A blank Ready at on day one is a broken dashboard; a seeded one is not."""
        assert _coord().thermal_minutes(20.0, 39.0, ambient=12.0) > 0

    def test_the_class_defaults_survive_object_new(self):
        """The fixtures build coordinators this way. If these were only set in
        __init__ every learning test in the suite would fail at once — which is
        precisely what happened when they were."""
        c = object.__new__(MSpaUpdateCoordinator)
        assert (c.thermal_a, c.thermal_tau_h) == (None, None)
        assert (c.thermal_a_n, c.thermal_tau_n) == (0, 0)

    def test_diagnostics_say_they_are_seeded(self):
        d = _coord().thermal_diagnostics()
        assert d["seeded_a"] and d["seeded_tau"]
        assert d["a_samples"] == 0 and d["tau_samples"] == 0


class TestLearningAFromHeating:

    def test_the_first_sample_replaces_the_seed_outright(self):
        """A spa far from the seed must converge at once, not crawl towards it."""
        c = _coord()
        c.learn_from_crossing(rate=0.80, water_mean=25.0, ambient=12.0)
        assert c.thermal_a == pytest.approx(0.80 + 13.0 / DEFAULT_TAU_H)
        assert c.thermal_a_n == 1

    def test_later_samples_are_smoothed(self):
        """`A` is the run's average implied value, then smoothed into the estimate —
        not the latest crossing alone, which would make it as noisy as one reading."""
        c = _coord()
        c.learn_from_crossing(1.00, 25.0, 12.0)
        first = c.thermal_a
        c.learn_from_crossing(1.20, 25.0, 12.0)
        run_mean = ((1.00 + 13.0 / DEFAULT_TAU_H) + (1.20 + 13.0 / DEFAULT_TAU_H)) / 2
        assert c.thermal_a == pytest.approx(
            A_ALPHA * run_mean + (1 - A_ALPHA) * first)
        assert c.thermal_a_n == 2

    def test_an_absurd_sample_leaves_the_estimate_alone(self):
        """R7: discarded, not clamped — and discarded before it joins the run's points,
        or it would already be inside both the average and the slope."""
        c = _coord()
        c.learn_from_crossing(1.00, 25.0, 12.0)
        good = c.thermal_a
        c.learn_from_crossing(99.0, 25.0, 12.0)
        assert c.thermal_a == good and c.thermal_a_n == 1
        assert len(c._thermal_points) == 1, "the bad crossing was stored anyway"

    def test_missing_air_is_not_guessed_at(self):
        c = _coord()
        c.learn_from_crossing(1.00, 25.0, None)
        assert c.thermal_a is None

    def test_it_uses_the_learned_tau_not_the_seed(self):
        """A and tau are coupled through the subtraction, so a learned tau has to
        reach this or the two parameters describe different spas."""
        c = _coord(thermal_tau_h=20.0)
        c.learn_from_crossing(rate=0.80, water_mean=25.0, ambient=12.0)
        assert c.thermal_a == pytest.approx(0.80 + 13.0 / 20.0)


class TestLearningTauFromHeating:
    """R2: tau comes from the slope of a heat-up, never from cooling.

    R6: it updates only once the run has swung far enough in gap to constrain that
    slope. The threshold is measured — on 11.09.2026 the first eight crossings imply
    18 h and all forty imply 65.6 h, which matches an independent cooling measurement.
    """

    @staticmethod
    def _run(c, pairs):
        """Feed (water, air) crossings at a fixed rate implied by a true model."""
        for water, air, rate in pairs:
            c.learn_from_crossing(rate, water, air)

    def _synthetic(self, a, tau, waters, air=12.0):
        return [(w, air, a - (w - air) / tau) for w in waters]

    def test_a_short_lever_does_not_move_tau(self):
        """The failure R6 exists to prevent: confident, wrong, and silent."""
        c = _coord()
        self._run(c, self._synthetic(1.30, 55.0, [20.0, 21.0, 22.0]))
        assert c.thermal_tau_h is None, "tau moved on a 2 K spread"
        assert c.thermal_tau_n == 0

    def test_tau_does_not_move_during_a_run(self):
        """Mid-run the lever is partial and the fit is biased, not merely noisy:
        replaying 11.09.2026 the first twelve crossings imply 37 h where all twenty
        imply 66 h. Holding tau also means the displayed finish cannot jump because the
        model changed underneath it."""
        c = _coord()
        waters = [20.0 + 0.5 * i for i in range(40)]
        self._run(c, self._synthetic(1.30, 55.0, waters))
        assert c.thermal_tau_h is None, "tau moved before the run finished"
        assert c.thermal_tau_n == 0

    def test_the_completed_run_recovers_tau(self):
        c = _coord()
        waters = [20.0 + 0.5 * i for i in range(40)]     # 20 -> 39.5, ~19 K of gap
        self._run(c, self._synthetic(1.30, 55.0, waters))
        c.finalise_thermal_run()
        assert c.thermal_tau_h == pytest.approx(55.0, rel=0.02)
        assert c.thermal_tau_n == 1
        assert c._thermal_points == [], "the points outlived the run"

    def test_a_run_too_short_to_earn_a_fit_leaves_tau_alone(self):
        c = _coord()
        self._run(c, self._synthetic(1.30, 55.0, [20.0, 20.5, 21.0]))
        c.finalise_thermal_run()
        assert c.thermal_tau_h is None and c.thermal_tau_n == 0

    def test_a_recovers_alongside_it(self):
        c = _coord()
        waters = [20.0 + 0.5 * i for i in range(40)]
        self._run(c, self._synthetic(1.30, 55.0, waters))
        assert c.thermal_a == pytest.approx(1.30, rel=0.05)

    def test_a_is_learned_from_the_very_first_crossing(self):
        """The intercept needs no lever, which is why A moves and tau waits."""
        c = _coord()
        self._run(c, self._synthetic(1.30, 55.0, [20.0]))
        assert c.thermal_a is not None and c.thermal_a_n == 1
        assert c.thermal_tau_h is None

    def test_cooling_is_not_wired_into_learning(self):
        """R2. The function still exists as an independent check; nothing calls it."""
        import inspect
        from custom_components.mspa import coordinator as mod
        src = inspect.getsource(mod)
        assert "tau_from_cooling" not in src, (
            "cooling is feeding the model again — see R2 in docs/prediction-rules.md")

    def test_a_new_run_clears_the_points_but_keeps_the_parameters(self):
        c = _coord()
        waters = [20.0 + 0.5 * i for i in range(40)]
        self._run(c, self._synthetic(1.30, 55.0, waters))
        c.finalise_thermal_run()
        tau, a = c.thermal_tau_h, c.thermal_a
        c.reset_thermal_run()
        assert c._thermal_points == []
        assert (c.thermal_tau_h, c.thermal_a) == (tau, a)


class TestTheTwoParametersStayIndependent:
    """A is the intercept and moves with the water level; tau is the slope and moves
    with the spa. A run too short to constrain the slope must not drag it anyway."""

    def test_a_short_run_moves_a_and_leaves_tau_alone(self):
        c = _coord(thermal_tau_h=48.0, thermal_tau_n=3)
        c.learn_from_crossing(1.0, 25.0, 12.0)
        assert c.thermal_tau_h == 48.0 and c.thermal_tau_n == 3
        assert c.thermal_a is not None

    def test_a_uses_the_learned_tau_not_the_seed(self):
        c = _coord(thermal_tau_h=20.0)
        c.learn_from_crossing(rate=0.80, water_mean=25.0, ambient=12.0)
        assert c.thermal_a == pytest.approx(0.80 + 13.0 / 20.0)


class TestPersistence:

    def test_the_saved_shape_is_two_numbers_and_two_counts(self):
        """If this grows, the model has stopped being simple."""
        import inspect
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_data)
        for key in ("thermal_a", "thermal_tau_h", "thermal_a_n", "thermal_tau_n",
                    "thermal_points", "thermal_crossings_seen"):
            assert f'"{key}"' in src, key

    def test_a_store_without_the_keys_leaves_the_seeds_standing(self):
        """A cleared .storage, or one written before the model existed, must give
        new-spa behaviour rather than an exception."""
        stored = {"heat_rate": 0.9}
        c = _coord()
        c.thermal_a = stored.get("thermal_a")
        c.thermal_tau_h = stored.get("thermal_tau_h")
        c.thermal_a_n = stored.get("thermal_a_n", 0) or 0
        assert c.thermal_model().a == DEFAULT_A
        assert c.thermal_diagnostics()["seeded_a"] is True


class TestARunSurvivesARestart:
    """25.09.2026 08:42: a restart seventeen hours and nine chords into a run, with
    2.5 °C to go, left the run with nothing to fit tau from. Every hot deploy is a
    restart, so this is the ordinary case, not the edge."""

    STORED = {
        "active_prediction": {"start_temp": 21.5, "target_temp": 39.0},
        "thermal_points": [[8.8, 1.088], [10.6, 0.963], [12.8, 0.895]],
        "thermal_crossings_seen": 12,
    }

    def test_points_come_back_as_pairs(self):
        c = _coord()
        c.restore_thermal_run(self.STORED)
        assert c._thermal_points == [(8.8, 1.088), (10.6, 0.963), (12.8, 0.895)]

    def test_the_skip_is_not_repeated(self):
        """Twelve crossings in, the next crossing anchors a chord straight away."""
        c = _coord()
        c._window_amb_n = 0; c._window_amb_sum = 0.0
        c.restore_thermal_run(self.STORED)
        c.note_thermal_crossing(0.0, 36.5)
        assert [w for _, w, _ in c._thermal_chord] == [36.5]

    def test_the_chord_in_progress_is_not_restored(self):
        """Its anchor is on the monotonic clock, which a restart resets."""
        c = _coord()
        c.restore_thermal_run({**self.STORED, "thermal_chord": [[1.0, 36.0, 12.0]]})
        assert c._thermal_chord == []

    def test_a_store_without_the_keys_restores_nothing(self):
        c = _coord()
        c.restore_thermal_run({"active_prediction": {"start_temp": 21.5}})
        assert c._thermal_points == [] and c._thermal_crossings_seen == 0

    def test_corrupt_points_are_skipped_not_fatal(self):
        c = _coord()
        c.restore_thermal_run({**self.STORED, "thermal_points": [[8.8, 1.088], "x", [1]]})
        assert c._thermal_points == [(8.8, 1.088)]

    def test_the_restored_points_feed_the_tau_fit(self):
        """The point of the exercise: a run that spans a restart still earns its fit."""
        c = _coord()
        pts = [[g, 1.20 - g / 35.0] for g in range(7, 20)]        # 13 points, 12 K
        c.restore_thermal_run({**self.STORED, "thermal_points": pts})
        c.finalise_thermal_run()
        assert c.thermal_tau_h == pytest.approx(35.0, rel=0.02)


class TestDiagnostics:

    def test_implied_volume_and_loss_are_reported(self):
        c = _coord(thermal_a=1.30, thermal_tau_h=55.0)
        d = c.thermal_diagnostics()
        assert 1200 < d["implied_litres"] < 1700
        assert 15 < d["loss_w_per_k"] < 60

    def test_the_chord_view_is_derived_and_decreasing(self):
        d = _coord(thermal_a=1.30, thermal_tau_h=55.0).thermal_diagnostics()
        r = d["chord_rates"]
        assert r[0] > r[1] > r[2]

    def test_no_air_means_no_chords_rather_than_wrong_ones(self):
        d = _coord(ambient_temp=None).thermal_diagnostics()
        assert d["chord_rates"] is None and d["asymptote_c"] is None


class TestNoStepAtHandover:
    """R3: the scheduler and Ready at share a method, so the predicted finish must not
    move the instant the schedule hands over to heating.

    Measured on 10.09.2026 before this was fixed: the scheduler planned from an
    extrapolated 18.0 °C, and the moment the heater fired scheduling_temp's
    stale-direction guard returned the raw 18.5 instead — 24 minutes of jump between
    two callers of the same model.
    """

    def _coord_mid_dwell(self, hours=2.0):
        """A cooling dwell whose anchor is `hours` old.

        Two hours by default, not the thirteen this was first written with. At
        0.166 °C/h a band takes three hours, so thirteen is four bands of nothing
        crossing and `scheduling_temp`'s staleness guard now — correctly — refuses to
        extrapolate it at all. Two hours is a live dwell: the projection has travelled
        a third of a degree and has not yet saturated, which is the state this class is
        about. The thirteen-hour case has tests of its own below.
        """
        from datetime import datetime, timedelta, timezone
        c = _coord()
        now = datetime.now(timezone.utc)
        c._last_data = {"water_temperature": "18.5", "filter": "on"}
        c.temp_anchor_time = now - timedelta(hours=hours)
        c.temp_anchor_temp = 18.5
        c.temp_anchor_rising = False
        c.circulating_since = now - timedelta(hours=30)
        c.heating_since = None
        c.computed_cool_rate = 0.166
        c._anchor_prev_reading = 18.5
        c.temp_anchor_target = 39.0
        # Once rising, scheduling_temp extrapolates at a bucket rate, so the bucket
        # model's state has to be present even though nothing here is testing it.
        c._band_stats = {}
        c._band_observations = []
        c.heat_rate_buckets = [1.14, 1.00, 0.89]
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        c.ambient_baseline = 13.6
        c.computed_heat_rate = 1.0
        c.prediction_bias = 1.0
        return c

    def test_the_dwell_is_extrapolated_before_the_heater_starts(self):
        c = self._coord_mid_dwell()
        assert c.scheduling_temp() < 18.5, "the long dwell was not extrapolated at all"

    def test_the_position_survives_the_direction_change(self):
        """The water does not move when the heater switches on; only the direction
        it is about to move in does."""
        c = self._coord_mid_dwell()
        before = c.scheduling_temp()
        c.reanchor_for_direction_change()
        from datetime import datetime, timezone
        c.heating_since = datetime.now(timezone.utc)      # as the real transition does
        after = c.scheduling_temp()
        assert after == pytest.approx(before, abs=0.02), (
            f"plan temperature stepped {before:.3f} -> {after:.3f} at handover")

    def test_without_the_fix_the_guard_would_discard_it(self):
        """Pins the defect this exists for: set heating_since without re-anchoring and
        the guard falls back to the raw reading."""
        from datetime import datetime, timezone
        c = self._coord_mid_dwell()
        before = c.scheduling_temp()
        c.heating_since = datetime.now(timezone.utc)
        assert c.scheduling_temp() == 18.5
        assert before < 18.5

    def test_a_dwell_past_the_staleness_cap_is_not_extrapolated(self):
        """10.09.2026 itself: nothing crossed for sixteen hours, so there is no
        projection left to carry over and the reading is all we have."""
        c = self._coord_mid_dwell(hours=16.4)
        assert c.scheduling_temp() == 18.5

    def test_the_stale_case_still_does_not_step_at_handover(self):
        """R3 holds either way. Where the extrapolation is refused, both sides of the
        handover refuse it, so they agree on the reading instead of on a guess."""
        from datetime import datetime, timezone
        c = self._coord_mid_dwell(hours=16.4)
        before = c.scheduling_temp()
        c.reanchor_for_direction_change()
        c.heating_since = datetime.now(timezone.utc)
        assert c.scheduling_temp() == pytest.approx(before, abs=0.02)

    def test_the_reanchor_cannot_launder_a_stale_projection(self):
        """The defect the cap closes: re-anchoring stamps a fresh timestamp on the
        carried-over value, so a stale projection would become permanently young."""
        c = self._coord_mid_dwell(hours=16.4)
        c.reanchor_for_direction_change()
        assert c.temp_anchor_temp == pytest.approx(18.5), (
            "the band floor was laundered into a fresh anchor")

    def test_the_new_anchor_faces_the_right_way(self):
        c = self._coord_mid_dwell()
        c.reanchor_for_direction_change()
        assert c.temp_anchor_rising is True

    def test_nothing_happens_when_there_is_no_estimate(self):
        c = _coord()
        c._last_data = {}
        c.temp_anchor_temp = None
        c.reanchor_for_direction_change()
        assert c.temp_anchor_temp is None


class TestTauAndAMoveTogether:
    """`A` is learned all run as rate + gap/tau at the seed tau, so the two are a pair.
    Moving tau alone at the end of the run left `A` describing a slope the model no
    longer had. 18.09.2026: tau went 55 → 30.7 with `A` left at 1.243, and that pair
    predicted the very run it was fitted on at 1995 minutes against 1411 actual."""

    A_TRUE, TAU_TRUE, AIR = 1.20, 35.0, 12.0

    def _run(self):
        """A noise-free run at (A_TRUE, TAU_TRUE), learned crossing by crossing at the
        seed tau of 55 — which is exactly how the live run was learned."""
        c = _coord()
        for gap in range(7, 26):                          # 19 points, 18 K of spread
            rate = self.A_TRUE - gap / self.TAU_TRUE
            c.learn_from_crossing(rate=rate, water_mean=self.AIR + gap, ambient=self.AIR)
        assert c.thermal_a != pytest.approx(self.A_TRUE, abs=0.02), (
            "the setup is wrong: A learned at tau=55 should be biased before finalise")
        return c

    def test_finalise_recovers_both_parameters(self):
        c = self._run()
        c.finalise_thermal_run()
        assert c.thermal_tau_h == pytest.approx(self.TAU_TRUE, rel=0.02)
        assert c.thermal_a == pytest.approx(self.A_TRUE, abs=0.02)

    def test_the_stored_pair_predicts_the_run_it_was_fitted_on(self):
        """The property that was violated: a pair fitted on a run must reproduce it."""
        from custom_components.mspa.thermal import ThermalModel
        c = self._run()
        c.finalise_thermal_run()
        truth = ThermalModel(self.A_TRUE, self.TAU_TRUE).heating_minutes(19.0, 37.0, self.AIR)
        got = c.thermal_model().heating_minutes(19.0, 37.0, self.AIR)
        assert got == pytest.approx(truth, rel=0.03)

    def test_a_is_not_touched_when_tau_earns_no_fit(self):
        """Too short a lever: tau stays, and so must A — nothing to re-derive it at."""
        c = _coord()
        for gap in range(10, 14):                         # 3 K of spread, 4 points
            c.learn_from_crossing(rate=1.0, water_mean=self.AIR + gap, ambient=self.AIR)
        before = c.thermal_a
        c.finalise_thermal_run()
        assert c.thermal_tau_h is None and c.thermal_a == before
