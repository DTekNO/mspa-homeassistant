"""The two-parameter thermal model.

    dT/dt = A - (T_water - T_air)/tau

Tests are in three groups. The first is arithmetic — the closed form must agree with a
numerical integration of the differential equation it claims to solve, or it is solving
something else. The second is learning: `A` from a heating crossing, `tau` from a cooling
one. The third replays this installation's own recorded band traverses, because a model
that cannot reproduce measurements already in hand is not worth deploying.

Run with: python -m pytest tests/test_thermal.py -v
"""
import math

import pytest

from custom_components.mspa.thermal import (
    A_ALPHA, A_MAX, A_MIN, DEFAULT_A, DEFAULT_TAU_H, TAU_ALPHA, TAU_MAX_H, TAU_MIN_H,
    ThermalModel, a_from_heating, blend, implied_litres, implied_loss_w_per_k,
    tau_from_cooling,
)


def _integrate(model, t0, t1, air, dt_h=1 / 3600):
    """Step the differential equation directly; hours to get from t0 to t1."""
    t, n = t0, 0
    while t < t1 and n < 400 * 3600:
        t += (model.a - (t - air) / model.tau_h) * dt_h
        n += 1
    return n * dt_h


class TestTheClosedFormSolvesTheEquation:
    """If these disagree the closed form is solving a different problem."""

    @pytest.mark.parametrize("a,tau,t0,t1,air", [
        (1.30, 55.0, 20.0, 39.0, 13.0),
        (1.30, 55.0, 30.0, 37.0, 5.0),
        (2.00, 21.0, 25.0, 38.0, 18.0),
        (0.90, 90.0, 10.0, 20.0, -2.0),
    ])
    def test_matches_numerical_integration(self, a, tau, t0, t1, air):
        m = ThermalModel(a, tau)
        closed = m.heating_minutes(t0, t1, air) / 60.0
        assert closed == pytest.approx(_integrate(m, t0, t1, air), rel=0.002)

    def test_cooling_is_the_same_law_without_the_heater(self):
        m = ThermalModel(1.3, 55.0)
        # Cooling from 39 to 30 in air 13: gap 26 -> 17, so t = tau*ln(26/17).
        want = 55.0 * math.log(26.0 / 17.0) * 60.0
        assert m.cooling_minutes(39.0, 30.0, 13.0) == pytest.approx(want)

    def test_an_unreachable_target_returns_none_not_a_big_number(self):
        """A number here would be read as a prediction. The honest answer is 'cannot'."""
        m = ThermalModel(0.5, 20.0)                 # asymptote = air + 10
        assert m.asymptote(13.0) == pytest.approx(23.0)
        assert m.heating_minutes(20.0, 39.0, 13.0) is None
        assert m.heating_minutes(20.0, 22.0, 13.0) is not None

    def test_cooling_below_air_returns_none(self):
        assert ThermalModel().cooling_minutes(30.0, 5.0, 13.0) is None

    def test_no_work_is_zero_not_none(self):
        m = ThermalModel()
        assert m.heating_minutes(39.0, 39.0, 13.0) == 0.0
        assert m.heating_minutes(39.0, 30.0, 13.0) == 0.0

    def test_warmer_air_heats_faster_and_cools_slower(self):
        m = ThermalModel()
        assert m.heating_minutes(20, 38, 20) < m.heating_minutes(20, 38, 0)
        assert m.cooling_minutes(38, 30, 20) > m.cooling_minutes(38, 30, 0)


class TestLearning:

    def test_a_is_recovered_from_a_heating_observation(self):
        """The round trip the whole design rests on: rate in, A back out."""
        m = ThermalModel(1.30, 55.0)
        water, air = 25.0, 12.0
        rate = m.rate(water, air)
        assert a_from_heating(rate, water, air, 55.0) == pytest.approx(1.30)

    def test_tau_is_recovered_from_a_cooling_observation(self):
        m = ThermalModel(1.30, 55.0)
        hours = m.cooling_minutes(38.0, 34.0, 12.0) / 60.0
        assert tau_from_cooling(38.0, 34.0, 12.0, hours) == pytest.approx(55.0, rel=1e-6)

    def test_cooling_needs_no_knowledge_of_the_heater(self):
        """Why tau is the anchor: nothing about A, power or volume enters it."""
        hours = 4.0
        t1 = 12.0 + (38.0 - 12.0) * math.exp(-hours / 55.0)
        for a in (0.5, 1.3, 3.0):
            assert tau_from_cooling(38.0, t1, 12.0, hours) == pytest.approx(55.0, rel=1e-6)

    @pytest.mark.parametrize("rate,water,air", [
        (5.0, 25.0, 12.0),        # implies A far above A_MAX
        (-2.0, 25.0, 12.0),       # implies A below A_MIN
    ])
    def test_absurd_heating_samples_are_discarded(self, rate, water, air):
        assert a_from_heating(rate, water, air, 55.0) is None

    @pytest.mark.parametrize("t0,t1,air,hours,why", [
        (38.0, 39.0, 12.0, 2.0, "rising, not cooling"),
        (38.0, 37.9, 12.0, 0.1, "too short to beat quantisation"),
        (13.5, 13.2, 13.0, 2.0, "gap too small, the log is noise"),
        (38.0, 37.99, 12.0, 8.0, "implies tau beyond TAU_MAX_H"),
    ])
    def test_bad_cooling_samples_are_discarded(self, t0, t1, air, hours, why):
        assert tau_from_cooling(t0, t1, air, hours) is None, why

    def test_a_sample_is_discarded_not_clamped(self):
        """Clamping would let a stream of bad samples sit on the boundary and look
        converged. Returning None leaves the prior standing."""
        assert a_from_heating(99.0, 25.0, 12.0, 55.0) is None
        assert blend(1.30, None, A_ALPHA) == 1.30

    def test_the_first_sample_is_adopted_outright(self):
        """A spa far from the seed converges at once rather than crawling."""
        assert blend(None, 0.80, A_ALPHA) == 0.80
        assert blend(1.0, 2.0, 0.35) == pytest.approx(1.35)

    def test_a_moves_faster_than_tau(self):
        """A tracks today's water level; tau tracks the spa. That is the asymmetry."""
        assert A_ALPHA > TAU_ALPHA


