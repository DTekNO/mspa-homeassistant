"""The thermal model: one equation, two parameters.

    dT/dt = A - (T_water - T_air) / tau

`A` is the heater against the thermal mass in °C/h; `tau` is the time constant of the
loss to the air. See docs/thermal-model.md for why these two and not others, and for the
measurements the seeds come from.

Deliberately free of Home Assistant: everything here is arithmetic over floats, so it
can be tested directly and replayed against recorded sessions.
"""
from __future__ import annotations

import math

# Seeds for an installation with no measurements yet. Both are replaced by the first
# real observation — tau by the first cooling stretch, A by the first crossing of a
# heat-up. See docs/thermal-model.md for where the numbers come from.
DEFAULT_TAU_H = 55.0
DEFAULT_A = 1.30

# A single bad sample must not be able to produce an absurd prediction. Samples outside
# these are discarded rather than clamped: clamping lets a stream of bad samples drag the
# estimate onto the boundary and hold it there, which looks like a converged answer.
TAU_MIN_H, TAU_MAX_H = 5.0, 200.0
A_MIN, A_MAX = 0.3, 4.0

# `A` is tracking today's water level, so it moves fast; `tau` is tracking the spa, so it
# moves slowly. That asymmetry is the point of having two parameters.
A_ALPHA = 0.35
TAU_ALPHA = 0.10

# R6: tau only updates from a run whose crossings span at least this much water/air gap.
#
# The number is measured, not chosen for neatness. Fitted on the first eight crossings of
# the 11.09.2026 run — a 4.9 K spread — the slope gives tau = 18 h. The same run's forty
# crossings, spanning 17.2 K, give 65.6 h, which agrees with an independent cooling
# measurement. A short lever produces a confident wrong answer and does it silently, so
# the guard is on the lever rather than on the result.
TAU_MIN_GAP_SPREAD_K = 12.0
# Fewer points than this is a fit to noise however wide the spread looks.
TAU_MIN_POINTS = 10

# How far the water must rise before a crossing chord is worth learning from.
#
# The reading is quantised to 0.5 °C, so a single crossing is a rise of 0.5 °C known to
# +/- 0.25 — a 50% error on the rate, over a span of 25-30 minutes. That noise does not
# average out, because `A` is blended at A_ALPHA and the plan is republished from it
# immediately. Replaying 11.09.2026 the first such chord read A = 1.46 against the run's
# settled 1.25, and opened the estimate 206 minutes fast.
#
# Holding the anchor across three crossings measures the same rate over 1.5 °C, which
# cuts the quantisation error to +/- 17%. Scored over both recorded September runs, mean
# absolute error falls from 38/41 minutes to 25/28 and the worst single step from
# 169/137 to 22/27.
#
# 2.0 and 3.0 °C score better still (20/25 and 19/22 MAE) and were rejected for a
# specific reason: the same points feed the end-of-run `tau` fit, and a longer chord
# thins them. 1.5 °C leaves 13 and 12 points on the two runs, clearing TAU_MIN_POINTS;
# 2.0 °C leaves 10 and 9, so the shorter run would silently stop learning `tau` at all.
# Buying three minutes of MAE by disabling half the model is not a trade.
THERMAL_CHORD_MIN_C = 1.5

# Crossings after heater-on to discard before the first chord is anchored.
#
# The probe sits in the pump housing and sees heated water before the tub has mixed,
# so the first band after heater-on runs far above the settled rate: 1.82 °C/h against
# ~1.1 on 17.09.2026, 1.36 against 1.13 on 11.09. A chord anchored at the first crossing
# includes that band, and on 17.09 it read A = 1.56 against the run's settled 1.26 — the
# first learned estimate landed four hours early and the next fifteen hours were spent
# walking it back.
#
# Replaying that run with the fixed hold: anchored at the 1st crossing, the first snap is
# 259 minutes and the display ranges over six hours; at the 2nd, 119 and 232; at the 3rd,
# 25 minutes and 149. The cost is time to the first learned estimate — about two hours,
# five crossings, instead of ninety minutes — which the hold covers by showing the
# scheduler's own finish until then.
THERMAL_CHORD_SKIP_CROSSINGS = 2

# Below this the gap is small enough that measurement noise dominates the logarithm.
_MIN_GAP_C = 1.0
# A sample shorter than this is mostly quantisation: the reading moves in 0.5 °C steps.
_MIN_SAMPLE_H = 0.25


