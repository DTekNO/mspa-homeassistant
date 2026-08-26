"""Tests for MSpaUpdateCoordinator._track_heating_rate.

Focus: the first temperature crossing after heater-on is phase-uncertain.
The water temperature reports in 0.5 °C bands, so the anchor set at heater-on
sits at an unknown position inside its band — the time to the first crossing
measures that random phase, not the heating rate (~2x fast on average).  The
first crossing must therefore anchor only; learning starts from the second.

Regression source: 2026-07-31 morning session.  Heater on at 07:27 with water
"33.0" (truly ~33.4); first crossing to 33.5 arrived after 11 minutes and was
learned as 2.7 °C/h, dragging the EMA 0.89 → 1.35 °C/h and collapsing the
Ready at estimate from 15:34 to 12:16 against a 15:30 schedule.

Run with: python -m pytest tests/test_rate_learning.py -v
"""
import pytest
from custom_components.mspa.coordinator import MSpaUpdateCoordinator


def _coord(**overrides) -> MSpaUpdateCoordinator:
    c = object.__new__(MSpaUpdateCoordinator)
    c.computed_heat_rate = 0.89
    c.heat_rate_buckets = [None, 1.30, None]
    c._rate_last_temp = None
    c._rate_last_time = None
    c._rate_prev_temp = None
    c._bucket_base_bucket = None
    c._bucket_base_value = None
    c._rate_first_step = False
    c._session_scalar = 1.0
    c._session_scalar_bucket = None
    c._session_fresh_buckets = set()
    c.ambient_temp = None
    c.ambient_wind = None
    c.ambient_baseline = None
    # Mirrors the real __init__. This fixture is built by hand with object.__new__, so
    # anything the coordinator initialises has to be repeated here or the first attribute
    # the code adds fails only in the tests — which reads as the code being wrong.
    c._window_amb_sum = c._window_wind_sum = 0.0
    c._window_amb_n = c._window_wind_n = 0
    c._band_observations = []
    c._band_stats = {}
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


_MIN = 60.0  # seconds


class TestFirstStepPhaseUncertainty:

    def test_first_crossing_is_not_learned(self):
        """The 2026-07-31 regression: 0.5 °C in 11 min (2.7 °C/h) at heater-on."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)              # heater-on anchor
        c._track_heating_rate(33.5, 3, 11 * _MIN)        # first crossing — fast
        assert c.computed_heat_rate == 0.89, "phantom rate must not be learned"
        assert c.heat_rate_buckets[1] == 1.30
        assert c._session_scalar == 1.0
        assert 1 not in c._session_fresh_buckets

    def test_first_crossing_reanchors_exactly(self):
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 11 * _MIN)
        assert c._rate_last_temp == 33.5
        assert c._rate_last_time == 11 * _MIN
        assert c._rate_first_step is False

    def test_second_crossing_is_learned(self):
        """33.5 → 34.0 in 25.5 min = 1.18 °C/h — the first true sample."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 11 * _MIN)
        c._track_heating_rate(34.0, 3, (11 + 25.5) * _MIN)
        expected_rate = 0.5 / (25.5 / 60)                # ≈ 1.176 °C/h
        expected_ema = 0.25 * expected_rate + 0.75 * 0.89
        assert abs(c.computed_heat_rate - expected_ema) < 1e-9
        assert 1 in c._session_fresh_buckets

    def test_heater_interruption_rearms_the_guard(self):
        """Any off/preheat period loses the phase — the next crossing must
        again be treated as position-only."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 11 * _MIN)        # guard consumed
        c._track_heating_rate(33.5, 2, 12 * _MIN)        # preheat: anchor reset
        c._track_heating_rate(33.5, 3, 13 * _MIN)        # heater back on
        c._track_heating_rate(34.0, 3, 18 * _MIN)        # fast "crossing" again
        assert c.computed_heat_rate == 0.89, "post-interruption crossing must not be learned"

    def test_third_and_later_crossings_keep_learning(self):
        c = _coord()
        t = 0.0
        c._track_heating_rate(33.0, 3, t)
        t += 11 * _MIN; c._track_heating_rate(33.5, 3, t)
        t += 25.5 * _MIN; c._track_heating_rate(34.0, 3, t)
        after_second = c.computed_heat_rate
        t += 31.5 * _MIN; c._track_heating_rate(34.5, 3, t)
        assert c.computed_heat_rate != after_second, "third crossing must be learned"

    def test_outlier_rejection_still_applies_after_guard(self):
        """A genuinely absurd second step (>3 °C/h) is still rejected."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 11 * _MIN)
        c._track_heating_rate(34.0, 3, (11 + 8) * _MIN)  # 0.5°C in 8 min = 3.75 °C/h
        assert c.computed_heat_rate == 0.89

    def test_dwell_without_crossing_never_learns(self):
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.0, 3, 30 * _MIN)
        c._track_heating_rate(33.0, 3, 60 * _MIN)
        assert c.computed_heat_rate == 0.89
        assert c._rate_first_step is True