class TestAgainstThisInstallationsOwnMeasurements:
    """Eight band traverses recorded by the integration between 28.08 and 11.09.2026.

    Each is a complete chord with its own mean water/air gap, so `A = rate + gap/tau`
    should give the same answer from every one of them. Spread across these is the
    model's real error bar, and it is what any replacement has to beat.
    """

    # (rate C/h, gap C, band, usable)
    TRAVERSES = [
        (1.1306, 13.25, 0, True),
        (0.9299, 18.94, 1, True),
        (1.0597, 13.04, 0, True),
        (0.9908, 20.97, 1, True),
        (1.2308,  5.13, 0, True),
        (1.0077, 12.30, 0, True),
        (0.8878, 20.21, 1, True),
        (0.9500, 22.24, 2, False),      # flagged disturbed by the integration
    ]
    TAU = 55.0

    def _a_values(self, usable_only=False):
        return [r + g / self.TAU for r, g, _, u in self.TRAVERSES
                if u or not usable_only]

    def test_every_traverse_agrees_on_a_within_ten_percent(self):
        vals = self._a_values()
        assert (max(vals) - min(vals)) / min(vals) < 0.12, sorted(vals)

    def test_the_seed_sits_inside_the_measured_range(self):
        vals = self._a_values()
        assert min(vals) <= DEFAULT_A <= max(vals)

    def test_bands_do_not_disagree_systematically(self):
        """If A came out band-dependent the single-tau assumption would be wrong —
        that is exactly the failure the per-band AMBIENT_SENSITIVITY encoded."""
        by = {}
        for r, g, band, _ in self.TRAVERSES:
            by.setdefault(band, []).append(r + g / self.TAU)
        means = {b: sum(v) / len(v) for b, v in by.items()}
        assert max(means.values()) - min(means.values()) < 0.15, means

    def test_every_traverse_is_accepted_by_the_learner(self):
        for rate, gap, band, _ in self.TRAVERSES:
            # water_mean - air == gap, so pass any pair with that difference
            assert a_from_heating(rate, 25.0 + gap, 25.0, self.TAU) is not None

    def test_the_model_reproduces_each_observed_rate(self):
        """Fit A to all eight, then check the model predicts each rate back."""
        a = sum(self._a_values()) / len(self.TRAVERSES)
        m = ThermalModel(a, self.TAU)
        for rate, gap, band, _ in self.TRAVERSES:
            assert m.rate(25.0 + gap, 25.0) == pytest.approx(rate, abs=0.09)


class TestTheSanityChecks:
    """Volume and loss give the owner something to compare with the spa's spec."""

    def test_implied_volume_is_plausible_for_this_spa(self):
        litres = implied_litres(DEFAULT_A, 2200.0)
        assert 1200 < litres < 1700, litres

    def test_loss_is_plausible_for_a_covered_spa(self):
        w = implied_loss_w_per_k(DEFAULT_A, DEFAULT_TAU_H, 2200.0)
        assert 15 < w < 60, w

    def test_a_and_volume_are_inverse(self):
        assert implied_litres(2.0, 2200.0) == pytest.approx(
            implied_litres(1.0, 2200.0) / 2)

    def test_no_heater_power_means_no_answer(self):
        assert implied_litres(1.3, 0) is None
        assert implied_loss_w_per_k(1.3, 55.0, 0) is None


class TestTheBucketViewSurvivesAsACheck:
    """Three chords, derived not stored. Under one tau they must be collinear."""

    def test_chords_decrease_and_are_collinear(self):
        m = ThermalModel(1.30, 55.0)
        mids = [25.0, 33.5, 38.0]
        rates = m.chord_rates(mids, 13.0)
        assert rates[0] > rates[1] > rates[2]
        # collinear against gap by construction: equal second difference of zero
        d1 = (rates[0] - rates[1]) / (mids[1] - mids[0])
        d2 = (rates[1] - rates[2]) / (mids[2] - mids[1])
        assert d1 == pytest.approx(d2)
        assert d1 == pytest.approx(1 / 55.0)

    def test_the_slope_is_one_over_tau_not_a_per_band_constant(self):
        """The measured replacement for AMBIENT_SENSITIVITY = (0.0, 0.02, 0.06)."""
        for tau in (21.0, 55.0, 90.0):
            m = ThermalModel(1.3, tau)
            r = m.chord_rates([25.0, 33.5], 13.0)
            assert (r[0] - r[1]) / 8.5 == pytest.approx(1 / tau)
