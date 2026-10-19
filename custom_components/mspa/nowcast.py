"""Nowcasting the live finish time from the run's own crossings.

The live estimate answers one question — when will *this* water reach *this* target —
and it is standing next to a tub that is telling it the answer. So it is built from the
run's own 0.5 °C crossings, and from nothing else that was learned on another day.

    dT/dt = A - (T_water - T_air) / tau

The same equation as thermal.py, parameterised differently. There, both `A` and `tau`
are learned across runs. Here the rate is *measured* over the last four crossings and
one carried constant turns that rate into a trajectory:

    lift = A * tau              the asymptote's height above the air
    tau  = (lift - gap) / rate
    A    = lift / tau

Exactly one constant is carried, and it has to be. Converting a measured rate into a
curve needs either `tau` or the lift, and four crossings cannot measure both: fitted
live on 08.10.2026 the two-parameter fit returned tau = 5.15 h and an asymptote below
the target, meaning the tub never arrives. See docs/nowcast-ready-at.md.

The lift is also the right one to carry. `A` scales as one over the water mass and
`tau` scales with it, so their product survives a refill untouched — which is precisely
the event that broke the learned design. And it barely matters: a 20 % error in it
costs eleven minutes on a six-degree climb, because the asymptote sits twenty degrees
above any reachable target, far from the steep part of the curve.

Deliberately free of Home Assistant: everything here is arithmetic over floats, so it
can be replayed against a recorded run.
"""
from __future__ import annotations

import math
from typing import NamedTuple

# The window is the last four crossing boundaries — a 1.5 °C span at this spa's 0.5 °C
# quantisation. Four is not a smoothing choice; it is the shortest span that measures a
# rate rather than a phase. One crossing is 0.5 °C known to +/- 0.25, a 50 % error; three
# boundary-to-boundary intervals cut that to +/- 17 %, and every boundary is exact so a
# wider span is less noisy without being more biased.
#
# It is a *rolling* window, recomputed at every crossing, which is what makes the
# estimate self-correcting: a lower water level, a cold night, a sunny afternoon all
# arrive as a changed rate without anything having to model them.
WINDOW_BOUNDARIES = 4
WINDOW_SPAN_C = 1.5

# There is no upper bound on the measurement window, and that is a measured choice.
#
# The wander in the published finish is the window's noise multiplied by the time
# remaining, so it is worst where there is most evidence and least need to be quick. The
# 15.10 cold start recorded 44 crossings; every cap tried cost accuracy. Scored against
# that run, as the mean error of what was actually displayed:
#
#     cap 4: 24 min     cap 12: 20 min     cap 20: 19 min     uncapped: 16 min
#
# Two things make it safe. `derive` is the exact integral rather than a chord, so a wide
# window carries no width bias — it recovers a simulated tub's `tau` to one part in a
# million from 4 boundaries to 40. And every disturbance that would leave a long window
# describing a tub that no longer exists — cold water, a soak, a heater stop — discards
# the run outright, so there is nothing for it to be stale about. The exception is
# draining, which is invisible to a window of any width; see docs/nowcast-ready-at.md.
#
# What it *is* approximate about is the air: the window carries one time-weighted outdoor
# temperature, so a window spanning a night assumes an air that in fact varied. The error
# is self-limiting, because the window is short while the gap is small and the air matters
# most, and by the time it is long the gap dwarfs the variation.
#
# The settle gate deliberately stays at WINDOW_BOUNDARIES: it is looking for the opening
# transient and wants to be quick.

# Seeded for an installation that has never completed a run. Replaced by the first
# completed run's own (A * tau), and thereafter a long-memory average — the lift is the
# one quantity a refill does not disturb, so it is the one that may safely be remembered.
DEFAULT_LIFT_C = 55.0
# A lift outside this is not a spa. Rejected rather than clamped: clamping lets a stream
# of bad samples sit on the boundary and look converged.
LIFT_MIN_C, LIFT_MAX_C = 20.0, 150.0

# Derived `tau` outside this means the window measured something that was not heating.
TAU_MIN_H, TAU_MAX_H = 5.0, 200.0

# Below this the water is not rising in any usable sense and the projection would be a
# division by nearly nothing.
MIN_RATE_C_PER_H = 0.05

