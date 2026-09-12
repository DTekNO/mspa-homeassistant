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


def tau_from_cooling(from_temp: float, to_temp: float, air: float,
                     hours: float) -> float | None:
    """`tau` implied by one cooling observation.

    No heater term, so this measurement is independent of heater power, water volume and
    everything the user configured. It is the anchor the rest of the model hangs from.
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
