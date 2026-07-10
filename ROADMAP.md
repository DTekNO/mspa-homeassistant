# Roadmap

Ideas and planned features that are not yet scheduled for a specific release.

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
