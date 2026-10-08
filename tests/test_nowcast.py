"""The nowcast: the live finish time from the run's own crossings.

Four groups. The first is the arithmetic — the derived `tau` must put the model's own
rate back where the window measured it, or the inversion is wrong. The second is the
window: what counts as a measurement and what is a sensor artefact. The third is the
forecast walk, which must agree with a closed-form integration of the equation it claims
to solve. The fourth replays 08.10.2026, the run this was designed on.

Run with: python -m pytest tests/test_nowcast.py -v
"""
import math

import pytest

from custom_components.mspa.nowcast import (
    DEFAULT_LIFT_C, LIFT_MAX_C, LIFT_MIN_C, MAX_HORIZON_H, MIN_RATE_C_PER_H,
    SETTLE_TOLERANCE, TAU_MAX_H, TAU_MIN_H, WINDOW_BOUNDARIES, WINDOW_SPAN_C,
    Crossing, derive, lift_from_run, measure, minutes_to_target, nowcast, settled,
)


def _run(rate, air=12.0, start=30.0, n=WINDOW_BOUNDARIES, t0=0.0):
    """`n` boundaries climbing at a constant `rate` °C/h in constant air."""
    step_s = (0.5 / rate) * 3600.0
    return [Crossing(t0 + i * step_s, start + i * 0.5, None if i == 0 else air)
            for i in range(n)]


# ---- the inversion -----------------------------------------------------------

class TestDerive:
    """tau = (lift - gap)/rate is an identity, not a fit. It must round-trip."""

    def test_round_trips_the_measured_rate(self):
        # Pick a tub, generate the rate it would really show, and check the inversion
        # recovers the parameters that produced it.
        tau_h, lift = 27.5, 55.0
        a = lift / tau_h
        water_mid, air = 32.75, 11.75
        rate = a - (water_mid - air) / tau_h
        win = measure(_run(rate, air=air, start=32.0))
        got_tau, got_a = derive(win, lift)
        assert got_tau == pytest.approx(tau_h, rel=1e-3)
        assert got_a == pytest.approx(a, rel=1e-3)

    def test_a_times_tau_is_the_lift(self):
        win = measure(_run(1.25))
        tau_h, a = derive(win, 55.0)
        assert a * tau_h == pytest.approx(55.0)

    def test_mass_independence(self):
        """Double the water: A halves, tau doubles, and the lift is untouched.

        This is the whole reason the lift is the carried constant, so it is asserted
        rather than left as a comment.
        """
        tau_h, lift, air, water_mid = 27.5, 55.0, 11.75, 32.75
        rate_small = lift / tau_h - (water_mid - air) / tau_h
        rate_big = lift / (2 * tau_h) - (water_mid - air) / (2 * tau_h)
        for rate, expect in ((rate_small, tau_h), (rate_big, 2 * tau_h)):
            got_tau, got_a = derive(measure(_run(rate, air=air, start=32.0)), lift)
            assert got_tau == pytest.approx(expect, rel=1e-3)
            assert got_a * got_tau == pytest.approx(lift)

    def test_declines_without_air(self):
        # The gap is the one term the window cannot supply for itself.
        assert derive(measure(_run(1.25, air=None)), 55.0) is None

    def test_declines_on_an_implausible_lift(self):
        win = measure(_run(1.25))
        assert derive(win, LIFT_MIN_C - 1) is None
        assert derive(win, LIFT_MAX_C + 1) is None

    def test_declines_when_tau_leaves_the_plausible_range(self):
        # A rate far too fast for the gap implies a time constant no spa has.
        assert derive(measure(_run(20.0)), 55.0) is None

    def test_declines_when_the_water_is_above_the_lift(self):
        # gap >= lift: the heater cannot hold this water against this air at all.
        assert derive(measure(_run(1.25, air=-30.0, start=30.0)), 55.0) is None


# ---- the window --------------------------------------------------------------