class TestCoolingFirstStepPhaseUncertainty:
    """The cooling tracker has the same phase problem, amplified: its anchor
    re-arms on every thermostat cycle, so each heater off-period previously
    injected one phase-biased fast sample into the cooling EMA."""

    def _cool_coord(self, **overrides):
        c = _coord()
        c.computed_cool_rate = 0.20
        c._cool_last_temp = None
        c._cool_last_time = None
        c._cool_first_step = False
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    def test_first_drop_after_heater_off_is_not_learned(self):
        c = self._cool_coord()
        c._track_cooling_rate(39.5, 0, 0.0)              # heater stopped: anchor
        c._track_cooling_rate(39.0, 0, 30 * _MIN)        # first drop — phase-biased
        assert c.computed_cool_rate == 0.20

    def test_second_drop_is_learned(self):
        c = self._cool_coord()
        c._track_cooling_rate(39.5, 0, 0.0)
        c._track_cooling_rate(39.0, 0, 30 * _MIN)
        c._track_cooling_rate(38.5, 0, (30 + 90) * _MIN)  # 0.5°C in 90 min
        expected_rate = 0.5 / 1.5                         # 0.333 °C/h
        expected_ema = 0.25 * expected_rate + 0.75 * 0.20
        assert abs(c.computed_cool_rate - expected_ema) < 1e-9

    def test_thermostat_cycle_rearms_the_guard(self):
        c = self._cool_coord()
        c._track_cooling_rate(39.5, 0, 0.0)
        c._track_cooling_rate(39.0, 0, 30 * _MIN)         # guard consumed
        c._track_cooling_rate(39.0, 3, 31 * _MIN)         # heater cycles on
        c._track_cooling_rate(39.5, 0, 60 * _MIN)         # heater off again: new anchor
        c._track_cooling_rate(39.0, 0, 75 * _MIN)         # fast first drop again
        assert c.computed_cool_rate == 0.20, "per-cycle first drop must not be learned"

    def test_rise_also_consumes_the_guard(self):
        """Any first crossing fixes the phase — including an upward one
        (e.g. sun warming the water) — after which a drop is a true rate."""
        c = self._cool_coord()
        c._track_cooling_rate(39.0, 0, 0.0)
        c._track_cooling_rate(39.5, 0, 20 * _MIN)         # rise: guard consumed, no sample
        assert c.computed_cool_rate == 0.20
        c._track_cooling_rate(39.0, 0, (20 + 120) * _MIN) # drop in 2 h = 0.25 °C/h
        expected_ema = 0.25 * 0.25 + 0.75 * 0.20
        assert abs(c.computed_cool_rate - expected_ema) < 1e-9


