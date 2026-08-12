# Roadmap

Ideas and planned features that are not yet scheduled for a specific release.

---

## Hand the Scheduled Start to the User

Supersedes an earlier "pre-roll hook" plan — see *Why not a pre-roll hook* below.

### Problem

The scheduler's startup is deliberately minimal: at `start_at` it sets the target
temperature and turns the heater on. Nothing else. That is right for most spas, but
some hardware needs something different first.

Reported by a user whose flow sensor throws F1 unless water is already moving —
they run the jets briefly before the heater. There is no way to influence that
sequence, and no way to opt out of it.

Today the only workaround is to infer the moment from the Heat Schedule sensor's
`start_at` attribute:

```jinja
{% set s = state_attr('sensor.mspa_heat_schedule', 'start_at') %}
{{ s is not none and now() >= (s | as_datetime) - timedelta(minutes=5) }}
```

That latches rather than missing (the one-sided form is deliberate — a two-sided
window can be skipped entirely when `start_at` jumps earlier), but it is inference,
it can fire late, and it races the integration's own actuation.

### Design

One option, default off, best described to the user as **"Start the spa
automatically"** (on today's behaviour) versus handing startup over.

When handed over, at `start_at` the integration:

- fires `mspa_schedule_start` with `target_time`, `target_temperature`, `water_temperature`
  and the device id in the payload
- sets `_schedule_triggered` exactly as now
- **does not** set the setpoint and **does not** switch the heater on

Automations subscribe with `trigger: event`. The Heat Schedule sensor's state
transition is also already triggerable for anyone who prefers that, though only the
event carries the payload.

### Why not a pre-roll hook

The earlier plan was to fire an event `pre_roll` minutes *before* acting, then act.
It required **freezing the plan** for the duration, because otherwise the heater
could fire before the pre-roll elapsed (start moves earlier) or long after the
user's preparation finished (start moves later). Continuous re-planning is the
predictor's main virtue, so giving it up — even for ten minutes — was the plan's
worst compromise, and it needed a hard cap purely to bound the damage.

Handing over removes that entirely. The event *is* the action, so there is nothing
afterwards to mis-order and nothing to freeze. It also drops three of the four
must-haves the hook needed (persisting the commitment across a restart, cancelling
it on a new schedule, managing an `async_call_later` timer) and introduces no new
state at all — `_schedule_triggered` already means "fired once per schedule" and
already persists.

It is also more general. The hook could only *prepend* an action, capped at ten
minutes. Handing over lets a user own the sequence: jets then heater with any gap,
a staged temperature ramp, a cover check, or skipping the session entirely if
nobody is home.

### What it costs

Every user who opts in must reimplement the startup. For someone who only wants
"run the jets for two minutes, then behave normally", the hook was less work and
less to get wrong. Mitigate by documenting a copy-paste automation that reproduces
the default startup exactly, so people begin from something that works and edit
from there.

### Must-haves

