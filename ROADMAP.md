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

## Cancelling a Heat Schedule

### Problem

There is currently no way to cancel a pending schedule. `scheduled_ready_at` is cleared automatically, but only once its time has *passed* — by which point the spa has already heated. A user who cancels a planned session is left with a schedule that will still fire.

This bites hardest with the documented calendar-sync automation. Deleting a calendar event normally rolls the sync on to the next event, but deleting the *only remaining* event leaves the calendar with no start time, so the automation's condition is false and **Scheduled for** silently retains its old value. The automation cannot fix this itself:

- `datetime.set_value` requires a datetime — there is no way to unset a datetime entity.
- Writing a past time is actively worse: `_check_schedule_trigger` fires when `now >= start_at`, so a past target triggers an immediate heat-up rather than cancelling one.

The only workaround today is to park **Scheduled for** far beyond the lookahead horizon, which is obscure and leaves a misleading value in the UI.

### Options

- **A `mspa.clear_schedule` service.** Explicit and scriptable, so the calendar automation can add an `else` branch that clears the schedule when no upcoming event exists. Probably the minimum viable fix.
- **A "Schedule enabled" switch on the device.** Discoverable in the UI and easy to automate, and it preserves the time so the user can re-enable without re-entering it. Costs an extra entity.
- **Treat a past value as a cancel** rather than an immediate trigger. This would make the natural automation-side gesture work, but it changes existing trigger semantics and risks breaking the deliberate "fire as soon as the window opens" behaviour that `test_fires_when_target_time_passed` covers. Would need care.

### Notes

Whichever route is chosen, the calendar-sync example in the README should gain an `else` branch that cancels when the calendar has no upcoming event, and the datetime entity's restore path should not resurrect a schedule the user had cancelled.

---

## Learned Weather Factor

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

- Is `sqrt(wind)` the right form, or is a cap at moderate wind speeds sufficient? Needs data.
- Should the gain be learned per bucket after enough evidence accumulates, or is the fixed `SHAPE` good enough indefinitely?
- Cover state is the largest unmodelled variable and the integration has no way to know it. Worth an optional `binary_sensor` config option so users who have instrumented their cover can feed it in?
- How should the gain behave when a user relocates the spa or adds insulation — is a manual reset needed, or does a slow forgetting factor on the fit handle it?

---

### Measured evidence (session of 2026-08-06/07)

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