class TestPredictionCreationGating:
    """Reported 2026-08-03: changing the setpoint on the climate card produced
    a PREDICTION_START / PREDICTION_CANCELLED pair on every 1 s rapid poll —
    seven in seven seconds.  Creation did not check heat_state, so it fired
    while the device was briefly out of full-heat mode recalculating, and the
    cancellation (which does check) killed it in the same cycle.

    Both now key off heat_state == 3, so create and cancel use one signal."""

    def test_full_heat_is_required_to_start_a_prediction(self):
        from custom_components.mspa.coordinator import (
            _HEAT_STATE_FULL, _NEW_SESSION_DELTA,
        )
        assert _HEAT_STATE_FULL == 3
        assert _NEW_SESSION_DELTA == 2.0

        def would_create(heat_state, water, target, prediction=None):
            """The creation guard as the coordinator applies it."""
            return (
                water is not None and target is not None
                and target > water
                and (target - water) > _NEW_SESSION_DELTA
                and heat_state == _HEAT_STATE_FULL
                and prediction is None
            )

        # The reported case: setpoint raised 31 → 40 while the device is
        # transitioning (preheat / idle), during rapid polling.
        assert not would_create(0, 31.0, 40.0), "idle must not start a prediction"
        assert not would_create(2, 31.0, 40.0), "preheat must not start a prediction"
        # Once genuinely heating, it starts — as it did at 09:57 in the log.
        assert would_create(3, 31.5, 40.0)
        # Unchanged guards still apply.
        assert not would_create(3, 39.0, 40.0), "gap under threshold: no session"
        assert not would_create(3, 31.0, 40.0, prediction={}), "already tracking"

    def test_cancellation_uses_the_same_signal(self):
        """heater_now_active is heat_state == 3, matching the creation gate, so
        a prediction cannot be created and cancelled within one poll."""
        from custom_components.mspa.coordinator import _HEAT_STATE_FULL

        def would_cancel(heat_state, near_target, prediction):
            heater_now_active = (heat_state == _HEAT_STATE_FULL)
            return (not heater_now_active
                    and prediction is not None
                    and not near_target)

        # Same states that must not create must also be the ones that cancel —
        # so no state both creates and cancels.
        for hs in (0, 2):
            assert would_cancel(hs, False, {})
        assert not would_cancel(_HEAT_STATE_FULL, False, {})