class TestMeasure:
    def test_needs_four_boundaries(self):
        rows = _run(1.25, n=WINDOW_BOUNDARIES)
        assert measure(rows[:-1]) is None
        assert measure(rows) is not None

    def test_uses_only_the_newest_four(self):
        # Eight boundaries, the first four slow and the last four fast: the window must
        # report the fast ones. This is the self-correction the design rests on.
        slow = _run(0.6, n=5)
        t0 = slow[-1].mono
        fast = _run(1.5, start=slow[-1].water, n=WINDOW_BOUNDARIES, t0=t0)
        win = measure(slow + fast[1:])
        assert win.rate == pytest.approx(1.5, rel=1e-6)

    def test_rejects_a_span_short_of_the_quantisation(self):
        # A reading that stepped back and forth gives four boundaries over less than
        # 1.5 °C — an artefact wearing a window's clothes.
        rows = [Crossing(0, 30.0), Crossing(1800, 30.5, 12.0),
                Crossing(3600, 30.0, 12.0), Crossing(5400, 30.5, 12.0)]
        assert measure(rows) is None

    def test_accepts_a_wider_span(self):
        # A missed poll gives a 1.0 °C step. Still four exact boundaries, still valid.
        rows = [Crossing(0, 30.0), Crossing(1800, 30.5, 12.0),
                Crossing(3600, 31.5, 12.0), Crossing(5400, 32.0, 12.0)]
        win = measure(rows)
        assert win.span_c == pytest.approx(2.0)

    def test_rejects_a_stalled_window(self):
        assert measure(_run(MIN_RATE_C_PER_H / 2)) is None

    def test_rejects_zero_elapsed(self):
        rows = [Crossing(0, 30.0 + i * 0.5, 12.0) for i in range(WINDOW_BOUNDARIES)]
        assert measure(rows) is None

    def test_air_is_time_weighted(self):
        """A slow cold interval must outweigh a quick warm one.

        A plain mean would make the two 10-minute intervals at 0 °C count as much as the
        two-hour one at 20 °C, which is the error the per-interval `air` exists to avoid.
        """
        rows = [Crossing(0, 30.0),
                Crossing(600, 30.5, 0.0),
                Crossing(1200, 31.0, 0.0),
                Crossing(1200 + 7200, 31.5, 20.0)]
        win = measure(rows)
        assert win.air == pytest.approx((0.0 * 1200 + 20.0 * 7200) / 8400)


# ---- the settling gate -------------------------------------------------------

class TestSettled:
    @staticmethod
    def _from_band_rates(rates, air=12.0, start=30.0):
        rows, t, w = [Crossing(0.0, start)], 0.0, start
        for r in rates:
            t += (0.5 / r) * 3600.0
            w += 0.5
            rows.append(Crossing(t, w, air))
        return rows

    def test_a_constant_rate_is_settled(self):
        assert settled(self._from_band_rates([1.25] * 6), 55.0) is True

    def test_unmixed_water_is_not_settled(self):
        """A rate falling faster than Newton allows reads as still stratified.

        The probe sees heated water before the tub mixes, so the opening bands run fast
        and the derived `tau` climbs as they decay. It is the climb that is detected, not
        a crossing count — the transient is four crossings after a power restore and two
        after an ordinary heater-on.
        """
        rows = self._from_band_rates([1.80, 1.60, 1.42, 1.30, 1.26, 1.25])
        assert settled(rows, 55.0) is False

    def test_settles_once_the_transient_has_left_both_windows(self):
        """The same run, four crossings later: both windows are now mixed water."""
        rows = self._from_band_rates(
            [1.80, 1.60, 1.42, 1.30, 1.26, 1.25, 1.24, 1.26, 1.23, 1.25])
        assert settled(rows, 55.0) is True

    def test_solar_drift_does_not_read_as_stratification(self):
        """Settled bands vary by much more than Newton allows, and legitimately.

        Across seven settled bands on 08.10.2026 the single-band `tau` ranged 22.9 to
        33.6 at a constant gap, because the sun was on the cover. The gate must not
        mistake that for unmixed water — which is exactly what a band-against-band test
        would have done. See SETTLE_TOLERANCE.
        """
        rows = self._from_band_rates([1.20, 1.38, 1.22, 1.40, 1.24, 1.36])
        assert settled(rows, 55.0) is True

    def test_not_settled_before_two_windows_exist(self):
        assert settled(_run(1.25, n=WINDOW_BOUNDARIES), 55.0) is False
        assert settled(self._from_band_rates([1.25] * 5), 55.0) is False