# How much the derived `tau` may still be climbing and count as settled.
#
# The probe sits in the pump housing and reads unmixed water, so the bands right after
# heater-on run fast. A fast band gives a small `tau`, and as the tub mixes the derived
# `tau` climbs toward the real one. Newton's own effect is already inside the formula —
# the gap is a term — so a pair of windows whose `tau` has *stopped* climbing is a pair
# whose bands are consistent with Newton, which is the test the settling rule wants.
#
# Not a crossing count. Today's rule discards two, which was measured against ordinary
# heater-on transients; after the power restore on 08.10.2026 the whole tub was
# stratified and the transient ran at least four. A count tuned on one is wrong on the
# other, and the test costs nothing the count does not.
#
# Compared between *non-overlapping* windows, which is why it takes
# 2 * WINDOW_BOUNDARIES - 1 crossings. Two candidates were tried and rejected:
#
#   * Single band against single band. Band rates are not noisy, they are genuinely
#     variable: across seven settled bands on 08.10.2026 the derived `tau` ranged 22.9
#     to 33.6 at a constant gap, because the sun was on the cover. A test at that scale
#     cannot see a transient through the weather.
#   * Windows shifted by one. They share three of their four boundaries, so a 30 % decay
#     in the band rates showed up as a 6 % move in the window `tau` — too small to
#     separate from the same solar drift. Non-overlapping windows show it as 20 %.
#
# 12 % is set above that solar drift and well below the transient. Provisional: it is
# the one number here not fitted to a recorded run, which is what the crossing log is
# being kept for.
SETTLE_TOLERANCE = 0.12

# One-minute steps. With tau near 28 h the step is 6e-4 of the time constant, so the
# Euler truncation over a six-hour run is about five seconds against the closed form —
# three orders of magnitude inside the five-minute rounding the answer is displayed at.
# There is no case for anything cleverer, and the walk expresses a cold segment (where
# the asymptote dips toward the target and the water merely creeps) without the closed
# form's special case for holding the water in place.
_STEP_H = 1.0 / 60.0
# Past this the question is not being answered, it is being invented.
MAX_HORIZON_H = 72.0


class Crossing(NamedTuple):
    """One 0.5 °C boundary, and the weather while the water climbed to it.

    `mono` is a monotonic clock, used for every interval: it cannot step backwards over
    an NTP correction, and an interval is the one quantity this whole method rests on.
    `air` is the time-weighted outdoor temperature over the interval that *ended* here,
    so the first crossing of a window contributes a position and no weather.
    """

    mono: float
    water: float
    air: float | None = None


class Window(NamedTuple):
    """What the last four boundaries measured."""

    rate: float             # °C/h over the whole window
    span_c: float           # how far the water actually moved
    hours: float            # how long it took
    water_start: float      # water at the oldest boundary
    water_mid: float        # midpoint water temperature
    air: float | None       # time-weighted outdoor over the window
    water: float            # water at the newest boundary
    mono: float             # when that boundary was crossed
    boundaries: int         # how many were used


class Nowcast(NamedTuple):
    """The projection, and everything needed to see why it says what it says."""

    minutes: float          # from the newest crossing to the target
    step_minutes: float     # from the newest crossing to the next 0.5 °C boundary
    rate: float
    tau_h: float
    a: float
    lift_c: float
    gap_c: float
    air_c: float
    asymptote_c: float
    water: float
    target: float
    span_c: float
    hours: float
    boundaries: int         # how many crossings the rate was measured over
    air_held: bool          # the forecast ran out and its last row was held