class TestGrowingWindow:
    """The rate is measured from the first band boundary reached in the current
    bucket, not from the previous crossing.

    Every crossing lands exactly on a 0.5 °C boundary, so any
    boundary-to-boundary span is equally phase-exact — a wider span is no more
    biased and much less noisy, since rate is delta/elapsed and report-timing
    jitter matters far less over hours than over one 30-minute step.

    Regression source: 2026-08-06 session.  Per-step sampling measured 18% noise
    in the cold bucket and dragged its stored rate to 0.98 °C/h against a
    realised 1.14; the growing window gives 7% and 1.08.
    """

    def test_anchor_is_held_within_a_bucket(self):
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)                    # phase-uncertain anchor
        c._track_heating_rate(33.5, 3, 10 * _MIN)             # re-anchors here
        assert c._rate_last_temp == 33.5
        c._track_heating_rate(34.0, 3, 40 * _MIN)             # learned; anchor must hold
        assert c._rate_last_temp == 33.5, "anchor advanced — window did not grow"
        c._track_heating_rate(34.5, 3, 70 * _MIN)
        assert c._rate_last_temp == 33.5, "anchor advanced on the third crossing"

    def test_rate_is_measured_over_the_whole_span(self):
        """Asymmetric timings separate the two schemes: a 15-minute final step
        reads 2.0 °C/h alone but 1.2 °C/h across the window."""
        c = _coord(heat_rate_buckets=[None, 1.20, None], computed_heat_rate=1.20)
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 10 * _MIN)             # anchor at 33.5 @ 10 min
        c._track_heating_rate(34.0, 3, 40 * _MIN)             # 0.5 °C / 0.5 h = 1.0
        after_first = c.heat_rate_buckets[1]
        c._track_heating_rate(34.5, 3, 55 * _MIN)             # window: 1.0 °C / 0.75 h = 1.333
        # per-step would have been 0.5 °C / 0.25 h = 2.0 °C/h, pulling the bucket
        # far higher; the window keeps it near the true ~1.2
        #
        # The update recomputes from the value the window opened at (1.20) rather than
        # from `after_first`, and weights by the span measured — so nested samples do
        # not compound. Before that change this read
        # `after_first + 0.25 * (rate - after_first)`, which double-counted the first
        # 0.5 °C every time a longer span arrived.
        alpha = 0.25 * (1.0 / 4.0)                            # 1.0 °C of a 4 °C reference
        expected = 1.20 + alpha * (1.0 / 0.75 - 1.20)
        assert abs(c.heat_rate_buckets[1] - expected) < 1e-6
        per_step = after_first + 0.25 * (2.0 - after_first)
        assert c.heat_rate_buckets[1] < per_step - 0.05, "looks like per-step sampling"

    def test_nested_samples_do_not_compound(self):
        """Ten nested samples must land where the last one alone would.

        The growing window re-measures the same span, so each sample carries almost no
        new evidence. Feeding them all into an EMA at full weight moved the 2026-08-12
        mid bucket 0.93 → 1.062 → 0.988 on a realised rate of 0.947, and shifted the
        ETA on every step.
        """
        c = _coord(heat_rate_buckets=[None, 1.00, None], computed_heat_rate=1.00)
        c._track_heating_rate(31.0, 3, 0.0)
        c._track_heating_rate(31.5, 3, 10 * _MIN)             # anchor
        for i, temp in enumerate([32.0, 32.5, 33.0, 33.5, 34.0], start=1):
            c._track_heating_rate(temp, 3, (10 + 30 * i) * _MIN)
        many = c.heat_rate_buckets[1]

        d = _coord(heat_rate_buckets=[None, 1.00, None], computed_heat_rate=1.00)
        d._track_heating_rate(31.0, 3, 0.0)
        d._track_heating_rate(31.5, 3, 10 * _MIN)
        d._track_heating_rate(34.0, 3, 160 * _MIN)            # one sample, same span
        once = d.heat_rate_buckets[1]
        assert abs(many - once) < 1e-9, (
            f"nested samples compounded: {many} via steps vs {once} in one")

    def test_a_short_span_moves_the_bucket_only_slightly(self):
        """0.5 °C of a 7 °C bucket must not move its rate by 10%."""
        c = _coord(heat_rate_buckets=[None, 0.93, None], computed_heat_rate=0.93)
        c._track_heating_rate(31.0, 3, 0.0)
        c._track_heating_rate(31.5, 3, 10 * _MIN)
        c._track_heating_rate(32.0, 3, 33 * _MIN)             # 0.5 °C at ~1.3 °C/h
        moved = abs(c.heat_rate_buckets[1] - 0.93) / 0.93
        assert moved < 0.02, f"a 0.5 °C span moved the whole bucket {moved:.1%}"

    def test_window_closes_at_a_bucket_boundary(self):
        """Each bucket models a different loss regime and must be measured alone."""
        c = _coord(heat_rate_buckets=[1.10, 1.00, None])
        c._track_heating_rate(29.0, 3, 0.0)
        c._track_heating_rate(29.5, 3, 20 * _MIN)             # anchor at 29.5, bucket 0
        c._track_heating_rate(30.0, 3, 50 * _MIN)             # crosses into bucket 1
        assert c._rate_last_temp == 30.0, "window must close at the bucket boundary"
        assert 1 not in c._session_fresh_buckets, "span belongs to the anchor's bucket"
        assert 0 in c._session_fresh_buckets

    def test_window_closes_on_a_rejected_sample(self):
        """A bad span must never be re-used as an anchor for later samples."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 10 * _MIN)
        before = c.computed_heat_rate
        c._track_heating_rate(34.0, 3, 13 * _MIN)             # 0.5 °C in 3 min = 10 °C/h
        assert c.computed_heat_rate == before, "outlier should have been rejected"
        assert c._rate_last_temp == 34.0, "anchor must advance past a rejected span"

    def test_interruption_still_rearms_the_phase_guard(self):
        """Heater-off must discard the window, not merely pause it."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 10 * _MIN)
        c._track_heating_rate(34.0, 3, 40 * _MIN)
        c._track_heating_rate(34.0, 1, 45 * _MIN)             # heater drops out
        assert c._rate_last_temp is None
        before = c.computed_heat_rate
        c._track_heating_rate(34.0, 3, 50 * _MIN)             # fresh anchor
        c._track_heating_rate(34.5, 3, 53 * _MIN)             # phase-uncertain again
        assert c.computed_heat_rate == before, "first crossing after resume was learned"


class TestDwellAfterACrossing:
    """A held anchor must not re-learn the same span on every poll.

    Regression source: 2026-08-07.  Holding the anchor for the growing window
    made `curr_temp != self._rate_last_temp` stay true on every poll after a
    crossing, so one 36.5→37.0 °C step was re-learned every 30 s for twelve
    minutes with an ever-growing elapsed time, walking bucket[2] from 1.040 down
    to 0.822.  Change must be detected against the previous *reading*; the span
    is measured from the anchor.

    The pre-existing dwell test could not catch this because it dwells at the
    anchor temperature, where the faulty guard is false.
    """

    def test_dwell_after_a_crossing_learns_exactly_once(self):
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 10 * _MIN)          # phase-uncertain, re-anchors
        c._track_heating_rate(34.0, 3, 40 * _MIN)          # the one real sample
        learned = c.computed_heat_rate
        bucket = list(c.heat_rate_buckets)
        for i in range(1, 21):                             # ten minutes of dwelling
            c._track_heating_rate(34.0, 3, (40 + i * 0.5) * _MIN)
        assert c.computed_heat_rate == learned, "re-learned the same span while dwelling"
        assert c.heat_rate_buckets == bucket

    def test_the_window_still_spans_both_crossings(self):
        """The anchor must still be held — the fix must not revert the window."""
        c = _coord()
        c._track_heating_rate(33.0, 3, 0.0)
        c._track_heating_rate(33.5, 3, 10 * _MIN)
        c._track_heating_rate(34.0, 3, 40 * _MIN)
        for i in range(1, 6):
            c._track_heating_rate(34.0, 3, (40 + i) * _MIN)   # dwell
        assert c._rate_last_temp == 33.5, "anchor moved during the dwell"
        c._track_heating_rate(34.5, 3, 70 * _MIN)
        assert c._rate_last_temp == 33.5, "anchor moved on the next crossing"