# ---- the forecast walk -------------------------------------------------------

class TestMinutesToTarget:
    @staticmethod
    def _closed_form(water, target, a, tau_h, air):
        s = air + a * tau_h
        return tau_h * math.log((s - water) / (s - target)) * 60.0

    def test_agrees_with_the_closed_form_in_constant_air(self):
        """The walk must solve the equation it claims to solve."""
        a, tau_h, air = 2.0, 27.5, 12.0
        minutes, held = minutes_to_target(33.5, 39.5, a, tau_h, air=air)
        # One-minute Euler against the exact solution: about five seconds out over
        # five hours, which is three orders inside the rounding it is displayed at.
        assert minutes == pytest.approx(
            self._closed_form(33.5, 39.5, a, tau_h, air), abs=0.2)
        assert held is False

    def test_segments_agree_with_a_two_stage_closed_form(self):
        a, tau_h = 2.0, 27.5
        # Two hours at 5 °C, then warm. Integrate the first leg in closed form, carry
        # the water, and solve the second.
        s1 = 5.0 + a * tau_h
        w1 = s1 - (s1 - 33.5) * math.exp(-2.0 / tau_h)
        expect = 120.0 + self._closed_form(w1, 39.5, a, tau_h, 18.0)
        minutes, _ = minutes_to_target(33.5, 39.5, a, tau_h,
                                       air_segments=[(2.0, 5.0), (12.0, 18.0)])
        assert minutes == pytest.approx(expect, abs=0.2)

    def test_where_the_warm_hours_fall_decides_the_sign(self):
        """The whole case for walking the forecast instead of averaging it.

        A flat mean over the forecast horizon cannot know whether its warm hours are the
        ones the run will actually be heating through. On 08.10.2026 the walk came out
        29 minutes *earlier* than a flat air temperature, because the cold night was
        averaged into a mean for a run that finished in the afternoon sun. Reverse the
        order and the error reverses with it, which is why the sign is not guessable and
        the integral is not optional.
        """
        a, tau_h = 2.0, 27.5
        warm_first = [(6.0, 15.0), (12.0, 2.0)]
        cold_first = [(6.0, 2.0), (12.0, 15.0)]
        for segs, faster_than_mean in ((warm_first, True), (cold_first, False)):
            mean = sum(h * t for h, t in segs) / sum(h for h, _ in segs)
            walked, _ = minutes_to_target(33.5, 39.5, a, tau_h, air_segments=segs)
            flat, _ = minutes_to_target(33.5, 39.5, a, tau_h, air=mean)
            assert (walked < flat) is faster_than_mean

    def test_holds_the_last_row_past_the_forecast_and_says_so(self):
        minutes, held = minutes_to_target(33.5, 39.5, 2.0, 27.5,
                                          air_segments=[(0.5, 12.0)])
        assert held is True
        assert minutes > 30.0

    def test_already_there(self):
        assert minutes_to_target(39.5, 39.5, 2.0, 27.5, air=12.0) == (0.0, False)

    def test_declines_without_any_air(self):
        assert minutes_to_target(33.5, 39.5, 2.0, 27.5) is None

    def test_declines_beyond_the_horizon(self):
        # An asymptote a whisker above the target takes forever to reach it. "Forever"
        # must come back as None rather than as a very large number.
        assert minutes_to_target(33.5, 39.5, 0.5, 100.0, air=-10.0) is None


# ---- the whole algorithm -----------------------------------------------------

