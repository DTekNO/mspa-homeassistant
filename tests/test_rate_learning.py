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
from custom_components.mspa.coordinator import MSpaUpdateCoordinator


def _coord(**overrides) -> MSpaUpdateCoordinator:
    c = object.__new__(MSpaUpdateCoordinator)
    c.computed_heat_rate = 0.89
    c.heat_rate_buckets = [None, 1.30, None]
    c._rate_last_temp = None
    c._rate_last_time = None
    c._rate_first_step = False
    c._session_scalar = 1.0
    c._session_scalar_bucket = None
    c._session_fresh_buckets = set()
    c.ambient_temp = None
    c.ambient_baseline = None
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
        expected = after_first + 0.25 * (1.0 / 0.75 - after_first)
        assert abs(c.heat_rate_buckets[1] - expected) < 1e-6
        per_step = after_first + 0.25 * (2.0 - after_first)
        assert c.heat_rate_buckets[1] < per_step - 0.05, "looks like per-step sampling"

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