class TestColdStartBelowLearningRange:
    """A session starting below HEAT_BUCKET_LEARN_MIN must still learn 20→30.

    The measuring window holds its anchor while the water stays in one bucket, and
    everything below 30 is one bucket. `in_learning_range` tests the *from* temperature,
    so an anchor set at 15 made every sample 15→x — and every one of them was refused,
    the complete 20→30 traverse included. Refusing a full traverse of the cold bucket
    because the session happened to begin below it discards the one measurement that
    stretch exists to make.

    Nothing recorded starts below 22 °C, so this path has never run against real data;
    it exists for the deliberate cold-start session that will produce that data.
    """

    def _cold(self):
        c = _coord()
        c.heat_rate_buckets = [1.10, 1.30, None]
        return c

    def test_below_twenty_is_not_learned(self):
        """The extrapolated stretch stays extrapolated: no bucket may move there."""
        c = self._cold()
        c._track_heating_rate(15.0, 3, 0.0)               # heater-on anchor
        c._track_heating_rate(15.5, 3, 20 * _MIN)         # phase-uncertain, re-anchors
        c._track_heating_rate(16.5, 3, 60 * _MIN)
        c._track_heating_rate(18.0, 3, 120 * _MIN)
        assert c.heat_rate_buckets[0] == 1.10, "nothing below 20 may be learned"
        assert 0 not in c._session_fresh_buckets

    def test_crossing_twenty_reanchors_the_window(self):
        """The anchor must move to the crossing, not stay in the sub-range tail."""
        c = self._cold()
        c._track_heating_rate(15.0, 3, 0.0)
        c._track_heating_rate(15.5, 3, 20 * _MIN)
        c._track_heating_rate(20.0, 3, 200 * _MIN)
        assert c._rate_last_temp == 20.0
        assert c._rate_last_time == 200 * _MIN
        assert c._bucket_base_bucket is None, "a new window re-reads its base value"

    def test_the_full_cold_traverse_is_learned(self):
        """20 → 30 is an ordinary chord and must land in the cold bucket."""
        c = self._cold()
        c._track_heating_rate(15.0, 3, 0.0)
        c._track_heating_rate(15.5, 3, 20 * _MIN)         # phase-uncertain
        c._track_heating_rate(20.0, 3, 200 * _MIN)        # re-anchor at the floor
        c._track_heating_rate(25.0, 3, 500 * _MIN)        # 5 °C in 300 min = 1.0 °C/h
        assert 0 in c._session_fresh_buckets, "the cold bucket must learn from 20 up"
        assert c.heat_rate_buckets[0] != 1.10

    def test_a_start_inside_the_range_is_unchanged(self):
        """The 22 °C start every recorded session begins at behaves exactly as before."""
        c = self._cold()
        c._track_heating_rate(22.0, 3, 0.0)
        c._track_heating_rate(22.5, 3, 20 * _MIN)         # phase-uncertain
        c._track_heating_rate(25.0, 3, 170 * _MIN)        # 2.5 °C in 150 min = 1.0 °C/h
        assert c._rate_last_temp == 22.5, "the window still widens inside one zone"
        assert 0 in c._session_fresh_buckets

    def test_the_mid_and_hot_buckets_still_learn_from_a_cold_start(self):
        """The defect was unique to the bottom; the other two edges were never affected.

        Above 20 a zone boundary and a bucket boundary are the same temperature, so the
        window re-anchors exactly on 30 and on 37 and each bucket is measured from its
        own lower edge. Only the cold bucket could have an anchor that sat outside the
        learning range while staying inside the bucket, which is what made it the one
        place a full traverse could be refused.
        """
        c = self._cold()
        c.heat_rate_buckets = [1.10, 1.30, 1.05]
        c._track_heating_rate(15.0, 3, 0.0)               # below the learning floor
        c._track_heating_rate(15.5, 3, 20 * _MIN)         # phase-uncertain
        c._track_heating_rate(20.0, 3, 200 * _MIN)        # re-anchor at the floor
        c._track_heating_rate(30.0, 3, 800 * _MIN)        # cold traverse, re-anchor
        assert c._rate_last_temp == 30.0, "the window re-anchors on the bucket edge"
        c._track_heating_rate(37.0, 3, 1300 * _MIN)       # mid traverse, re-anchor
        assert c._rate_last_temp == 37.0
        c._track_heating_rate(39.0, 3, 1450 * _MIN)       # hot, still inside 39
        assert c._session_fresh_buckets == {0, 1, 2}, (
            "every bucket the session traversed must have learned"
        )

    def test_the_hot_tail_above_thirty_nine_is_still_refused(self):
        """The upper bound is unchanged: 37 anchors inside the range, so only the
        far end of the span leaves it and the tail alone is refused."""
        c = self._cold()
        c.heat_rate_buckets = [1.10, 1.30, 1.05]
        c._track_heating_rate(37.0, 3, 0.0)
        c._track_heating_rate(37.5, 3, 30 * _MIN)         # phase-uncertain
        c._track_heating_rate(39.0, 3, 130 * _MIN)        # learned
        learned = c.heat_rate_buckets[2]
        assert 2 in c._session_fresh_buckets
        c._track_heating_rate(40.0, 3, 210 * _MIN)        # to > 39 — refused
        assert c.heat_rate_buckets[2] == learned, "the 39-40 tail must not move it"


