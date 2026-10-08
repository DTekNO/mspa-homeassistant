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

- One prediction sensor, `ready_at_time`. The shadow pair `newton_ready_at` and
  `newton_start_at` are removed, and the display-string `ready_at` is deprecated.
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

### Which entities survive

`ready_at_time` is the one that stays. Its state is an ISO timestamp with
`device_class: timestamp`, which the frontend already renders in each viewer's own
format, and it already carries a `compact` attribute holding the same short string that
`ready_at` publishes as its state. So the string sensor adds nothing a dashboard cannot
get from an attribute, and `ready_status` already carries the words (`heating`,
`scheduled`, `ready`).

| Entity | Fate |
|---|---|
| `ready_at_time` | Keep. The single prediction. |
| `ready_at` | Deprecate, then remove. |
| `ready_status` | Keep. Carries the state words. |
| `newton_ready_at`, `newton_start_at` | Remove. The shadow is unsound — see below. |

This repository has never removed or renamed an entity, and people have these on
dashboards. So `ready_at` keeps working for at least one release, marked deprecated in
the changelog, and is removed in a later one. It must not disappear in the same change
that rewrites the prediction underneath it.

### Regimes

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

These are written, never read by the model.

### Saving it: a crossing log, not a timeseries

The water sensor only changes at half-degree boundaries, so **the crossing list is already
the full resolution of that measurement**. There is no finer signal to preserve and no
need to log raw polls.

The integration does have to persist it itself, rather than leaning on the recorder.
Home Assistant's recorder purges states after
`purge_keep_days` (ten by default) and keeps only hourly long-term statistics after that,
which destroys the crossing *times* — the one quantity this entire method rests on. Ten
days from now today's run is unanalysable.

**Write it to its own store, not the rates store.** The rates store is rewritten on every
poll; a thousand-row log there would mean rewriting roughly a hundred kilobytes every
thirty seconds, about 300 MB a day onto the Pi's storage. A separate store written only
when a crossing completes is about fifty writes a day.

One row per crossing:

| Field | Why |
|---|---|
| `t` | Crossing timestamp. The thing LTS destroys. |
| `water` | Temperature at the crossing. |
| `secs` | Interval since the previous crossing. |
| `air` | Time-weighted outdoor temperature over the interval. |
| `air_start`, `air_end` | So an alternative weighting can be tested later. |
| `uv` | Solar proxy. |
| `wind`, `gust` | Separate, until one is shown to matter. |
| `rate`, `gap` | Derived, stored so a row stands alone. |

Bound it at about a thousand rows. At forty crossings per run and a couple of runs a
week that is roughly three months, which comfortably covers the question being asked.
Nothing here is manual: the integration writes every row as the crossing happens, so no
history ever has to be downloaded by hand.

Revisit in a few weeks with real data. If either proves to matter, the forecast already
carries both.

## What happens to the stored coefficients

The store does not need clearing. It needs re-parameterising.

### From (A, tau) to (lift, tau)

Today `A` and `tau` each carry their own memory: `A` is blended at 0.35 per chord across
36 samples, `tau` is fitted once per run across 2. Nothing constrains their product. But
the product is the one physically stable quantity — it is mass-independent — while each
factor alone moves with the water level. The current scheme therefore puts a long memory
on the two things that genuinely change, and lets the thing that does not change drift
wherever those two happen to take it. That is why a refill takes several runs to work
through.

Invert it:

- **`lift`** gets the long memory. It is a property of heater power and insulation, and a
  refill does not touch it.
- **`tau`** is taken fresh from each completed run, because that is what tracks the water.
- **`A`** is no longer stored as a learned value at all. It is derived, `A = lift / tau`.

Today's run measures a tau near 28 h on the new water level. Under this scheme the
scheduler uses it on the next run; under the current one it would crawl from 36.68 toward
28 over several.

**Migration needs no surgery.** On first load, `lift = thermal_a × thermal_tau_h`, which
is 55.196 today and is the correct starting value.

### Key by key

| Keys | Fate |
|---|---|
| `lift`, `lift_n` | **New.** The only thing the nowcast needs from the store. |
| `thermal_tau_h`, `thermal_tau_n` | Keep, re-defined as the tau measured in the last completed run rather than a cross-run EMA. |
| `thermal_a`, `thermal_a_n` | Retire as learned values. `A` becomes derived. |
| `thermal_points`, `thermal_crossings_seen`, `active_prediction`, `temp_anchor_time`, `schedule_triggered` | Unchanged. Per-run working state. |
| `prediction_history`, `band_observations`, `band_stats`, `bias_evaluation` | **Keep — these become the validation set.** See below. |
| `prediction_bias`, `prediction_bias_applied` | Remove from the prediction path. |
| `heat_rate`, `cool_rate`, `heat_rate_buckets*`, `bucket_shape*`, `band_ambient_k`, `ambient_ref_c`, `bucket_save_ts`, `ambient_baseline` | Keep for now as the no-air fallback. Off the main path. |
| `newton_ready_at`, `newton_start_at`, `newton_fit`, `newton_implied_tub`, `newton_ambient_source` | Delete with the shadow sensors. |
| `forecast_resolution`, `forecast_hours`, `schedule_ambient`, `schedule_ambient_kind` | Keep. Forecast diagnostics. |

### The session records are the validation set

`prediction_history` and `bias_evaluation` hold the initial estimate and the actual heat
time for each completed session. They were collected to learn a bias. They are worth more
than that now: they are the only record of how well anything predicted, and they are what
the nowcast must be scored against before it ships.

So keep collecting them, and repoint the scoring to compare the nowcast rather than the
bucket variants.

### But drop the bias itself

`prediction_bias` is a multiplicative correction learned from how wrong the last sessions
were. The nowcast must not have one. A systematic error in a model that measures the tub
directly is a fault to find, not a coefficient to absorb it — and the existing bias was
learned against the bucket model in any case. Keep recording the ratio if it is useful to
watch; do not apply it.

### Why the buckets stay a while longer

The nowcast needs an outdoor temperature to compute a gap, so it cannot answer at all
without one. Until it has a no-air mode, the bucket model remains the fallback for an
installation with no weather entity. It leaves the main path immediately; it leaves the
codebase later.

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