def measure(crossings, boundaries: int = WINDOW_BOUNDARIES) -> Window | None:
    """Rate and conditions over the last `boundaries` crossings, or None.

    None whenever the window is not yet a measurement: too few boundaries, no elapsed
    time, or a span short of WINDOW_SPAN_C. That last guard is the quantisation one —
    a reading that stepped back and forth produces four boundaries covering less than
    1.5 °C, which is a sensor artefact wearing a window's clothes.
    """
    rows = [c for c in (crossings or []) if c is not None]
    boundaries = max(WINDOW_BOUNDARIES, int(boundaries))
    if len(rows) < boundaries:
        return None
    win = rows[-boundaries:]
    first, last = win[0], win[-1]
    span = last.water - first.water
    hours = (last.mono - first.mono) / 3600.0
    # The span a window of this width ought to cover, less one step of slack for a
    # reading that stepped back and forth — the quantisation guard.
    want = WINDOW_SPAN_C * (boundaries - 1) / (WINDOW_BOUNDARIES - 1)
    if hours <= 0 or span < want - 1e-9:
        return None
    rate = span / hours
    if rate < MIN_RATE_C_PER_H:
        return None
    # Time-weighted, not a plain mean: the intervals between boundaries are not equal
    # lengths, and the whole point of carrying `air` per interval is that the slow cold
    # interval should count for more than the quick warm one.
    num = den = 0.0
    for prev, cur in zip(win, win[1:]):
        if cur.air is None:
            continue
        secs = cur.mono - prev.mono
        if secs <= 0:
            continue
        num += cur.air * secs
        den += secs
    return Window(
        rate=rate, span_c=span, hours=hours,
        water_start=first.water, water_mid=(first.water + last.water) / 2.0,
        air=(num / den) if den > 0 else None,
        water=last.water, mono=last.mono, boundaries=len(win),
    )


def derive(window: Window | None, lift_c: float) -> tuple[float, float] | None:
    """`(tau_h, A)` implied by a measured window and the carried lift, or None.

    This is the **exact** inversion of the equation over the window, not a chord
    approximation. Integrating `dT/dt = A - (T - T_air)/tau` with `A = lift/tau` between
    the window's two boundaries gives

        elapsed = tau * ln((S - T_start) / (S - T_end)),    S = air + lift

    and therefore `tau = elapsed / ln(...)`, with `A = lift/tau`. No fit, no slope, no
    midpoint.

    The chord form it replaces — `tau = (lift - gap) / rate` at the midpoint gap — is the
    first-order expansion of this. Simulated against a known tub it is indistinguishable
    out to a 3.5 °C window, drifts +0.3 % at 9.5 °C and +1.7 % across a 19.5 °C span,
    because the measured rate is `span/elapsed`, a time-weighted mean, while the midpoint
    gap implies the span-weighted one. So the chord was not what limited the window
    width at today's cap — but it would become the limit, and the integral costs one
    logarithm and is exact at every width.

    None when there is no air reading (the gap is the term the window cannot supply for
    itself), when the lift is implausible, or when the water is already at or beyond the
    asymptote — in which case no time constant describes the climb.
    """
    if window is None or window.air is None:
        return None
    if lift_c is None or not LIFT_MIN_C <= lift_c <= LIFT_MAX_C:
        return None
    asymptote = window.air + lift_c
    if asymptote <= window.water:
        # The water is already above anything this heater can hold against this air.
        return None
    ratio = (asymptote - window.water_start) / (asymptote - window.water)
    if ratio <= 1.0:
        return None
    tau_h = window.hours / math.log(ratio)
    if not TAU_MIN_H <= tau_h <= TAU_MAX_H:
        return None
    return tau_h, lift_c / tau_h


def settled(crossings, lift_c: float, tolerance: float = SETTLE_TOLERANCE) -> bool:
    """Whether the tub has mixed, judged on two non-overlapping windows.

    Settled when the newer window's `tau` is no longer climbing — see SETTLE_TOLERANCE
    for why the comparison is between windows that do not share their evidence, and why
    it is `tau` rather than the raw rate (the gap moves between the two windows, and
    `tau` is the quantity with that already divided out).

    A *falling* `tau` is allowed through. Unmixed water can only make a band read faster
    than the bulk, so a fall is the weather, not stratification.

    Needs 2 * WINDOW_BOUNDARIES - 1 crossings: the two windows meet at one shared
    boundary, which is a position rather than a measurement.
    """
    rows = [c for c in (crossings or []) if c is not None]
    need = 2 * WINDOW_BOUNDARIES - 1
    if len(rows) < need:
        return False
    newer = derive(measure(rows[-WINDOW_BOUNDARIES:]), lift_c)
    older = derive(measure(rows[-need:-WINDOW_BOUNDARIES + 1]), lift_c)
    if newer is None or older is None:
        return False
    return newer[0] <= older[0] * (1.0 + tolerance)