class TestNowcast:
    def test_the_0810_window(self):
        """08.10.2026 13:33: 33.5 °C, a measured 1.2567 °C/h, air 11.75, lift 55.196.

        The run this design came from. The window implies tau near 27 h and A near 2.03,
        and puts 39.5 °C about five and a quarter hours out — against the learned model's
        22:55, which was nearly four hours late because its parameters described a tub
        with 260 litres more water in it.
        """
        n = nowcast(_run(1.2567, air=11.75, start=32.0), 39.5, 55.196, air_now=11.75)
        assert n.tau_h == pytest.approx(27.2, abs=0.3)
        assert n.a == pytest.approx(2.03, abs=0.03)
        assert n.gap_c == pytest.approx(21.0, abs=0.1)
        assert n.minutes / 60.0 == pytest.approx(5.4, abs=0.2)

    def test_the_lift_barely_matters(self):
        """A 20 % error in the carried constant costs about ten minutes.

        The sensitivity table in docs/nowcast-ready-at.md, asserted. It is why one
        seeded constant refined once per run is enough, and why this is not learning.
        """
        rows = _run(1.2567, air=11.75, start=32.0)
        lo = nowcast(rows, 39.5, 50.0, air_now=11.75).minutes
        mid = nowcast(rows, 39.5, 55.196, air_now=11.75).minutes
        hi = nowcast(rows, 39.5, 60.0, air_now=11.75).minutes
        assert lo > mid > hi                      # a bigger lift is a faster tub
        assert abs(lo - hi) < 20.0

    def test_step_minutes_is_the_next_boundary(self):
        n = nowcast(_run(1.2567, air=11.75, start=32.0), 39.5, 55.196, air_now=11.75)
        assert n.step_minutes == pytest.approx(0.5 / 1.2567 * 60.0, rel=0.05)

    def test_declines_when_the_asymptote_is_below_the_target(self):
        # Deep cold: air + lift under the target. The honest answer is that it does not
        # arrive, not a number.
        assert nowcast(_run(1.0, air=-20.0, start=30.0), 39.5, 55.0, air_now=-20.0) is None

    def test_declines_before_the_window_exists(self):
        assert nowcast(_run(1.25, n=3), 39.5, 55.0, air_now=12.0) is None

    def test_a_lower_water_level_shows_up_as_a_faster_rate(self):
        """The refill case, end to end.

        Less water heats faster. Nothing models the level; it arrives as a measured rate
        and the projection follows it immediately — where the learned model took several
        runs to work it through.
        """
        full = nowcast(_run(1.00, air=11.75, start=32.0), 39.5, 55.0, air_now=11.75)
        part = nowcast(_run(1.26, air=11.75, start=32.0), 39.5, 55.0, air_now=11.75)
        assert part.minutes < full.minutes
        assert part.tau_h < full.tau_h            # less water, shorter time constant
        assert part.a > full.a                    # and a stronger heater against it


# ---- the carried constant ----------------------------------------------------

class TestLiftFromRun:
    def test_migration_from_the_learned_pair(self):
        # The store's own values on 08.10.2026. No surgery: the lift is already there.
        assert lift_from_run(1.5047, 36.68) == pytest.approx(55.2, abs=0.1)

    def test_rejects_an_implausible_product(self):
        assert lift_from_run(4.0, 200.0) is None
        assert lift_from_run(0.3, 10.0) is None

    def test_rejects_nothing_to_multiply(self):
        assert lift_from_run(None, 27.5) is None
        assert lift_from_run(1.5, None) is None

    def test_the_seed_is_inside_its_own_bounds(self):
        assert LIFT_MIN_C <= DEFAULT_LIFT_C <= LIFT_MAX_C


class TestAFreezingNightIsAReading:
    """0.0 °C is falsy, and the one night where getting the air wrong matters most."""

    def test_zero_air_is_used_not_discarded(self):
        rows = _run(0.60, air=0.0, start=30.0)
        n = nowcast(rows, 39.5, 55.0, air_now=0.0)
        assert n is not None
        assert n.air_c == 0.0
        assert n.asymptote_c == pytest.approx(55.0)
        # The same window at 10 °C must finish sooner; if the 0.0 had been dropped and
        # the window's own air used instead, these would be identical.
        warm = nowcast(_run(0.60, air=10.0, start=30.0), 39.5, 55.0, air_now=10.0)
        assert warm.minutes < n.minutes