class ThermalModel:
    """A spa's two parameters, and the predictions they imply."""

    __slots__ = ("a", "tau_h")

    def __init__(self, a: float = DEFAULT_A, tau_h: float = DEFAULT_TAU_H) -> None:
        self.a = a
        self.tau_h = tau_h

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"ThermalModel(a={self.a:.3f}, tau_h={self.tau_h:.1f})"

    # ---- what the model says -------------------------------------------------

    def asymptote(self, air: float) -> float:
        """The temperature this heater could hold against this air, given forever."""
        return air + self.a * self.tau_h

    def rate(self, water: float, air: float) -> float:
        """Instantaneous °C/h at this water and air temperature. May be negative."""
        return self.a - (water - air) / self.tau_h

    def heating_minutes(self, from_temp: float, to_temp: float,
                        air: float) -> float | None:
        """Minutes to heat between two temperatures at a fixed air temperature.

        None when the target is at or beyond the asymptote — the honest answer is that
        it cannot be reached, and a very large number would be read as a prediction.
        """
        if from_temp is None or to_temp is None or air is None:
            return None
        if to_temp <= from_temp:
            return 0.0
        s = self.asymptote(air)
        if s <= to_temp:
            return None
        return self.tau_h * math.log((s - from_temp) / (s - to_temp)) * 60.0

    def cooling_minutes(self, from_temp: float, to_temp: float,
                        air: float) -> float | None:
        """Minutes to cool between two temperatures with the heater off.

        The same law with the heater term removed, so this is where `tau` is measured.
        None when the target is at or below the air temperature.
        """
        if from_temp is None or to_temp is None or air is None:
            return None
        if to_temp >= from_temp:
            return 0.0
        if to_temp <= air:
            return None
        return self.tau_h * math.log((from_temp - air) / (to_temp - air)) * 60.0

    def heating_minutes_piecewise(self, from_temp: float, to_temp: float,
                                  air_segments) -> float | None:
        """Minutes to heat, walking forward through a changing air temperature — R5.

        `air_segments` is an iterable of `(duration_hours, air_c)` covering the run in
        order, oldest first. Each is integrated in closed form at its own air
        temperature and the water carried into the next, so a run that starts before
        dawn and finishes in the afternoon is priced against the curve it will actually
        meet rather than against one average of it.

        Boundaries are handled by solving within a segment rather than by apportioning
        an average across one. In each segment the time to reach the target is known
        exactly; if it fits, the answer is that time plus everything already elapsed,
        and if it does not, the water is advanced to the segment's end and the walk
        continues. A run finishing ninety minutes into a six-hour block therefore costs
        ninety minutes of that block, not a quarter of its average.

        None when the run cannot finish inside the segments supplied — the caller knows
        how far ahead its forecast reaches and a number beyond that would be invented.
        """
        if from_temp is None or to_temp is None:
            return None
        if to_temp <= from_temp:
            return 0.0
        water = from_temp
        elapsed_h = 0.0
        for hours, air in air_segments:
            if hours is None or air is None or hours <= 0:
                continue
            s = self.asymptote(air)
            if s > to_temp:
                # Time to reach the target at this air temperature, if it fits here.
                need = self.tau_h * math.log((s - water) / (s - to_temp))
                if need <= hours:
                    return (elapsed_h + need) * 60.0
            if s > water:
                # Advance to the end of the segment: exponential approach to `s`.
                water = s - (s - water) * math.exp(-hours / self.tau_h)
            # s <= water: the spa is losing ground in this segment; the water is held
            # rather than allowed to fall, because the heater is on and the model's
            # cooling branch is not what a stalled heat-up does.
            elapsed_h += hours
        return None

    def chord_rates(self, midpoints, air: float):
        """The bucket view: mean °C/h across each band, derived rather than stored.

        Under one `tau` these must decrease and must be collinear against gap. That is a
        check on the fit, not a mechanism — see docs/thermal-model.md.
        """
        return [self.rate(m, air) for m in midpoints]


# ---- learning ----------------------------------------------------------------

def a_from_heating(rate_c_per_h: float, water_mean: float, air: float,
                   tau_h: float) -> float | None:
    """`A` implied by one heating observation: A = rate + gap/tau.

    One subtraction, available from the first 0.5 °C crossing of a run rather than after
    a whole band. That is the whole reason the session scalar existed, done in a way that
    does not go stale.
    """
    if None in (rate_c_per_h, water_mean, air, tau_h) or tau_h <= 0:
        return None
    a = rate_c_per_h + (water_mean - air) / tau_h
    return a if A_MIN <= a <= A_MAX else None