def minutes_to_target(water: float, target: float, a: float, tau_h: float,
                      *, air_segments=None, air: float | None = None):
    """Minutes to heat from `water` to `target`, walking the air forecast — R5.

    `air_segments` is `[(duration_hours, air_c)]` from now onward, which is what the
    forecast already provides. Doing this properly matters and the sign is not
    guessable: on 08.10.2026 the walk moved the answer 29 minutes *earlier* than a flat
    air temperature, because a sunny afternoon outweighed the cold night for a run
    finishing in the evening.

    Returns `(minutes, air_held)`, or None. `air_held` says the forecast ran out and its
    last row was carried on — the mildest available assumption, and flagged rather than
    hidden. None when the run cannot finish inside MAX_HORIZON_H, which is the honest
    answer for a target the heater cannot reach against the air it is given.
    """
    if None in (water, target, a, tau_h) or tau_h <= 0:
        return None
    if target <= water:
        return 0.0, False
    segs = [(h, t) for h, t in (air_segments or [])
            if h is not None and t is not None and h > 0]
    if segs:
        seg_left, cur_air = segs[0]
        idx = 0
        held = False
    elif air is not None:
        seg_left, cur_air, idx, held = float("inf"), air, 0, False
    else:
        return None

    t_h = 0.0
    temp = float(water)
    while t_h < MAX_HORIZON_H:
        if seg_left <= 0.0:
            idx += 1
            if idx < len(segs):
                seg_left, cur_air = segs[idx]
            else:
                # Beyond the forecast: hold its last row rather than inventing a trend.
                seg_left, held = float("inf"), True
        step = min(_STEP_H, seg_left)
        climb = (a - (temp - cur_air) / tau_h) * step
        if temp + climb >= target:
            # Interpolate within the step rather than reporting the step boundary.
            # Without this the answer is quantised to whole minutes, which is a minute
            # of avoidable error on a quantity the whole design exists to get right.
            frac = (target - temp) / climb if climb > 0 else 1.0
            return (t_h + step * frac) * 60.0, held
        temp += climb
        t_h += step
        seg_left -= step
    return None


def nowcast(crossings, target: float, lift_c: float, *,
            air_segments=None, air_now: float | None = None,
            boundaries: int | None = None) -> Nowcast | None:
    """The whole algorithm. Minutes from the newest crossing to the target, or None.

    The projection starts at the newest *crossing* rather than at the current reading,
    because that is the one point where the water temperature and the clock are both
    facts: the reading between boundaries is known only to within half a degree, while a
    crossing time is exact. The caller adds the time since.
    """
    rows = [c for c in (crossings or []) if c is not None]
    if boundaries is None:
        boundaries = len(rows)      # everything the run has — see WINDOW_BOUNDARIES
    # Widest window the evidence supports, down to the minimum.
    win = params = None
    while boundaries >= WINDOW_BOUNDARIES and params is None:
        win = measure(rows, boundaries)
        params = derive(win, lift_c)
        boundaries -= 1
    if win is None or params is None:
        return None
    tau_h, a = params
    asymptote = win.air + lift_c
    if asymptote <= target:
        # Honest refusal: this heater cannot hold this water against this air. Returning
        # a very large number would be read as a prediction.
        return None
    # `is not None`, not `or`: an air temperature of exactly 0.0 is a reading, and a
    # freezing night is the one where getting this wrong matters most.
    flat = air_now if air_now is not None else win.air
    walked = minutes_to_target(win.water, target, a, tau_h,
                               air_segments=air_segments, air=flat)
    if walked is None:
        return None
    minutes, held = walked
    step = minutes_to_target(win.water, min(win.water + 0.5, target), a, tau_h,
                             air_segments=air_segments, air=flat)
    return Nowcast(
        minutes=minutes,
        step_minutes=(step[0] if step else 0.0),
        rate=win.rate, tau_h=tau_h, a=a, lift_c=lift_c,
        gap_c=win.water_mid - win.air, air_c=win.air, asymptote_c=asymptote,
        water=win.water, target=float(target),
        span_c=win.span_c, hours=win.hours, boundaries=win.boundaries, air_held=held,
    )


def lift_from_run(a: float | None, tau_h: float | None) -> float | None:
    """The lift a completed run measured, from its own `A` and `tau`.

    This is also the migration: an installation carrying a learned `(A, tau)` pair
    already carries the lift, as their product, and needs no surgery to start using it.
    """
    if a is None or tau_h is None or a <= 0 or tau_h <= 0:
        return None
    lift = a * tau_h
    return lift if LIFT_MIN_C <= lift <= LIFT_MAX_C else None