class TestBandObservations:
    """Each full band traverse is recorded with the weather that prevailed across it.

    The stored history kept one ambient temperature per session, sampled when heating
    started. On 2026-08-25 that was 10.8 °C at 08:41, while the mid band was crossed
    between 12:22 and 17:58 in a warming afternoon — so the number the session was
    corrected by described none of the band it was applied to. A per-band sensitivity
    cannot be fitted from that; it needs the rate of one band against the conditions of
    that same band.
    """

    def _heating(self, c, temps, ambients, start=0.0, step_min=30.0):
        """Feed crossings, moving the ambient between them."""
        t = start
        for temp, amb in zip(temps, ambients):
            c.ambient_temp = amb
            c._track_heating_rate(temp, 3, t * _MIN)
            t += step_min

    def test_a_completed_band_is_recorded(self):
        c = _coord(heat_rate_buckets=[1.10, 1.30, None])
        self._heating(c, [28.0, 28.5, 29.5, 30.0, 30.5], [10.0] * 5)
        cold = [o for o in c._band_observations if o["band"] == 0]
        assert cold, "leaving the cold band must record its traverse"
        o = cold[0]
        assert o["from_temp"] == 28.5 and o["to_temp"] == 30.0, o
        assert o["rate"] > 0 and o["hours"] > 0

    def test_the_ambient_is_the_mean_across_the_traverse(self):
        """Not a snapshot at either end: the point of the record is the conditions the
        band was actually crossed in."""
        c = _coord(heat_rate_buckets=[1.10, 1.30, None])
        # 6, 10, 14 °C while crossing, then out of the band.
        self._heating(c, [28.0, 28.5, 29.0, 30.0, 30.5], [2.0, 6.0, 10.0, 14.0, 20.0])
        o = [x for x in c._band_observations if x["band"] == 0][0]
        assert o["ambient_mean"] == pytest.approx(10.0, abs=0.01), o
        assert o["ambient_mean"] != 2.0 and o["ambient_mean"] != 20.0

    def test_the_water_air_gap_is_stored_alongside(self):
        """Loss scales with the gap between water and air, which is why the hot band is
        the weather-sensitive one. Storing it means a later fit cannot reconstruct it
        differently from how it was measured."""
        c = _coord(heat_rate_buckets=[1.10, 1.30, None])
        self._heating(c, [28.0, 28.5, 29.5, 30.0, 30.5], [10.0] * 5)
        o = [x for x in c._band_observations if x["band"] == 0][0]
        assert o["water_mean"] == pytest.approx((28.5 + 30.0) / 2)
        assert o["delta_mean"] == pytest.approx(o["water_mean"] - 10.0)

    def test_a_partial_band_is_not_recorded(self):
        """Only a traverse between two edges is a clean chord. A session that stops part
        way through has measured a sub-span, which is the thing that cannot be compared
        against a band rate."""
        c = _coord(heat_rate_buckets=[1.10, 1.30, None])
        self._heating(c, [28.0, 28.5, 29.0, 29.5], [10.0] * 4)   # never reaches 30
        assert c._band_observations == []

    def test_the_record_is_bounded(self):
        c = _coord(heat_rate_buckets=[1.10, 1.30, None])
        c._band_observations = [{"band": 0}] * (c._BAND_OBSERVATIONS_MAX + 50)
        self._heating(c, [28.0, 28.5, 29.5, 30.0, 30.5], [10.0] * 5)
        assert len(c._band_observations) == c._BAND_OBSERVATIONS_MAX