def fit_run(points):
    """Fit `A` and `tau` from one run's (gap, rate) crossings — R2, heating only.

    Returns `(a, tau_h)`. `tau_h` is None when the run has not yet swung far enough in
    gap to constrain the slope (R6); `a` is still returned, because the intercept is
    constrained by a single point and is the parameter that moves between runs.

    Both are None when there is nothing usable at all.
    """
    pts = [(g, r) for g, r in (points or []) if g is not None and r is not None]
    if not pts:
        return None, None
    n = len(pts)
    if n == 1:
        return (pts[0][1] if A_MIN <= pts[0][1] <= A_MAX else None), None
    gaps = [g for g, _ in pts]
    spread = max(gaps) - min(gaps)
    sx = sum(gaps); sy = sum(r for _, r in pts)
    sxx = sum(g * g for g in gaps); sxy = sum(g * r for g, r in pts)
    den = n * sxx - sx * sx
    if den <= 0:
        return None, None
    slope = (n * sxy - sx * sy) / den
    a = (sy - slope * sx) / n

    tau = None
    if n >= TAU_MIN_POINTS and spread >= TAU_MIN_GAP_SPREAD_K and slope < 0:
        t = -1.0 / slope
        if TAU_MIN_H <= t <= TAU_MAX_H:
            tau = t
    if tau is None:
        # Not enough lever for the slope, so do not let a bad slope drag the intercept
        # with it: hold the intercept to what the points say at their own mean gap.
        a = None
    if a is not None and not (A_MIN <= a <= A_MAX):
        a = None
    return a, tau


def a_at_fixed_tau(points, tau_h):
    """`A` from one run's crossings with `tau` held — the every-crossing estimator.

    Used while a run has not yet earned a `tau` of its own (R6). Averaging the
    per-crossing `A` rather than fitting a line means one crossing is enough and no
    slope is implied from a lever too short to support one.
    """
    if not points or not tau_h:
        return None
    vals = [r + g / tau_h for g, r in points
            if g is not None and r is not None and A_MIN <= r + g / tau_h <= A_MAX]
    return sum(vals) / len(vals) if vals else None


def tau_from_cooling(from_temp: float, to_temp: float, air: float,
                     hours: float) -> float | None:
    """`tau` implied by one cooling observation.

    **Deliberately not wired into learning** — see R2 in docs/prediction-rules.md. Most
    installations never let the tub cool for long enough to produce one of these, so a
    model that calibrated from cooling would never calibrate at all on a normal spa.
    `tau` is learned from the slope of a heat-up instead.

    Kept because it is the one measurement of `tau` with no heater term in it, which
    makes it the independent check on a value learned from heating: two routes to the
    same physical quantity, from data with no overlap.
    """
    if None in (from_temp, to_temp, air, hours):
        return None
    if hours < _MIN_SAMPLE_H or to_temp >= from_temp:
        return None
    g0, g1 = from_temp - air, to_temp - air
    if g0 < _MIN_GAP_C or g1 < _MIN_GAP_C:
        return None
    tau = hours / math.log(g0 / g1)
    return tau if TAU_MIN_H <= tau <= TAU_MAX_H else None


def blend(prior: float | None, sample: float | None, alpha: float) -> float | None:
    """Exponential smoothing that adopts the first sample outright.

    Seeding the EMA with the first observation rather than with a default means a spa
    whose true value is far from the seed converges immediately instead of crawling.
    """
    if sample is None:
        return prior
    if prior is None:
        return sample
    return alpha * sample + (1.0 - alpha) * prior


def implied_litres(a: float, heater_w: float) -> float | None:
    """Equivalent water volume, from `A` and the configured heater power.

    A sanity check the owner can make against the spa's specification: the arithmetic is
    only as good as the configured wattage, but an implausible volume means an
    implausible `A`.
    """
    if not a or a <= 0 or not heater_w:
        return None
    return heater_w * 3600.0 / (a * 4186.0)


def implied_loss_w_per_k(a: float, tau_h: float, heater_w: float) -> float | None:
    """Standing loss in watts per °C of gap — the other half of the sanity check."""
    litres = implied_litres(a, heater_w)
    if litres is None or not tau_h:
        return None
    return 4186.0 * litres / (tau_h * 3600.0)
