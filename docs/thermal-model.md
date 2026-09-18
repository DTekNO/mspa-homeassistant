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

## What a measurement is

A rate is measured over a **held chord of at least 1.5 °C** — three crossings — and
chords do not overlap. The gap between crossings is used, not skipped: 20.0 → 21.5 is one
rate over 1.5 °C, not three rates over 0.5 each.

This is forced by the reading, not by the physics. The spa reports water temperature in
0.5 °C steps, so a single crossing is a rise known to ±0.25 °C — ±50% on the rate. That
noise does not average away, because `A` is blended at `A_ALPHA` and the plan is
republished from it at once. Over 1.5 °C the same quantisation is ±17%.

Two consequences worth stating plainly:

* **The first three crossings after the heater starts are not learned from** — two are
  discarded and the third anchors the chord. The probe sits in the pump housing and sees
  heated water before the tub has mixed, so the first band after heater-on runs about 1.6×
  the settled rate (1.82 against ~1.1 °C/h on 17.09.2026; 1.36 against 1.13 on 11.09), and
  a chord that included it read `A` = 1.56 against a settled 1.26 — the first estimate four
  hours early. And in any case the run opened
  somewhere inside a band and that position was never observed, so a rate measured from
  it spans an unknown distance.
* **Nothing is displayed until the first chord completes** — about two hours, five crossings. Before
  that, the estimate would move only because the water advanced against a rate that had
  not changed, which reads as a revision and is not one. See R12.

Longer chords score better and are not used: 2.0 °C would leave fewer than
`TAU_MIN_POINTS` points on a short run, so it would buy a few minutes of accuracy by
silently switching `τ` learning off. R6 and R11 are the same rule seen from two ends.

## Where the plan starts from

The water temperature a plan is built from is `scheduling_temp()`, which extrapolates
within the current 0.5 °C band from the crossing that entered it. That extrapolation is
trusted for a bounded time — `_ANCHOR_MAX_AGE_BANDS` times the time one band should take
at the rate being extrapolated — and past that the reported reading is used instead.

The bound exists because the extrapolation clamps to one band, and a clamped number
carries no sign of how long it has been clamped. On 10.09.2026 the water crossed down
through 18.5 at 02:39, the reading sat there all day, and at 18:50 the projection was
still running that 02:39 cooling rate sixteen hours later — returning the band floor,
18.0, of which thirteen hours had been spent pinned there. The water crossed *up* through
19.0 thirteen minutes after the heater fired, so the truth at handover was about 18.9.

The bound is relative to the rate rather than a fixed age, because during a genuine
cooling dwell crossings are around three hours apart — any fixed cap short enough to
catch sixteen hours also fires inside an ordinary dwell, putting back the band-sized lump
the extrapolation exists to remove. What separates the two cases is not elapsed time but
how far past its own prediction the model has run.

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
