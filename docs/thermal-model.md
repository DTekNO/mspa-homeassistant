# The thermal model

One equation, two parameters, three rules. This replaces the bucket model, its
corrections and their precedence table.

## The equation

A heated body of water losing heat in proportion to its temperature gap with the air:

```
dT/dt = A − (T_water − T_air)/τ
```

`A` is what the heater alone would achieve against zero loss — power over thermal mass,
in °C/h. `τ` is the time constant of the loss. Nothing else.

It integrates in closed form, so a prediction is arithmetic rather than a simulation:

```
t = τ · ln( (S − T_start) / (S − T_end) )        S = T_air + A·τ
```

`S` is the asymptote — the temperature this heater could hold against this air, given
forever. A target at or above `S` is unreachable and the model says so rather than
returning a large number.

## Why these two parameters and not others

They behave differently, and that difference is the whole design.

**`τ` is a property of the spa.** Insulation, cover, surface area. It moves with the
seasons at most. It is the **slope** of rate against gap, so it needs a run that swings
far enough in gap to constrain a slope — see R6 in [the rules](prediction-rules.md).

Cooling would measure it more cleanly, since the heater term vanishes. That route is
deliberately not used: most installations never let the tub cool for days, so a model
calibrating from cooling would never calibrate at all. `tau_from_cooling` remains in the
module as an *independent check* on a value learned from heating — two routes to one
physical quantity, from data with no overlap.

**`A` moves with the water level.** Rain adds water, surplus gets removed, and `A` moves
with it — measured across five weeks of this installation's own traverses it ranged 1.23
to 1.37, about 10%. It is also the parameter predictions are most sensitive to. So it is
re-learned from the current run rather than carried across sessions.

The old model fused both into three bucket rates and then learned the fused quantity
slowly, which is why a rainy fortnight could contaminate the loss term for weeks.

## The three rules

**1. Learn, from heating only.** Every 0.5 °C crossing gives a `(gap, rate)` pair.
Across a run those pairs are a line: its **intercept is `A`** and its **slope is −1/τ**.

`A` updates every crossing, because one point constrains an intercept. `τ` updates only
once the run's crossings span at least `TAU_MIN_GAP_SPREAD_K` of gap, because a short
lever gives a confident wrong answer: on 11.09.2026 the first eight crossings imply
18 h and all forty imply 65.6 h.

Both are smoothed into the running estimate — `A` fast because it tracks today, `τ`
slowly because it tracks the spa.

**2. Correct for temperature.** `T_air` is an input to the equation, not a correction
bolted on outside it. When predicting hours ahead, use the forecast average over the
hours being predicted — which the integration already computes.

**3. Apply consistently.** The scheduler, the Ready at estimate and every replan call
the same function with the same parameters. They cannot disagree, because there is only
one of them.

## What this removes

Three bucket EMAs as stored state. The fresh-bucket override. The session scalar and its
freeze. `AMBIENT_SENSITIVITY` per band. `learned_ambient_factor` and its shrinkage. The
prediction bias. The frozen session plan and band-edge revision. The precedence table
that ordered them.

Each of those existed to patch a model that had no air term inside it. With air in the
equation there is nothing left for them to correct.

## What survives: the buckets, as a view

Three chord rates across the learning range remain useful — not as stored state, but
**derived** from the model:

```
rate(band) = A − (band_midpoint − T_air)/τ
```

They are a check rather than a mechanism. Under one `τ` the three must be collinear
against gap, and they must decrease. If they are not, the fit is wrong, and that shows
up as a diagnostic rather than as a silently bad prediction.

## Seeding

A brand-new installation has no cooling data and no crossings. Both parameters start
from a default and are replaced by measurement:

| | seed | source |
|---|---|---|
| `τ` | 55 h | measured on this installation two independent ways: 101 cooling hours give 61.3 / 53.6 / 54.7 by three methods, and the forty crossings of the 11.09.2026 heat-up give 65.6 |
| `A` | 1.30 °C/h | the mean of eight measured band traverses, which spanned 1.23–1.37 |

The seed for `A` implies roughly 1450 litres at 2200 W, which is the right order for a
six-person spa. Neither seed is special: the first cooling stretch replaces `τ`, and the
first crossing of a heat-up replaces `A`.

## Bounds

Both parameters are clamped, because a single bad sample should not be able to produce
an absurd prediction:

* `τ` between 5 h and 200 h. Below 5 the spa would be uninsulated; above 200 it would be
  a vacuum flask.
* `A` between 0.3 and 4.0 °C/h. At 2200 W those correspond to about 6300 and 470 litres.

A sample outside the bounds is discarded rather than clamped — clamping would let a
stream of bad samples drag the estimate to the boundary and hold it there.

## How it is verified

The model is falsifiable in a way the bucket model was not, and the checks are cheap:

* **`τ` from cooling must agree with `τ` from heating.** Two independent measurements of
  the same physical quantity, from data with no overlap.
* **The three derived chords must be collinear against gap.** Any single `τ` forces it.
* **`A` must be consistent across bands within one run.** On the run of 11.09.2026 the
  three crossings gave 1.231, 1.255 and 1.354 — within 10%, with the outlier being the
  band the integration itself flagged as disturbed.
* **The implied volume must be plausible.** `A` and the configured heater power give
  litres, which the owner can compare against the spa's specification.
