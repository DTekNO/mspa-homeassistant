# Roadmap

Ideas and planned features that are not yet scheduled for a specific release.

---

## Pre-roll Hook for the Scheduled Start

### Problem

The scheduler's startup is deliberately minimal: at `start_at` it sets the target
temperature and turns the heater on. Nothing else. That is right for most spas,
but some hardware needs a preparatory step first.

Reported by a user whose flow sensor throws F1 unless water is already moving —
they run the jets briefly before the heater to get flow going. They can do that
today with a template trigger on the Heat Schedule sensor's `start_at` attribute,
but it is inference rather than notification, and it has a real gap: `start_at`
is recomputed every poll, so if it moves earlier by more than the automation's
lead time — a faster-than-expected cool-down, or a replan after a setpoint change
— the pre-hook can be skipped entirely and the heater starts with no preparation.
The one-sided form below latches instead of missing, but it can still fire *late*:

```jinja
{% set s = state_attr('sensor.mspa_heat_schedule', 'start_at') %}
{{ s is not none and now() >= (s | as_datetime) - timedelta(minutes=5) }}
```

There is also no ordering guarantee. The integration cannot promise the event
reaches the automation before it commands the heater, because it never emits one.

### Design

A configurable **pre-roll**, default unset. When unset nothing changes and no
event fires — existing installations are unaffected.

When set, `_check_schedule_trigger` fires an event at `start_at − pre_roll`,
waits out the pre-roll via `async_call_later`, then performs the existing
setpoint and heater commands. Automations subscribe with `trigger: event`.

**The plan must freeze for the duration.** This is the part that turns the event
from a hint into a guarantee: once the integration has announced "starting in N
minutes" it must not re-plan, or the heater could fire before the pre-roll
elapses (start moves earlier) or long after the automation's preparation has
finished (start moves later). Continuous re-evaluation is the predictor's main
virtue, so giving it up is a real cost — which is why the pre-roll wants a hard
cap. **10 minutes** is a reasonable bound: at typical rates around 1 °C/h the
water moves ~0.17 °C over that window, so the abandoned re-planning is worth far
less than the ordering guarantee.

Event payload should carry the frozen `start_at`, `target_time` and
`target_temperature`, so an automation can time itself against the real
actuation moment rather than hardcoding a delay that duplicates the config.

### Must-haves

- **Persist the commitment.** A restart inside the pre-roll window must not lose
  it, or the event re-fires and the heater may start early. This is the same
  class of bug as the `_schedule_triggered` persistence fix — freeze state and
  frozen start time belong with the other stored scheduler state.
- **Cancel on a new schedule.** If the user moves or clears the schedule during
  the pre-roll, the commitment must be abandoned rather than honouring a plan
  that has been replaced — mirroring how a new schedule resets
  `_schedule_triggered`.
- **Fire at most once per schedule**, guarded like `_schedule_triggered`.
- **Fire even when no heating is needed.** With the spa already at target,
  `minutes_needed == 0` and `start_at == target_time`; the preparation step is
  still wanted, so the event should fire `pre_roll` before the target time.

### Notes

Granularity is bounded by `DEFAULT_SCAN_INTERVAL` (60 s), so the pre-roll is
minutes, not seconds — a few seconds of lead would sit inside the scheduler's own
jitter and could land after the heater command. That suits the motivating case
anyway, where a couple of minutes of jet flow is what actually clears the sensor.

Not built yet, deliberately. One user with a sticky flow sensor is enough to know
the *shape* is right but not enough to fix the payload. If a second request
arrives, that will say whether the hook should stay generic (an event, integration
knows nothing about jets) or become a concrete "run jets for N seconds before
heating" option.

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
