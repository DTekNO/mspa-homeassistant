# Nowcasting "Ready at"

Agreed 08.10.2026. Nothing here is built yet.

## The problem

**Ready at** is computed from `A` and `tau` learned across runs. `A` is an EMA over
36 samples spanning months, and the thing it averages over — the amount of water in the
tub — changes between runs. On 08.10, with the tub freshly refilled to a lower level, the
card read 22:55 while the tub was measurably going to make 18:50. Nearly four hours out,
and not because the physics is wrong but because the parameters described a tub with
about 260 litres more water in it.

The live estimate does not need to be learned. It is standing next to a tub that is
telling it the answer.

## The decision

The live estimate is rebuilt from the current run's own observations. The scheduler keeps
the learned model, because it has to predict before a single crossing exists.

- One **Ready at** sensor. `newton_ready_at` and `newton_start_at` are removed.
- Rate from a **rolling window over the last four crossing boundaries**, recomputed at
  every crossing.
- Project with **Newton's law, integrated over the hourly temperature forecast**.
- No learned rates in the display path: `A`, the buckets, the bias, the session scalar
  and the ambient factor all go.

## The one number that is carried

Converting a measured rate into a projection needs either `tau` or the asymptote lift
`A·tau`. Both cannot come from the window — see *Why tau cannot be fitted locally* below.
So exactly one constant is carried, and it is the lift.

The lift is the right choice because it is **independent of water mass**. `A` scales as
one over mass and `tau` scales with mass, so their product survives a refill untouched,
which is precisely the event that broke the old design. It is a property of heater power
and insulation.

It also barely matters. Measured on 08.10 at 33.5 °C with 6 K to run:

| Lift | tau | A | Ready |
|---|---|---|---|
| 50.0 | 23.01 | 2.173 | 18:57 |
| 55.2 | 27.14 | 2.034 | 18:50 |
| 60.0 | 30.96 | 1.938 | 18:46 |

A 20 % error costs 11 minutes, because this tub's asymptote sits 22 K or more above any
reachable target, far from the steep part of the curve. A seeded default refined once per
completed run is sufficient. This is not learning in any meaningful sense.

## The sensor

One entity, three regimes, with an attribute naming the active one.

| Regime | When | Shows |
|---|---|---|
| `scheduled` | A schedule is pending, not yet heating | The scheduled ready time |
| `opening` | Heating, window not yet usable | The held opening estimate |
| `nowcast` | Heating, window settled | The nowcast |

The hold is what prevents a jump at handover. That property came from the hold, not from
the scheduler and the display sharing an engine, so the two may now differ freely. Release
the hold after the window is settled, not after one chord.

## The algorithm

1. On every poll while heating, record each 0.5 °C **crossing**: timestamp, water
   temperature, and the time-weighted outdoor temperature over the interval since the
   previous crossing.
2. **Window** = the last four boundaries, a 1.5 °C span.
3. `rate = 1.5 / elapsed_hours`.
4. `gap = midpoint_water − time_weighted_air_over_window`.
5. `tau = (lift − gap) / rate`, then `A = lift / tau`.
6. Integrate `dT/dt = A − (T − T_air(t)) / tau` forward in one-minute steps, taking
   `T_air` from the hourly forecast, until the target is reached.

Step 6 is worth doing properly. On 08.10 the forecast walk moved the answer 29 minutes
**earlier** than a flat air temperature, because a sunny afternoon outweighed the cold
night for a run finishing in the evening. The sign is not guessable without the integral.

## Exceptions

These are most of the work.

- **Settling after heater-on.** The probe sits in the pump housing and reads unmixed
  water. Today's rule discards two crossings. After a *power restore* the whole tub is
  stratified and the transient is far longer: on 08.10 it ran at least four crossings.
  Gate on a test rather than a count — hold until successive band rates stop falling
  faster than Newton allows, about 0.014 °C/h per crossing at a 21 K gap.
- **Before the window exists.** Show the scheduler's committed finish if there is one,
  otherwise a seeded estimate from the lift and a default tau.
- **Discard the window** on a data gap, on any temperature drop (cold water added, or a
  reading that settles back after mixing), and on a heater interruption.
- **Target change** keeps the window. The measured rate is still valid; only the
  destination moved.
- **Quantisation.** Require the window to span a real 1.5 °C, not a sensor artefact.
- **Guards.** If `rate <= 0`, or `tau` falls outside a plausible range, fall back to the
  opening estimate rather than publishing a number.

## Why tau cannot be fitted locally

Four crossings span about 6 K of gap and carry roughly 3 % noise each, so the standard
error on a fitted slope lands at 25–30 %. Done live on 08.10 the two-parameter fit
returned `tau = 5.15 h` and an asymptote of 36.5 °C — *below* the 39.5 °C target, meaning
the model would have reported that the tub never arrives.

This is the same failure that makes the `newton_ready_at` shadow wobble: its traverse fit
carries an 18 % standard error on the slope and puts the asymptote only 8.5 K above the
target, where at one sigma the low end falls below it.

A single band is no better. Across seven consecutive bands on 08.10 the derived tau ranged
from 22.89 to 33.60, a ±20 % swing, while the four-boundary window over the same stretch
held steady. The window is load-bearing, not a refinement.

## Observability: UV and wind

Solar gain and wind are **deliberately not modelled**. Both are suspected to matter and
neither is understood well enough to include.

On 08.10 the gap sat at 20.8–21.3 K for seven consecutive bands, because the air rose
almost exactly as fast as the water. That made it a clean experiment, and the rate still
moved by more than ambient could explain: between two bands the air gained 0.40 K and the
water 0.50 K, predicting a rate change of −0.004 °C/h against an observed +0.113. The sky
was clear and the UV index rose from 1.0 to 1.2 over the same window.

So that it can be settled later rather than guessed at now, **every crossing record also
stores**:

- `uv_index` — the suspected solar proxy. Cloud cover is the wrong variable; UV index is
  closer to the energy actually arriving.
- `wind_speed` and `wind_gust_speed` — kept separately rather than reduced to one figure,
  since it is not known which correlates.

These are written, never read by the model. A bounded append-only log of the last ~1000
crossings in the rates store is enough for a couple of months of runs and costs a few tens
of kilobytes. Each row needs timestamp, water, interval, time-weighted air, UV, wind,
gust, and the derived rate and gap — everything required to redo the regression offline.

Revisit in a few weeks with real data. If either proves to matter, the forecast already
carries both.

## The scheduler

Unchanged in structure: it must still predict from learned values, because at the moment
it decides when to start there are no crossings at all.

One improvement it should get: feed it the **settled tau from each completed run**. That
describes the water actually in the tub, where the cross-run `A` it uses today lags a
refill by several runs. On 08.10 the learned model thought the tub held 1257 litres while
the measured tau implied about 1000.

## Validation

Replay against recorded runs before shipping. The 08.10 run is the first validation set,
and the predictions logged during it can be scored directly: crossings at 32.0, 32.5 and
33.0 were called to within 5 minutes several hours ahead, against a learned-A estimate
drifting 15 minutes further behind at each one.