- **A no-op safety net.** This is the one risk the hook did not have: if the user's
  automation is broken, disabled, or never written, nothing happens and the spa
  silently is not ready — while Ready at keeps counting down to a heat-up that never
  began.

  Watch for heating actually starting, and if the heater is still off ~5 minutes
  after the event, treat the handover as failed:

  - **Get the user's attention.** A `persistent_notification` is the right weight —
    HA-native, needs no configuration, and survives until dismissed. A log warning
    alone is invisible to exactly the user this protects.
  - **Stop predicting a fiction.** Abort the schedule rather than leaving it
    `triggered`, so Ready at and Heat Schedule stop counting down to a session that
    is not happening. Reporting nothing is more honest than reporting a confident
    time for a heater that is off, and it is the difference between a visible
    failure and a silent one.

  Whether it should *also* fall back to starting the spa itself is left open. Falling
  back is safer for the spa and matches what most users would want; it also defeats
  the point for someone who deliberately wants no heating this session — say a cover
  check that decided against it. A per-option choice ("notify only" versus "notify
  and take over") may be the honest resolution, but that is a second decision and
  should wait for a second user.
- **Fire at most once per schedule**, reusing the existing `_schedule_triggered` guard.
- **Fire even when no heating is needed.** With the spa already at target,
  `minutes_needed == 0` and `start_at == target_time`; the preparation step is still
  wanted, so the event must still fire.
- **Ready at must follow the real setpoint.** `_compute_ready_at` currently predicts
  to `sched_temp` once triggered, on the assumption the integration set it. If a
  user ramps to something else, the ETA is measured against a target that was never
  applied. Either track the live setpoint after handover or document the estimate as
  approximate in this mode.

### Notes

Granularity is bounded by `DEFAULT_SCAN_INTERVAL` (60 s), so the event can arrive up
to a minute after the nominal `start_at`. That is inherent and suits the motivating
case, where a couple of minutes of jet flow is what clears the sensor.

Not built yet, deliberately. One user with a sticky flow sensor is enough to know the
shape is right but not enough to fix the payload or settle the fallback question. If
a second request arrives, that will say which.

---

## Extracting the Prediction Engine

An idea, not a plan: the scheduler and prediction model are not really about MSpa.
Any spa — any heated body of water — could use them. Extracting them would let other
integrations benefit, while mspa keeps them built in so its users install one thing.

### Where the seam is

The engine is nearly pure computation over a time series. It consumes water
temperature, setpoint, a heating-active signal, and optionally outdoor conditions;
it produces learned rates, an ETA, and a start time. What is genuinely MSpa-specific
is everything else: the API client and auth, quota and polling behaviour, the device
features, the climate entity, and the actuation calls.

Candidates to move: rate learning (`_track_heating_rate` / `_track_cooling_rate`,
the phase-uncertainty guard, buckets, session scalar, prediction bias, ambient
factor), the ETA and start-time maths (`_anchor_eta_utc`,
`_segmented_heating_minutes`), and the learned-state persistence. The Ready at and
Heat Schedule sensors are presentation and would probably stay, consuming the
engine.

### Distribution, without PyPI

- **Vendored via git subtree or submodule.** One source repo; mspa carries the module
  inside `custom_components/mspa/`. Subtree keeps the files really present, so the
  release zip works with no CI changes, at the cost of manual sync. Submodule keeps
  history clean but the release workflow must `git submodule update --init` before
  zipping, or the zip ships an empty directory.
- **CI-synced copy.** A workflow copies the module into mspa on release — the same
  pattern already used to keep ha-alert-card's `dist/` from drifting, so the
  machinery and the habit both exist.
- **A separate HACS integration that mspa depends on.** Rejected: HACS does not
  auto-install dependencies for custom integrations, so users would have to install
  two things, which breaks "built in to mspa".

The likely answer is a module in its own repo, vendored into mspa, plus a thin
standalone integration wrapping the same module for other spas. Two copies can
coexist in one HA instance without collision, since `custom_components` namespaces
them — an mspa user simply would not install the standalone one.

### Why not yet

The extraction is **cheaper later and riskier now**, which is the opposite of the
usual advice, for one reason: the interface is not settled.

The algorithm changed three times in the week of 2026-08-03 alone (phase
uncertainty, the latch lifecycle, ETA smoothing), and the weather work in *Learned
Weather Factor* will change what the engine consumes — it needs each observation
tagged with its conditions, which is a new input, and it will probably retire
`prediction_bias` as a weather carrier. Freezing a public interface across that
would slow the work that matters.

There is also real coupling that only looks incidental. The phase-uncertainty guard
depends on knowing the reporting quantum (0.5 °C bands) — a device property, not a
physical one. Other spas may report differently, so that has to become a parameter.
The prediction-record gating depends on `heat_state == 3`, which is MSpa's notion of
full-heat mode. Both are fine as parameters, but they show the boundary needs
designing rather than merely drawing.

### The cheap step that de-risks it

Keep tightening the seam *inside* mspa, which is worth doing on its own merits.
Move the prediction functions into a `predictor.py` that imports nothing from Home
Assistant and never reaches into `coordinator` — taking explicit arguments and
returning values instead. `_track_heating_rate` / `_track_cooling_rate` were already
pulled out of the update loop for testability; this is the same move continued.

That gives a testable, dependency-free core now, makes the eventual extraction
mechanical rather than exploratory, and costs nothing if the split never happens.

---

## Learn from a Stored Sample Series, not Incremental State

Supersedes the streaming approach that *Rate Sampling over a Growing Window*
improved (implemented 2026-08-07). That fix made each observation far less noisy,
but the estimator still learns from state carried forward poll to poll, and that
state is the fragile part: every defect fixed on 2026-08-06/07 was a symptom of it.

### Idea

Keep a small persisted series of `(timestamp, water_temperature, heat_state)` —
appended only when the temperature or heat mode changes — and derive rates from
that series instead of from an anchor advanced in flight.

A 16-hour heat-up produces roughly 35 temperature changes, so this is a couple of
kilobytes per session. Keeping the last handful of sessions is ~10 kB, alongside
the learning state already stored in `_rates_store`.

### Source: the recorder, or our own series

Both are viable — an earlier draft of this entry wrongly ruled the recorder out on
the grounds that the entities are disabled by default. That is true of the dedicated
`water_temperature` and `heat_state` diagnostic sensors, but **the climate entity
carries the same two signals and is enabled for everyone**:

```python
def current_temperature(self):
    return self.coordinator.last_data.get("water_temperature")   # identical value

def hvac_action(self):
    if heat_state == 3: return HVACAction.HEATING                # the qualifier
```

It declares no `_unrecorded_attributes`, so both are in history, and `hvac_action`
distinguishes preheating, heating, idle and off — exactly the heat-mode qualifier
that makes a span usable.

Both arrive in the *same* attribute set, so one recorder row carries the
temperature and the heat mode together — no correlating two entity histories, and
no risk of pairing a temperature with a heat mode from a different instant:

```
current_temperature: 37
temperature: 39.5
hvac_action: heating
```

**Recorder — pros:** no new storage; no new state to persist or migrate; and
decisively, **it works retroactively on data already collected.** Weeks of climate
history exist right now, so the weather fit in *Learned Weather Factor* could be
bootstrapped from real sessions across varied conditions instead of waiting a
season to accumulate them. That is a large shortcut for the item this entry is a
prerequisite to.

**Recorder — cons:** retention is finite (default ten days), so it cannot be the
long-term memory; the recorder is optional and users can exclude entities; queries
must run through `recorder.get_instance(hass).async_add_executor_job` with
`after_dependencies: ["recorder"]`; and it ties the estimator to Home Assistant in
the module *Extracting the Prediction Engine* wants dependency-free.

**Long-term statistics are a third source, and the best one for bootstrapping.**
Unlike states, LTS are *never purged*. Both signals qualify: `water_temperature`
declares `SensorStateClass.MEASUREMENT`, and `heat_state` is in `MEASUREMENT_KEYS`
so it gets one too — so wherever those sensors have been enabled, hourly mean/min/max
exist back to the day they were switched on.

Two properties make this better than reconstructing from crossings:

- **A fixed one-hour window has no timing jitter.** Differencing consecutive hourly
  means gives the rate directly, over a window that is exactly an hour every time.
  That removes the entire noise source this section exists to fight: the 15% scatter
  measured on 0.5 °C crossings came from a few minutes of report-timing variation on
  a ~30 minute span. Errors also partially cancel across the difference, so hourly
  statistics may well beat the growing window as well as predating it.
- **`heat_state` mean == 3.0 exactly means the whole hour was full heat.** Any
  interruption — preheat, idle, cycling — drags the mean below 3, so the qualifier is
  a single equality test rather than an interval reconstruction. Hours that fail it
  are simply dropped.

Combined with hourly outdoor temperature from a local weather sensor, that yields
`(rate, water_temp, ambient)` for every clean heating hour since the spa was
commissioned — evenly spaced, conditions attached, which is precisely the dataset
*Learned Weather Factor* needs and the form it wants it in.

**Caveats specific to this route:**

- **Entity renames break continuity.** Statistics are keyed by `statistic_id`, which
  follows the entity id. Renaming through the UI migrates them, but removing and
  re-adding the device does not — the old series is orphaned, still queryable if the
  old id is known. Developer Tools → Statistics lists orphaned ids, flagged as no
  longer provided, so the history is recoverable but the ids must be collected first.
- **Only hourly is durable.** Five-minute statistics are short-lived; hourly is the
  resolution to design against, which happens to be the resolution wanted.
- **Hourly buckets straddle temperature buckets.** An hour spanning 29.5 → 30.5 °C
  belongs to neither cleanly. Either attribute by the hour's mean temperature and
  accept slight smearing, or keep only hours wholly inside one bucket.
- **Local sensor versus met.no.** A local outdoor sensor better represents the
  microclimate the spa actually loses heat to, and is the right choice for the
  temperature term. met.no is the fallback where local history is short, and the
  source for *wind*, which a basic weather station does not measure and the
  `sqrt(wind)` term needs. Watch for placement bias — a sun-exposed module reads
  high in the afternoon, which could alias with time of day.

**Answer: three sources, three jobs.** Our own short series as the authoritative
learning input — portable, immune to purge and user config, keeps the estimator
dependency-free. Long-term statistics to *bootstrap and backfit*, giving months of
conditioned observations immediately instead of after a season. Recorder states only
if sub-hourly detail is ever needed for a specific investigation. The latter two are
optional and may degrade to unavailable without affecting normal operation, keeping
the coupling at the edge rather than in the core.

### What it buys

- **Phase uncertainty becomes a once-per-session cost, not once-per-restart.**
  This is the strongest argument. The uncertainty is a property of a single
  moment — heater-on, when the water sits at an unknown position inside its
  0.5 °C band. The first crossing resolves it: from then on the water is known to
  have been *exactly* on a boundary at a *known* time, and that fact does not
  expire. But it currently lives in volatile state, so every restart discards it
  and re-pays a penalty physics charges only once. The 2026-08-06/07 session paid
  it three times across two restarts and a hot deploy. Worse, now that the window
  grows, a restart truncates an accumulated 2 °C span back to 0.5 °C — so the
  streaming design makes restarts *more* damaging than they were, which is itself
  a reason to move off it.

- **Restart immunity generally.** `_rate_last_temp`, `_rate_last_time`,
  `_rate_first_step`, `_session_scalar` and `_session_fresh_buckets` are all lost
  on restart today. From a stored series nothing is lost, because the rate is
  recomputed from the samples rather than accumulated.
- **Retrospective recomputation — the big one for development.** Every algorithm
  change currently needs a fresh 16-hour session to evaluate. With the samples
  stored, a new rule can be replayed against past sessions immediately. This is
  exactly what was done by hand from a log and a CSV on 2026-08-07, and it found
  the truth the in-flight learner had missed: the cold bucket had drifted to
  0.98 °C/h against a realised 1.14.
- **Robust fitting instead of a bounds check.** Outliers can be rejected with
  hindsight rather than by a fixed `_MIN_HEAT_RATE`/`_MAX_HEAT_RATE` window. The
  paired reporting glitch of 2026-08-06 — 27.5→28.0 at 0.51 °C/h then 28.0→28.5 at
  29.5 °C/h, one report late and the next early — is obvious in a series and
  invisible to a streaming bounds test, which rejected the impossible half and
  learned the spurious half.
- **Gap tolerance.** If HA is down for an hour mid-session, the series has both
  endpoints and the span is still measurable, provided heat mode held.
- **It subsumes the growing window.** A window is the streaming approximation of a
  fit over a span. With the series available, the bucket rate becomes a regression
  over that bucket's samples, and the anchor/close/reset rules stop being needed.

### Open questions

- How many sessions to retain, and whether to keep raw samples or per-session
  summaries once a session is old. Raw is more useful for replay; summaries are
  smaller.
- Whether the series should carry ambient conditions per sample. *Learned Weather
  Factor* needs observations tagged with their conditions, and doing it here is
  cheaper than a parallel mechanism — but it widens each record.
- Whether rate learning should then run only at session end, or continuously over
  the series so far. Continuous is closer to today's behaviour and keeps the ETA
  responsive; session-end is simpler and probably more accurate.
- Interaction with `heat_state` gaps. Thermostat cycling near setpoint fragments a
  session into many short heating spans; the series makes it possible to stitch
  those by accumulating heating time only, which the streaming version cannot do.
- Could the *initial* phase uncertainty be reduced too, rather than merely paid
  once? The series also covers the cool-down before heating, and a cooling
  trajectory reveals when the water *entered* its current band. Knowing that entry
  time and the cooling rate bounds where inside the band it sat at heater-on, which
  would make even the first span usable instead of discarded. Speculative, and it
  depends on the cooling rate being known better than the band is wide — but the
  data to test it would be sitting there.

---

## Alternative: a physical heating model instead of buckets

**Status 2026-08-10: fitted, and it holds.** Previously recorded as untestable from a
single session; the long-term statistics settle it.

Newton's law gives `dT/dt = (T_air + P/k - T_water) / tau` for a heated body losing
heat in proportion to `(T_water - T_air)`. Two parameters replace three bucket
constants, rate falls *linearly* with water temperature — so the buckets are a
piecewise-constant approximation of a straight line — and the weather influence falls
out of the physics instead of needing a sensitivity table.

### The test, and why it is a real test

The law makes a falsifiable prediction: regress rate on water *and* air temperature
and the two coefficients must come out **equal and opposite**, both equal to `1/tau`.
Nothing about a curve fit forces that; it either happens or the model is wrong.

On 639 clean full-heat hours from the previous spa, spanning −9 to +20 °C outdoor:

```
water  -0.0161  ±0.0013        air  +0.0167  ±0.0013        ratio 1.04
```

**Equal and opposite to within 4%**, on coefficients individually significant at
t ≈ 12. That implies `tau` = 62 h and an asymptote 68.6 °C above air temperature.

It also fits better than the buckets on the same data, with one parameter fewer:

| model | params | rms |
|---|---|---|
| Newton (water + air) | 2 | **0.206 °C/h** |
| three buckets | 3 | 0.235 °C/h |

### Why the earlier attempt failed

Fitted on the single 2026-08-06/07 session it gave `tau` 152 h and an asymptote of
189 °C — the degenerate fit familiar from the well recharge curve, where a nearly-flat
rate lets the asymptote run to infinity to imitate a straight line. That was a
sample-size problem, not a model problem: 34 steps over a 17 °C span with 0.1 °C/h of
per-step noise cannot resolve the curvature, and 639 hours across a 29 °C outdoor
range resolves it easily.

### What is not yet settled

The **current** spa gives water −0.0390 ±0.0034 against air +0.0214 ±0.0045 — a ratio
of 0.55, not 1.04. Both coefficients are individually strong, but there are only 192
hours over a 2.7–21.4 °C outdoor range, so the air term is poorly constrained. The
implied `tau` of 25.6 h is plausible for a smaller tub than the 62 h above, but the
split should not be trusted until this machine has a winter behind it. The statistics
are accumulating; re-run the fit in spring.

### What adopting it would remove

- **`AMBIENT_SENSITIVITY` and its baseline entirely.** The air-temperature term *is*
  the weather model, so there is no gain to calibrate, no reference conditions to pin,
  and none of the self-cancelling behaviour described under *Learned Weather Factor*.
  This is the strongest argument for it — it deletes a subsystem rather than fixing one.
- The three bucket constants and their per-bucket correction precedence.
- Most of `prediction_bias`, which should converge near 1.0 once the shape is right.

### Decision

Unchanged in the short term: buckets predicted the 2026-08-06/07 session to within
**2 minutes** (992 estimated against 994 actual, 0.2%), so there is nothing to gain
on accuracy today. What has changed is that the alternative is now evidence-backed
rather than appealing, and it removes more code than it adds. Revisit when the current
spa has winter data, or sooner if the bucket shape keeps drifting flat — live buckets
read 1.03 / 0.99 / 1.01 against a statistics-derived 1.263 / 1.086 / 0.841, and a flat
shape is exactly what a straight line would fix.

## Learned Weather Factor

> **Read *Alternative: a physical heating model* first.** On 639 hours of statistics,
> Newton's law reproduces the air-temperature dependence with coefficients equal and
> opposite to within 4%. If that model is adopted, most of this entry dissolves: there
> is no separate gain to learn and no reference conditions to pin, because the air term
> is the weather model. Everything below remains the plan for keeping the bucket
> approach; it is no longer the only option.


### Motivation

The prediction model currently corrects for outdoor conditions in two places, and neither of them actually *learns* the relationship.

**`ambient_rate_factor` uses hardcoded sensitivities.** `AMBIENT_SENSITIVITY = (0.0, 0.02, 0.06)` — a fraction of the bucket's rate lost per °C below the learned baseline — was derived from physical reasoning about the water-to-air temperature gap, not measured. No installation has ever confirmed those numbers. A well-insulated spa with a rigid cover might sit at half those values; an uncovered one in an exposed garden could be double.

**Wind is recorded but never used in the rate model.** `_read_weather_entity` reads wind speed, and it was used by the prediction-bias kernel until 2026.7.2, but `ambient_rate_factor` only takes temperature. Wind drives evaporative loss, which on an uncovered spa is plausibly a *larger* term than conduction. Observed sessions have ranged from 2 m/s to 10.5 m/s — a big uncontrolled variable sitting in the residual.

**Worst of all, the bucket rates absorb weather variation.** There is one EMA per temperature band, fed by every session regardless of conditions. A cold-night observation drags the bucket down; a warm-day observation drags it back up. The bucket therefore converges on an average of whatever weather happened recently, rather than a property of the spa. Time-decay on stored rates exists purely to paper over this — "forget summer before winter arrives" — which discards good data to work around a modelling gap.

The consequence is that seasonal knowledge cannot accumulate. After a full year of operation the integration is no better at predicting a cold January morning than it was in its first week, because every winter observation has been averaged against summer ones and then decayed away.

### Concept

Learn the weather sensitivity from observation, persist it, and evaluate it at current conditions. Split the rate into what the spa does and what the weather does to it:

```
observed_rate  =  base_rate[bucket]  ×  weather_factor(ambient_temp, wind)
```

Two consequences follow, and they are the whole point of the change:

- **When an observation arrives**, divide out `weather_factor` *before* updating the bucket EMA. The buckets become "rate at reference conditions" — a genuine property of the spa — and stop absorbing weather at all.
- **When predicting**, evaluate `weather_factor` at today's conditions. No similarity matching, no history window.

Crucially this means **every session teaches the slope**. A January run and a July run both inform the same coefficients, so winter data improves a summer prediction instead of being discarded as irrelevant. That is the opposite of the old bias kernel, which could only ask "which of my last 10 sessions resembled today" and threw away everything that did not.

### Parameterisation

Data is sparse — roughly one session per day, each yielding a handful of 0.5 °C rate observations. A full model (base rate plus temperature and wind coefficients per bucket = nine free parameters) would overfit badly.

Start with **one learned parameter**: a scalar weather gain `g`, applied to the existing physically-motivated per-bucket shape.

```
sensitivity[i] = g × SHAPE[i]        where SHAPE = (0.0, 1.0, 3.0)
```

`SHAPE` preserves the physics — heat loss scales with the water-to-air gap, so the near-setpoint bucket is affected far more than cold water — while `g` calibrates the magnitude to the actual installation. It cannot invert the ordering, converges within a handful of sessions, and reduces to current behaviour at `g = 0.02`.

Add a second parameter for wind the same way once the temperature gain is stable, ideally on a `sqrt(wind)` or capped-linear term rather than raw linear, since evaporative loss does not grow without bound.

Fit online by least squares on the residual: for each observation, compare the observed rate to the rate the model predicted at those conditions, and nudge `g` in the direction that reduces the error. Recursive least squares or a simple gradient step with a small learning rate both work; store the running sample count so the gain can be trusted proportionally to the evidence behind it.

### Bootstrap

No cold start needed. `prediction_history` already persists `ambient_temp`, `ambient_wind`, `estimated_minutes`, `actual_minutes`, `start_temp`, and `target_temp` per session. That is enough to fit an initial `g` offline from existing installations' stored data, and to sanity-check whether 0.02/0.06 was in the right region at all.

### Instrumentation first

The precondition for building this is training data at observation granularity, which is not currently logged. Before the model itself:

- Log every accepted rate observation as `(bucket_index, observed_rate, ambient_temp, wind, cover_state_if_known)`.
- Persist a rolling window of these — considerably more than the 10 sessions kept now, since the fit needs spread across conditions, and the whole point is multi-season coverage.
- Add a diagnostic sensor exposing the current gain, its sample count, and the residual spread, so the fit can be inspected on the device page.

Shipping the instrumentation early means data accumulates while the existing model keeps running, so by the time the fitting code is ready there are real coefficients to validate against rather than guesses. It also composes well with the opt-in analytics work below — the same per-observation records are exactly what would let sensitivities be compared across models and climates.

### What this removes

- **The weather kernel in the bias.** Already removed in 2026.7.2 for stability reasons; this change is what makes its absence correct rather than merely safer, because weather moves to where it belongs — the rate.
- **Most of the time-decay on stored rates.** Once rates are condition-normalised, a summer rate *is* a winter rate. Decay can be relaxed substantially or dropped, keeping hard-won learning instead of bleeding it away.
- **Much of the residual role of `prediction_bias`.** It should converge close to 1.0 and stay there. Keep it as a genuine catch-all, but it stops carrying weather.

`session_scalar` stays, and becomes more clearly defined: the in-session correction for what the weather model *cannot* see — cover left off, water level, a windbreak, an unusually cold fill.

### Open questions

- **Within-bucket rate decline.** Rate sampling now measures over a growing
  per-bucket window (implemented 2026-08-07), which gives the bucket's *average*
  rate — the right input for predicting the whole bucket, but slightly optimistic
  for its last stretch. The 2026-08-06 mid bucket showed no strong monotone trend
  across eight steps, so the effect looks second-order against the 15% → sub-1%
  noise reduction. Worth re-checking on a session that reaches the hot bucket,
  where the decline should be steepest — and it interacts with this fit, since a
  condition-normalised rate wants clean samples more than plentiful ones.
- Is `sqrt(wind)` the right form, or is a cap at moderate wind speeds sufficient? Needs data.
- Should the gain be learned per bucket after enough evidence accumulates, or is the fixed `SHAPE` good enough indefinitely?
- Cover state is the largest unmodelled variable and the integration has no way to know it. Worth an optional `binary_sensor` config option so users who have instrumented their cover can feed it in?
- How should the gain behave when a user relocates the spa or adds insulation — is a manual reset needed, or does a slow forgetting factor on the fit handle it?

---

### Measured evidence (long-term statistics, 2026-08-07)

Hourly long-term statistics make this measurable retrospectively. Differencing
consecutive hourly mean water temperatures gives a rate over an exactly-one-hour
window, and requiring the `heat_state` hourly mean to be *exactly* 3.0 keeps only
hours that were full-heat throughout. That yielded **831 clean heating hours** across
14 months and an outdoor range of −8.6 to +21.4 °C.

Two spas are present in the history and must not be pooled for absolute rates — the
previous unit had a different pump and heater. Like-for-like in the mid bucket at
5–15 °C outdoor: 0.716 °C/h against 1.063 °C/h, 48% apart. Sensitivity is a
*relative* quantity, so the older data still informs the shape, and it is the only
source of sub-zero observations.

**Measured sensitivity, current spa** (n=192, outdoor 2.7–21.4 °C, 95% CI):

| bucket | measured | 95% CI | `AMBIENT_SENSITIVITY` | verdict |
|---|---|---|---|---|
| cold <30 | +0.0005 | −0.018 .. +0.019 | 0.00 | consistent, but uninformative |
| mid 30–37 | +0.0238 | +0.011 .. +0.037 | 0.02 | **confirmed** |
| hot ≥37 | +0.0269 | +0.009 .. +0.045 | 0.06 | **too steep — 0.06 is outside the CI** |

**Measured ratio to the 10–15 °C rate, previous spa** (the only winter data):

| bucket | −15..−5 | −5..0 | 0..5 | 5..10 | 10..15 |
|---|---|---|---|---|---|
| cold | 0.58 (7) | 0.55 (43) | 0.60 (50) | 0.69 (77) | 1.00 (20) |
| mid | 0.41 (5) | 0.66 (35) | 0.68 (42) | 0.73 (61) | 1.00 (41) |
| hot | 0.59 (6) | 0.70 (40) | 0.87 (60) | 0.83 (49) | 1.00 (29) |

### Two conclusions that change the plan

**1. The hot-bucket sensitivity is roughly twice too steep, and it clamps absurdly
early.** At 0.06, the factor reaches the `AMBIENT_FACTOR_MIN` floor of 0.30 at about
+3 °C outdoor — so the model predicts the spa almost stops heating at freezing. The
well-powered −5..0 °C bin (35–43 samples per bucket) measures 0.55–0.70. The owner's
account of a January session at −10 °C being unproblematic matches the data and not
the code. Sub-zero bins have only 5–7 samples each, so treat the very coldest column
as indicative; the −5..0 column is solid.

**2. The self-cancelling baseline has been masking the bad calibration — so fixing
the baseline alone would make winter predictions materially worse.** Because
`ambient_baseline` follows the weather (see the session evidence below), the factor
returns ~1.0 whatever the season and the mis-calibrated sensitivity never fires. Pin
the reference conditions without re-deriving the gains and every winter prediction
would suddenly be corrected by a factor that is twice too severe. **The two must
change together**, and the sensitivities should be *measured* from this dataset
rather than kept as constants.

Both are reasons to prefer the plan in *Concept* above — learn the gain from
observations rather than hardcode a sensitivity — over merely repairing the baseline.

### Remaining gaps

- **No sub-zero data for the current spa.** It was commissioned at Easter 2026, so
  its record starts in April and reaches only 2.7 °C. Winter sensitivity for *this*
  machine is extrapolated. One cold season fixes it, and the statistics are already
  accumulating.
- **Outdoor temperature explains only 11–16% of the variance** (r² 0.114 mid, 0.155
  hot). Weather is real but secondary — consistent with the session evidence below,
  where the rate model was accurate to 1% and `prediction_bias` caused the whole
  46-minute error. Expect the weather work to sharpen predictions, not transform them.
- **Cover state is unmeasured** and is the obvious candidate for much of that
  unexplained variance.
### Wind: measured, and not worth modelling (2026-08-10)

**Result: drop the `sqrt(wind)` term.** Wind adds no useful explanatory power once
water and air temperature are accounted for.

| | r² without wind | with gust | t on the gust term |
|---|---|---|---|
| current spa (n=192) | 0.448 | 0.456 | −1.6 |
| previous spa (n=639) | 0.328 | 0.329 | −1.0 |

`sqrt(gust)` is no better (t = −1.8 and −0.9). The sign is physically right — more
wind, slower heating — but the magnitude is indistinguishable from zero on either
dataset, including the 639-hour one spanning −9 to +20 °C and gusts of 1.1 to
19.5 m/s. Wind was not hiding inside the temperature term either: `corr(air, gust)`
is only −0.26 and +0.39.

Data: [seklima.met.no](https://seklima.met.no/), station **SN50500 Flesland**,
14,016 hourly rows from 2025-01-01, elements *Høyeste vindkast (1 t)* and *Høyeste
middelvind (1 t)*. No registration needed — this is the route to use for any future
weather question, in preference to the Frost API.

Two traps worth remembering:

- **`norsk normaltid` is CET all year, with no daylight saving.** So UTC = CET − 1 h,
  *not* the local Norwegian offset, which is +2 in summer. Getting this wrong shifts
  every join by an hour in summer and looks like noise.
- Values are semicolon-separated with **decimal commas**, and the last line is a
  licence notice rather than data.

**The station-versus-sensor mismatch turned out to be minor**, contrary to the
caution recorded earlier. The stored prediction record read `ambient_wind: 11.5 m/s`
at 2026-08-06T19:26Z from the weather entity; Flesland's 19:00Z hour recorded a
11.4 m/s gust. Agreement to 1%, so a coefficient fitted on this station would have
been directly usable — the concern was overstated.

## Optional External Temperature Probe

**Status: wanted, blocked on hardware availability** (owner's Ondilo ICO out of service,
expected back late August 2026). Raised 2026-08-12.

A floating thermometer in the tub measures the water the user actually cares about,
independent of pump state. That makes it the clean answer to the pump-housing problem
below — and because housings differ between spa models there is no per-housing offset to
calibrate, so this is the *only* answer.

It is a bigger change than "read a different entity", because most of the awkward
machinery in the predictor exists to work around the built-in probe's limitations.

**What it would let us delete or simplify**

| Machinery | Why it exists | With a 0.1 °C tub probe |
|---|---|---|
| Band-centre anchoring | 0.5 °C quantization; true value known only at a crossing | Unnecessary, or a 0.1 °C band |
| Phase-uncertainty guard | First crossing after a state change measures position, not rate | Unnecessary — every sample is usable |
| Growing per-bucket window | Crossings are scarce and noisy | Plain regression on dense samples |
| Three-bucket rate model | Too few samples to fit anything better | Fit the physical model directly |
| `prediction_bias`, ambient factor | Empirical patches over an empirical rate model | Largely subsumed by the physical fit |
| Schedule-start deadband | 33 min lumps per crossing | Harmless but no longer needed |

The last row of that table is the real prize. Newton's law already fits the recorded
data to 4% over 639 hours, but it cannot be used as *the* model while the input is
quantized to 0.5 °C and only trustworthy while the pump runs. Dense tub-temperature data
makes `dT/dt = (T_air + P/k − T_water)/τ` directly fittable, which would replace the
buckets, the ambient factor and the bias with one physical model — the direction the
[Learned Weather Factor](#learned-weather-factor) work already points in.

It would also make the quantization miss documented in the README mostly disappear, since
the start time would move smoothly rather than in lumps.

**What it must not break, and this is the subtle part**

*The spa's own thermostat still runs off the internal probe.* The device decides it is
finished when **its** sensor reads the setpoint, so "done" is defined in internal-probe
terms no matter what the external probe says. Predicting from the external probe alone
could therefore promise a time the device never honours.

The split that resolves it: use the external probe to know the **starting** temperature
and to learn **rates** (both are questions about the tub), and keep the internal probe as
the definition of **done**. During heating the pump is running and the two agree closely,
so the mismatch is confined to planning while idle — which is exactly where the external
probe helps and the internal one cannot.

**Guards needed**

- Fall back to the internal reading when the probe is unavailable, stale, or absent —
  and expose which one is in use, alongside the existing `temperature_basis`.
- A staleness threshold: a battery probe reporting every 15–60 min is not a substitute
  for a 30 s poll during a heat-up.
- Do not assume the probe is truth either. A *floating* probe reads surface water, which
  runs warmer than the bulk while heating — the real stratification, as opposed to the
  housing artefact that was mistaken for it.
- Unit handling has to go through the existing temperature-unit control.
- Rates learned from a probe must not be blended with rates learned from the internal
  sensor; they measure different things. A probe being added or removed should be treated
  as a new learning regime, not a continuation.

**Not in scope:** sampling the tub by briefly running the pump. Considered and rejected
2026-08-12 — the integration should not start hardware to take a measurement.

### Cooling-trajectory band position: measured, deferred (2026-08-11)

**Result: keep band-centre anchoring; do not extrapolate the cooling trajectory yet.**
Model in `analysis/anchor_position.py`, re-runnable against the private exports.

The proposal was to place the water *within* its 0.5 °C band at a heating start, by
extrapolating from the last cooling crossing at the learned cool rate, rather than
holding the band centre. A heating start makes this falsifiable: if the water sits a
depth *d* below the upper threshold, the first crossing must arrive after *d* / heat
rate. Scored on the two recorded starts that follow a cool-down:

| rule | assumed depth | predicts | observed 1.0 min |
|---|---|---|---|
| verbatim reading (pre-fix) | 0.250 °C | 13.5 min | **+12.5 min** |
| band centre (shipped) | 0.000 °C | 0.0 min | **−1.0 min** |
| cooling trajectory (proposed) | 0.100 °C | 5.4 min | **+4.4 min** |

So the band-centre fix is vindicated against the old behaviour — wrong by 12–14 min at
every session start — but the trajectory refinement would make it *worse*. It expects
drift during the 55 min the water sat idle; the crossing came one poll after heater-on.

**A competing explanation fits better than band position, and it is not
stratification** (owner, 2026-08-12, superseding the guess recorded here first).
**The probe sits in the external pump housing, not in the tub.** With the circulation
pump off it measures a small separate volume of stagnant water, which loses heat faster
than the insulated tub and so reads at or below the real tub temperature. When
circulation resumes the reading steps up to the tub's actual temperature — not because
the water heated, but because the probe started measuring different water.

The following band took 22.6 min against a 25–28 min run average, so the heat the step
revealed was real and already in the tub, which fits this reading exactly.

This is a hardware placement fact, not a modelling gap, and it has a hard consequence:
**with the pump off, the tub temperature is not measured and cannot be recovered.** No
extrapolation reaches it. The bias direction is benign — a cold reading over-states the
work remaining and starts early — so the integration reports the condition rather than
attempting to correct it, via the `circulating` and `temperature_basis` attributes on
Ready at and Heat Schedule. Sampling by briefly running the pump was considered and
ruled out as out of scope.

**The cooling law itself is now measured**, from 60 idle stretches ≥6 h in the hourly
statistics, spanning a 5–30 °C water-air gap:

| gap band | n | mean gap | mean rate | implied τ |
|---|---|---|---|---|
| 0–10 | 7 | 6.6 | 0.084 | 79 h |
| 10–15 | 8 | 13.3 | 0.129 | 104 h |
| 15–20 | 18 | 17.8 | 0.157 | 113 h |
| 20–25 | 19 | 22.2 | 0.279 | 80 h |
| 25–40 | 8 | 27.3 | 0.437 | 62 h |

Newton linear (`rate = gap / 81 h`) and a power law (`0.00108 · gap^1.75`) fit the
observed range comparably (RSS 0.622 vs 0.568); cooling accelerates slightly faster
than linearly, as evaporation does. Recorder crossings agree at full resolution
(69/103/84 h over one cool-down).

**The stored `cool_rate` scalar is wrong almost everywhere.** At 0.363 °C/h it matches
only a ~29 °C gap — it was learned from hot water. Near ambient it overstates cooling
**4×**. (These stretches were all circulating, so they do describe the tub.) Any future use of it for extrapolation must be replaced by `cool_rate(water −
air)`, which makes the correction depend on the weather entity and means it has to
degrade to band-centre anchoring when there isn't one.

**When it would be worth having** — drift beyond band-centre, as minutes of ETA at the
hot-bucket rate, capped at one band (a cell at 39 min means the anchor has simply gone
stale and a crossing would have re-anchored):

| water | air | gap | 15 min | 30 min | 60 min | 120 min |
|---|---|---|---|---|---|---|
| 22 | 14 | 8 | 2 | 4 | 8 | 15 |
| 30 | 10 | 20 | 5 | 10 | 19 | 39 |
| 38 | 0 | 38 | 9 | 18 | 37 | 39 |
| 38 | −10 | 48 | 12 | 23 | 39 | 39 |

Negligible in the summer regime every recorded session sits in; 20–39 min once the air
is near zero with the water hot — the same order as the disagreement the anchor fix
just removed. **Cancelling a heat-up and restarting it reaches that regime in any
season**, so this is not only a winter question.

### Implemented for circulating water, 2026-08-12

Three corrections to the analysis above, all from the owner, and together they turned
this from "rejected" into "shipped, narrowly scoped".

**The step at heater-on is an artefact of where the probe is, not of mixing.** It sits in
the external pump housing; with the pump off it reads a separate stagnant volume. So the
2026-08-11 scoring above was comparing rules against data that was not measuring the tub,
which is why the trajectory rule looked wrong. It was not being tested fairly.

**The scheduler plans while the *heater* is idle, not while the spa is idle** — the pump
can perfectly well be circulating then, and it is recommended that it is. That is the
window the extrapolation needed, and it is available.

**The conservatism objection was arithmetic error on my part.** I claimed smoothing cost
~0.25 °C of margin and ~20 min of start time. It costs nothing: the estimate runs half a
band warm just after a crossing and half a band cold just before the next, and the
reported reading *is* the mean of the two. Measured over a dwell the mean difference is
0.0 min. The clamp also makes the hand-off exactly continuous — a full band of drift
lands on the anchor the next crossing will set.

**Also corrected: the cooling rates were all measured on circulating water**, so τ ≈ 81 h
and the stored `cool_rate` describe the tub after all. An earlier note here claiming they
might describe the housing was wrong and has been removed.

**What shipped.** `predictor.extrapolate_within_band()` (pure, clamped) and
`coordinator.scheduling_temp()`, used by both the trigger and the Heat Schedule display
so the two cannot diverge. The planned start now ramps in even steps through a dwell
instead of sitting flat and lurching 32 min at each crossing, which removes the mechanism
behind the "miss" entirely for circulating water.

Every guard degrades to the reported reading, i.e. to the previous behaviour, so the
feature cannot do worse than its own absence: not circulating; anchor recorded before
circulation started; heater state changed after the anchor (direction may have reversed);
direction unknown, from a restart or a jump of more than one band; no learned rate. Band
saturation is logged at debug, since it means the rate is optimistic in that regime.

**The ETA was left alone, because it never needed this.** `anchor_time +
minutes(anchor_temp → target)` is algebraically identical to `now +
minutes(extrapolated_temp(now) → target)` when the extrapolation uses the predictor's own
local rate — verified numerically as exactly zero difference until the clamp saturates,
past which the existing staleness correction takes over. The anchor *is* the
extrapolation. Only the scheduler lacked one.

**Still not addressed, and not addressable here:** with the pump off there is no tub
measurement to extrapolate, and housings differ between spa models so no offset can be
calibrated. The fallback is the old lumpy behaviour, the `circulating` and
`temperature_basis` attributes report the condition, and the README recommends either
leaving the pump running or driving it from the `start_at` attribute — which is safe in
the one direction that matters, since a stagnant reading over-states the work and so
makes `start_at` the earliest the schedule could fire. The durable fix is the [Optional
External Temperature Probe](#optional-external-temperature-probe).

**Still owed:** whether the cooling law extrapolates — nothing observes a gap beyond
30 °C, and the two fits diverge ~60% by gap 48. The current tub has no data before April
2026, so this needs a winter. See also [Learned Weather
Factor](#learned-weather-factor), which wants the same measurements.

## Measured evidence (session of 2026-08-06/07)

A 17.5 °C cold start, 22.0 → 39.5 °C, the first long predictive run without test
cycling in the week. It finished ~55 min late against a 13:00 target. Breaking the
error down settles what the current design can and cannot do.

**The segmented rates were nearly perfect; `prediction_bias` caused the miss.**

| | predicted | actual | error |
|---|---|---|---|
| segmented buckets, no bias | 11.09 h | 11.21 h | **−1.0%** |
| segmented buckets × bias 0.942 | 10.45 h | 11.21 h | **−6.8%** |

Realised per bucket: cold 1.138 °C/h against 1.11 learned (+2.5%), mid
0.958 against 1.03 (−7.0%). The mid-bucket shortfall is the part that looks like
weather — losses grow with water temperature, so a cold night should bite hardest
there, exactly the shape `AMBIENT_SENSITIVITY` encodes.

**But the ambient correction could not have corrected it, for two independent
reasons.**

1. **The cold bucket has sensitivity 0.0** and was 7.03 of the 11.21 hours — 63% of
   the session. No ambient correction is possible there by construction. Defensible
   physically (a 22 °C tub loses little to 14 °C air) but it means the majority of a
   cold start is uncorrectable however good the rest of the model is.

2. **The baseline chases the weather, so the factor self-cancels.** `ambient_baseline`
   is an EMA (`alpha` 0.05) updated *inside the rate-learning block* — i.e. from the
   same observations that set the rates. Over a season it converges on current
   conditions, `ambient_now − ambient_baseline → 0`, and the factor returns ~1.0
   whatever the weather. It can only catch a deviation from *recent* weather, such as
   one cold snap; it cannot catch the season, because the seasonal signal is absorbed
   into the baseline instead of corrected for.

   Measured: baseline had drifted to 14.01 °C (from the 15.0 default) and outdoor was
   13.7–14.3 °C, so every bucket got a factor of 1.000–0.999. To explain the mid-bucket
   shortfall at sensitivity 0.02 would need `outdoor − baseline = −3.5 °C`, i.e. a
   baseline near **17.5 °C** — plausibly the conditions the rates were actually learned
   under, several weeks earlier and warmer. The baseline had since followed the weather
   down to meet it.

**Why this validates the plan above.** The concept section already has the fix: divide
the weather factor out *before* updating the bucket EMA, so buckets become "rate at
reference conditions". The measurement shows why that is the necessary shape rather
than a refinement — the current arrangement has the rates and the baseline learning
from the same samples, so they co-move and the correction cancels. The reference
conditions must be **fixed**, not learned alongside the rates.

Two consequences worth carrying into the design:

- **Bucket rates are net rates** (gross heating minus losses), so a learned rate is
  only valid at the ambient temperature where it was learned. That is the root
  justification for normalising rather than for tuning sensitivities.
- **The cold bucket's zero sensitivity should be re-derived, not assumed.** Once
  observations are logged with their conditions (see *Instrumentation first*), the
  cold-bucket gain becomes measurable. It is probably small, but 63% of a cold start
  is too much of the session to leave at exactly zero on assumption.

Caveat: this is one session. The bias finding is strong (a 6.8% error against a 1.0%
model), but the 17.5 °C implied baseline rests on assuming the whole mid-bucket
shortfall is ambient, which is unproven — it could be partly bucket-shape error, since
losses may grow faster with temperature than three flat buckets can represent.

---

## Opt-in Analytics & Capability Detection

### Motivation

The MSpa integration includes a self-learning algorithm that predicts how long the spa will take to heat or cool to a target temperature. The algorithm adapts over time using observed heating and cooling rates from each individual installation. However, there are limits to what a single installation can teach it:

- Heating rate varies significantly by ambient temperature and season. An installation in Norway will behave very differently from one in southern Spain.
- MSpa produces many models with different heater power ratings, insulation, and physical dimensions. We do not have access to all models for testing.
- The algorithm's bias correction and bucket-based rate model need validation across a wide range of real-world conditions to confirm they are working as intended.
- Some MSpa models report a device-provided heating rate via the API; others report zero. We do not have a complete picture of which models support which features.

Aggregating anonymous performance data across consenting installations would let us validate and improve the algorithm far faster than a single developer could alone. The capability tester addresses a related problem: knowing which features (jets, ozone, UVC, etc.) are physically present on each model, so the integration can eventually auto-configure itself rather than showing controls that do nothing on unsupported hardware.

---

### Backend: PostHog Cloud

PostHog Cloud is a managed product analytics service with a generous free tier (1 million events per month, 1-year data retention). It supports structured event ingestion via a simple REST API, built-in dashboards, and SQL queries over the collected data. With ~170 active installs and an expected rate of a few sessions per installation per month, the free tier comfortably covers this project for the foreseeable future.

The PostHog project API key will be bundled in the integration source code. This is intentional and safe — PostHog project API keys are write-only by design, intended for exactly this kind of client-side use. They cannot be used to read or delete data.

---

### Part 1 — Prediction Accuracy Telemetry

**Trigger**: end of a heating or cooling session (when actual completion is detected by the coordinator).

**Event**: `prediction_session_completed`

**Properties**:
- `predicted_minutes`, `actual_minutes`, `prediction_error_pct` — durations only, never timestamps
- `start_temp`, `target_temp`, `direction` (`heating` / `cooling`)
- `product_model`, `integration_version`, `device_seed_used` (bool — was the API rate used as cold-start fallback?)
- `bucket_rates_cold_deg_per_hour`, `bucket_rates_mid_deg_per_hour`, `bucket_rates_hot_deg_per_hour`
- `session_scalar`, `bias_correction`
- `ambient_temp_c`, `ambient_condition` — from the configured weather entity, if present (e.g. `sunny`, `rainy`). Helps explain rate variation across installs.
- `install_id` — a random UUID generated once at setup and stored in the config entry. Persistent across sessions so that multiple submissions from the same installation can be aggregated. Never derived from or linked to the device serial number, account credentials, IP address, or any other identifying information.

**Implementation notes**:
- Opt-in toggle in the options flow: `"Share anonymous prediction statistics"` (default `False`)
- Fire-and-forget `hass.async_create_task(_post_stats(...))` in the coordinator when a session ends
- Short timeout (5 s), silent failure on any error — never blocks normal operation
- PostHog ingest endpoint: `https://app.posthog.com/capture/` with project API key stored in `const.py`
- After each successful submission, store the full payload JSON in `coordinator.last_analytics_prediction` and write it to a **diagnostic sensor** (`Analytics: Last prediction report`) so users can inspect exactly what was sent from the device panel
- Guard against duplicate events if session-end detection fires more than once for the same session

---

### Part 2 — Capability Tester

**Use case**: MSpa produces many models with different supported features (jets, ozone, UVC, etc.). The integration currently cannot determine which switches are real for a given device without attempting to use them. Knowing the true capability set per model would allow the integration to hide controls that do nothing on unsupported hardware, and to build a community-sourced model→feature map over time.

**Flow**:
1. User removes the spa lid — bubbles or jets may operate briefly during the test and should not run with the lid on.
2. User triggers the test via a button entity or service call: `mspa.run_capability_test`.
3. Integration reads the current device shadow as a baseline.
4. Each candidate switch is toggled sequentially (filter, bubbles, jets, ozone, UVC) with a short delay between each command. **The heater is deliberately excluded** — it is always present on all models, and toggling it could begin warming the water unnecessarily.
5. The shadow is read after each toggle — if the relevant field changed, the capability is confirmed present on this device.
6. All switches are restored to their original state.
7. Results are written to persistent storage (same mechanism as learned heating rates) so they survive HA restarts without needing to re-run the test.
8. Results are also written to a `Capabilities` diagnostic sensor visible on the device panel.
9. If analytics is opted in, the full payload is submitted to PostHog and stored in a **diagnostic sensor** (`Analytics: Last capability report`) so users can inspect exactly what was submitted.

**Telemetry event** (if analytics opted in): `capability_test_completed`
- `product_model`, `integration_version`, `confirmed_capabilities[]`, `rejected_capabilities[]`, `install_id`

**Long-term value**: builds a model→capability map across the install base. Could eventually auto-configure which controls are shown per model, removing ghost controls for unsupported features.

---

### Draft opt-in disclosure text

The following would appear in the options flow when the toggle is enabled:

---

> **Help improve the MSpa integration**
>
> When this option is enabled, the integration may send two types of anonymous statistics to the MSpa integration developers:
>
> **1. Prediction accuracy** — sent automatically after each heating or cooling session completes. Helps us understand how well the predictive model performs across different spa models, climates, and conditions.
> - How long heating or cooling took (as a duration in minutes), and how accurately it was predicted
> - Start and target temperatures, and heating/cooling direction
> - The learned heating rates and correction factors used for the prediction
> - Ambient outdoor temperature and weather condition at the time of the session (if a weather entity is configured)
> - Your spa model identifier and the integration version number
>
> **2. Device capabilities** — sent once if you choose to run the optional Capability Test. Helps us build a model-to-feature map so the integration can be better tailored to each MSpa model.
> - Which switches responded during the test (e.g. bubbles, jets, ozone, UVC)
> - Your spa model identifier and the integration version number
>
> Both types of submission include a random anonymous installation ID — generated locally and stored only on your device. It is used solely to aggregate multiple sessions from the same installation. It is never derived from or linked to your spa's serial number, account credentials, or any other identifying information.
>
> **We do not collect:**
> - Your name, email address, password, or any account information
> - Your spa's serial number, MAC address, or device identifier
> - Your location or IP address
> - When you use the spa (no timestamps are ever sent)
> - Any Home Assistant configuration outside of this integration
>
> Data is sent to [PostHog](https://posthog.com) and used solely for improving this integration. You can disable this at any time from the integration options.
>
> **Note on opting out**: Disabling this option immediately stops all future submissions. Previously submitted data cannot be retroactively deleted — because no timestamps, identifiers, or personal information are ever included, there is no way to locate or link past submissions to any individual installation.
>
> **Transparency**: The full content of the most recently sent report (both prediction and capability) is always visible on the device panel under diagnostic sensors, so you can review exactly what was submitted at any time.

---

### Prerequisites and open questions before building

- PostHog project created, API key available, data retention policy confirmed (free tier: 1 year)
- `install_id` UUID generation added to the config flow (stored in config entry data)
- Decide whether the capability test is a one-time setup wizard step or a repeatable diagnostic action (re-running is useful if the user adds accessories or wants to verify after a firmware update)
- Confirm PostHog free tier event budget is sufficient at scale; upgrade path documented if the install base grows significantly
- Review HA integration quality requirements and HACS guidelines to confirm opt-in telemetry is permitted and what disclosure standards apply