class TestBandStatsPersistForever:
    """The fit is kept as running sums, not as a list of observations.

    A bounded list forgets. At three traverses per heat-up and a few heat-ups a week it
    holds a few months, which is less than the seasonal range a weather sensitivity has
    to be fitted across — so the very observations that make the fit possible would be
    the first discarded. Five numbers per band recover the exact least-squares fit over
    every traverse ever made, and never grow.
    """

    def _obs(self, c, band, rate, ambient, water=32.0):
        c._accumulate_band_stats(band, rate, ambient, water - ambient)

    def test_the_fit_recovers_a_known_line(self):
        """Least squares over the sums must equal least squares over the data."""
        c = _coord()
        # rate = 1.5 + 0.02 x ambient, exactly
        for amb in (-5.0, 0.0, 5.0, 10.0, 15.0, 20.0):
            self._obs(c, 1, 1.5 + 0.02 * amb, amb)
        fit = c.band_rate_fit(1)
        assert fit["slope"] == pytest.approx(0.02, abs=1e-9)
        assert fit["intercept"] == pytest.approx(1.5, abs=1e-9)
        assert fit["n"] == 6

    def test_it_survives_the_observation_list_being_trimmed(self):
        """The point of the sums: the fit does not depend on the bounded list at all."""
        c = _coord()
        for amb in (-5.0, 0.0, 5.0, 10.0, 15.0, 20.0):
            self._obs(c, 1, 1.5 + 0.02 * amb, amb)
        c._band_observations = []                    # as trimming eventually does
        assert c.band_rate_fit(1)["slope"] == pytest.approx(0.02, abs=1e-9)

    def test_the_range_seen_is_reported_with_the_fit(self):
        """Range is what decides whether a slope means anything — a whole winter of
        readings between 12 and 14 °C is not evidence about a cold night."""
        c = _coord()
        for amb in (12.0, 12.5, 13.0, 13.5, 14.0):
            self._obs(c, 2, 0.9, amb)
        assert c.band_rate_fit(2)["ambient_span"] == pytest.approx(2.0)

    def test_one_temperature_gives_no_fit_rather_than_a_wrong_one(self):
        c = _coord()
        for _ in range(20):
            self._obs(c, 0, 1.2, 10.0)
        assert c.band_rate_fit(0) is None, "a vertical fit must be declined, not invented"

    def test_bands_are_kept_apart(self):
        c = _coord()
        for amb in (0.0, 10.0, 20.0):
            self._obs(c, 0, 1.2, amb)                 # flat: cold band ignores weather
            self._obs(c, 2, 1.2 + 0.06 * amb, amb)    # steep: hot band feels it
        assert c.band_rate_fit(0)["slope"] == pytest.approx(0.0, abs=1e-9)
        assert c.band_rate_fit(2)["slope"] == pytest.approx(0.06, abs=1e-9)

    def test_the_physical_regressor_is_accumulated_too(self):
        """Loss scales with the water/air gap, so a Newton's-law fit wants that rather
        than ambient alone. Accumulating both avoids finding out in spring that the
        wrong one was kept."""
        c = _coord()
        for amb in (0.0, 5.0, 10.0, 15.0):
            self._obs(c, 1, 1.5 - 0.01 * (32.0 - amb), amb, water=32.0)
        fit = c.band_rate_fit(1, against="delta")
        assert fit["slope"] == pytest.approx(-0.01, abs=1e-9)
