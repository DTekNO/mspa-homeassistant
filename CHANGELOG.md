# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The Ready at time no longer wobbles.** During a long heating session the
  estimate changed 165 times in 11 hours — 39 times in the worst hour — including
  14 direction reversals and two jumps of over half an hour. The smoothing added
  in 2026.8.1 was doing what it was written to do, but it was aimed at the wrong
  thing: its speed limit was loose enough that ordinary minute-to-minute jitter
  passed straight through, and it deliberately let *large* corrections bypass
  smoothing on the assumption they meant you had changed the schedule. In practice
  the large corrections were the model revising its own estimate as it learned, so
  the biggest jumps were exactly the ones that escaped smoothing.

  The display now ignores movement under 5 minutes, closes larger gaps gradually,
  and only jumps when you actually change something — the schedule time, the
  schedule temperature or the thermostat. It is also rounded to 5 minutes, since
  minute precision on an estimate hours away was never real. Replayed against the
  recorded session: **165 changes become 32**, the worst hour drops from 39 to 5,
  and the estimate still tracks reality to within 5 minutes.

## [2026.8.1]

A consolidation release for the **⚠️ Experimental [Predictive Scheduling](README.md#experimental-predictive-scheduling)**
feature from 2026.7.1 — the **Heat Schedule** and **Ready at** sensors are now
substantially more trustworthy. A systematic bias that had been re-teaching the
model optimistic heating rates is gone, three bugs that left `Ready` stuck are
fixed, and the estimate no longer jumps around as it corrects. **If you tried
predictive scheduling in 2026.7.1 and found the estimates drifted or `Ready` got
stuck, this is the release to retry with.** Nothing about the core spa controls
changes.

### Fixed — heating estimates

- **Heating and cooling rates were learned systematically too fast.** Water
  temperature reports in 0.5 °C bands, so the first band crossing after heating
  starts measures where inside the band the water happened to be, not how fast it
  is warming — reading roughly double the truth. It is now used only to fix
  position, with rate learning starting from the second crossing. Cooling was
  affected on every thermostat cycle and gets the same treatment.

### Fixed — the `Ready` state and restarts

- **`Ready` could outlive the heat it advertised.** Lowering the thermostat after
  a soak deliberately keeps `Ready` showing, but nothing ever withdrew it — water
  at 24 °C two days later still reported ready. It now stands down once the water
  has cooled 3 °C below its peak; see
  [Why it still says `Ready`](README.md#why-it-still-says-ready-after-you-turn-the-thermostat-down).

- **`Ready` stayed stuck after raising the thermostat**, showing ready with
  degrees still to heat instead of recalculating.

- **`Ready` stayed stuck after a schedule finished**, persisting through cool-down
  until a new schedule was set or the integration reloaded.

- **A restart during scheduled heating forgot the session had started**, and
  computed a fresh start time for a heater that was already running.

### Added

- **A real timestamp for automations.** `ready_at` now carries a full ISO 8601
  timestamp whenever the state shows a time — including while a schedule is
  pending and the spa is cooling toward a standby setpoint, which previously
  reported nothing at all. `ready_at_kind` says whether that is the schedule you
  set or a live prediction. The Heat Schedule sensor's `start_at` can be used the
  same way, which helps if your spa needs a preparatory step before heating. See
  [Getting a timestamp instead of the display string](README.md#getting-a-timestamp-instead-of-the-display-string).

### Changed

- **The Ready at estimate no longer jumps.** Corrections used to arrive in lumps
  (`12:19 → 12:49`); the display now ramps at up to 3 minutes per minute, while
  genuine replans are still followed immediately.

- **`effective_rate_deg_per_hour` now shows the rate actually in use**, not the
  flat global average, which drives nothing. All
  [sensor attributes](README.md#sensor-attributes) are documented in the README.

- **Diagnostics are self-sufficient** — log lines carry the effective rate, the
  per-band rates and the correction factors, and rate learning decisions are
  logged at INFO.

### Internal

- Prediction-accuracy bookkeeping no longer records nonsense sessions from a
  setpoint change.
- Rate tracking extracted from the update loop and covered by tests, including a
  regression built from the log that exposed the phase-uncertainty defect.
  152 tests in total.

## [2026.7.1] - 2026-07-30

### Summary

This release introduces **predictive scheduling**: tell the spa when you want to use it, and the integration works out when to start heating — learning your spa's actual thermal behaviour as it goes, and adjusting for the weather.

Three new entities (**Ready at**, **Heat Schedule**, **Scheduled for**) replace the two old time-to-target sensors. The prediction model gains an **ambient-aware heating-rate correction**: with an optional weather entity configured, the integration knows that a cold, windy night slows the final approach to set-point far more than it slows warming cold water, and shifts the heating start time earlier accordingly. Both scheduling sensors now derive "Ready" from a single shared code path, so they can no longer disagree with each other.

> **Versioning change**: this project now uses calendar versioning (`YYYY.M.PATCH`), replacing the previous `3.x.y` scheme. `2026.7.1` supersedes `3.0.5`.

### Added

#### Predictive scheduling entities *(Experimental)*

- **Ready at sensor** — one human-readable answer to "is the spa ready, and if not, when?" (`10:34`, `10:34 +1d`, or `Ready`). Exposes `minutes_remaining`, `ready_at` (ISO 8601 timestamp), a `color` attribute for Mushroom-style cards, and full diagnostic attributes including direction, per-bucket heat rates, session scalar, and prediction bias.
- **Heat Schedule sensor** — predicts when to *start* conditioning to hit a planned session time (`Start at 04:15`, `Start now`, `Heating`, `Ready`, `Not scheduled`). Uses the learned temperature-bucketed rates and works for both heating and cooling. Requires the **Scheduled for** datetime control to be set.
- **Scheduled for** datetime control — a datetime entity owned by the device (appears in the device panel), so no external `input_datetime` helper is needed. Set it to when you want the spa ready and the schedule recalculates automatically.
- **Automatic schedule trigger** — when the computed start time arrives the integration sets the target temperature and turns the heater on by itself. The schedule is re-evaluated continuously until it fires, so an overnight cool-down that slows the spa moves the start time earlier rather than silently missing the target.
- **Schedule lookahead horizon** — beyond the configured horizon (default 5 days) the Heat Schedule sensor reports `Scheduled +Nd` rather than projecting a start time that far out, since the water temperature and learned rates will both have moved by then. Planning weeks ahead therefore still shows on a dashboard as a confirmed schedule, and `Not scheduled` keeps its distinct meaning: no schedule is set. Raise the horizon in the integration options if you want precise start times sooner.

#### Ambient-aware heating-rate correction *(Experimental)*

- **Weather-driven rate correction** — with an optional weather entity configured, learned bucket rates are scaled by current outdoor temperature relative to a learned seasonal baseline:

  ```
  factor = clamp(1 + sensitivity × (ambient_now − ambient_baseline), 0.3, 1.5)
  ```

  Sensitivity is **per temperature bucket** (cold `0.0`, mid `0.02`, hot `0.06` per °C), reflecting the physics: heat loss scales with the water-to-air temperature difference, so cold water is nearly insensitive to outdoor conditions while the near-setpoint "hot" bucket is affected most. This matches observed cold-night behaviour, where the cold and mid buckets track their learned rates but the hot bucket collapses.
- **Self-learning ambient baseline** — a slow EMA (α = 0.05) of observed session temperatures establishes what "normal" outdoor conditions look like for your location and season, so the correction is relative to your own climate rather than a hard-coded assumption. Seeded at 15 °C until enough samples accumulate.
- **Correction precedence** — a bucket already observed during the current session is used verbatim (real data beats any model); otherwise the empirical session scalar wins if active; otherwise the weather model applies. This makes the weather model matter exactly where it is needed — the pre-start estimate, before any observation for today exists.
- **Graceful degradation** — with no weather entity configured, or when the weather entity is unavailable, the correction factor is `1.0` and estimates fall back to the plain learned rates. Nothing is required to opt out.
- **Gentler rate decay with weather data** — stored bucket rates decay ≈ 0.6%/day with a weather entity configured versus ≈ 2%/day without, since `ambient_rate_factor` explains some of the seasonal variation directly and the stored rates therefore stay useful for longer.
- New diagnostic fields recorded per session: `ambient_temp`, `ambient_wind`, `ambient_baseline`, and `ambient_factor_hot`.

#### Other additions

- **Device-reported heating rate clamped** — the API `device_heat_perhour` seed value is now clamped to 0.5–2.0 °C/h to prevent implausible cold-start estimates. Models that report zero have no cold-start seed and will not produce estimates until the EMA has real data.

### Changed

- **Ready at is context-first** — the sensor now decides *which* target it is talking about before estimating anything, resolving cases where a pending schedule targeted a different temperature than the current thermostat set-point. Three explicit contexts:
  - **Schedule pending** (a future schedule that has not fired) → shows the scheduled ready time, or `Ready` if the water is already at the scheduled temperature.
  - **Scheduled heating** (the trigger has fired) → shows a live ETA to the *scheduled* temperature, ignoring the original plan.
  - **Free** (no schedule) → shows an ETA to the thermostat set-point, or `Ready`.

  This fixes a reported bug where a stale "at target" latch from an earlier low-temperature session made the sensor read `Ready` while the Heat Schedule sensor still showed `Start at 12:50`.
- **Anchor-based ETA** — estimates are computed from a fixed (temperature, timestamp) anchor rather than being recalculated from scratch each poll. The displayed time now stays stable while the water temperature is unchanged and moves only when there is new information, instead of drifting by a minute on every poll.
- **Ready at and Heat Schedule now share one readiness definition** — the Heat Schedule sensor previously applied its own `within 1 °C of target` shortcut, which could declare `Ready` up to an hour before the Ready at sensor agreed. It now delegates to the same function the Ready at sensor uses, so the two entities always converge on `Ready` simultaneously.
- **Heat Schedule holds `Heating` once triggered** — after the trigger fires the sensor reports a stable `Heating` rather than oscillating between `Start now` and `Start at HH:MM` as the temperature fluctuates during thermostat cycling.

### Fixed

- **Bubbles could not be switched on without first setting a level** — the device reports `bubble_level = 0` while bubbles are off, so the turn-on command was sent as `{bubble_state: 1, bubble_level: 0}` and silently ignored by the spa. The level is now clamped to at least 1 on turn-on, so the switch works on its own. Spas that report a non-zero level while off are unaffected — their last-known level is preserved.
- **Setting a bubble level started the bubbles without the switch following** — setting a level activates the blower as a device-side effect, but Home Assistant did not know. The level service now sends the state explicitly and registers `bubble = on` for command confirmation, so the switch no longer snaps back to off on the next poll.
- **Bubble level slider did not update until the next poll** — the new level is written to the local state cache immediately, so the slider reflects the chosen value on the next render instead of waiting for cloud confirmation.
- **`UnboundLocalError` in Ready at attributes after a schedule trigger** — reading the sensor's attributes immediately after the schedule fired could raise instead of returning values.
- **Prediction bias could drift on restart and move away from recent accuracy** — the bias was recomputed from stored session history on every load, weighting past sessions by how closely their weather matched *current* conditions. That made it change on restart with no new data (observed moving 1.072 → 1.060 across a restart), and because the weights were re-derived against instantaneous wind it could rise after a session whose accuracy should have lowered it (observed 1.055 → 1.060 → 1.063 while the two sessions concerned came in at ratios of 1.016 and 0.964).

  The effect was systematic over-estimation: on the sessions analysed the raw rate model was accurate to ±2.6% while the biased estimate was out by 6.9% — the correction was making predictions 2.6× worse and starting scheduled heat-ups roughly 30 minutes earlier than necessary.

  The bias is now an incremental EMA (α = 0.3) folded in **once per completed session** and persisted directly, so it changes only when there is new evidence and a session below the current bias always pulls it down. Weather no longer participates — adjusting for conditions is the rate model's job via `ambient_rate_factor`. The clamp is tightened from [0.5, 2.0] to [0.9, 1.1], since the segmented model does the real work and the bias should only ever be a small residual. On upgrade, stored history is replayed through the new EMA so accumulated learning is preserved rather than discarded.
- **Re-asserting the same scheduled time re-armed the scheduler** — `datetime.set_value` on **Scheduled for** committed unconditionally, and committing deliberately clears the trigger and readiness latches so that a genuine reschedule takes effect. Any automation run that recomputed the same time therefore re-armed the scheduler: mid-heat-up the trigger would re-fire, the target temperature would be resent, and the Heat Schedule sensor would drop out of `Heating` back to a start-time state.

  This is easy to hit with the calendar-sync automation in the README, because a `state` trigger on a calendar entity fires on attribute-only changes — so an edit to `end_time`, `message` or `location` re-runs the automation while `start_time`, and hence the computed ready time, is unchanged.

  Setting a time that matches the one already set is now a no-op. A genuine change still re-arms the scheduler exactly as before.

### Removed
- **⚠️ BREAKING: "Ready At" timestamp sensor removed** *(Experimental)* — the `sensor.mspa_xxx_ready_at` timestamp entity (device class: timestamp) has been removed. It is replaced by the new **Ready at** sensor.

  **Migration**:
  - Dashboards showing the timestamp → use the sensor state directly (`10:34`, `10:34 +1d`, `Ready`)
  - Conditional cards using `state_not: unavailable` → change to `state_not: Ready` (the sensor stays available and shows `Ready` when at target)
  - Automations triggering `to: unavailable` (spa reached target) → change to `to: "Ready"`
  - Automations/templates reading the timestamp value → use the `ready_at` attribute on the new Ready at sensor

- **⚠️ BREAKING: "Time to Target Temperature" sensor removed** *(Experimental)* — the `sensor.mspa_xxx_time_to_target_temperature` entity has been removed. All its attributes have been merged into the new **Ready at** sensor.

  **Migration**:
  - Dashboards or cards showing minutes → use `state_attr('sensor.mspa_xxx_ready_at', 'minutes_remaining')`
  - Conditional cards using `state_not: unavailable` → change to `state_not: Ready`
  - Automations comparing minutes numerically → use `state_attr(..., 'minutes_remaining') | int`
  - Attribute references (`direction`, `prediction_bias`, bucket rates) → same attribute names, now on the Ready at sensor

### Internal

- **Scheduling test suite rebuilt** — the three large per-sensor test files are replaced by scenario-based suites covering the named use patterns (cold start, stale latch with a higher pending schedule, thermostat lowered while at temperature, warm day at scheduled temperature), the readiness latch state machine, trigger firing and guard conditions, the ambient-rate correction, prediction-bias monotonicity and clamping, the schedule re-assert guard, the lookahead horizon, and spa control coupling (filter→heater, bubble level→on). 107 tests, ~0.2 s.
- **Weather affects the rate model, not the bias.** A development iteration weighted the bias by weather similarity to current conditions; that proved unstable and was dropped in favour of `ambient_rate_factor`, which is where conditions belong. A properly learned weather model is planned — see [ROADMAP](ROADMAP.md#learned-weather-factor).
- `homeassistant.core.callback` is now stubbed as an identity decorator in the test harness. As a bare mock it replaced every decorated method with a mock, silently turning them into no-ops and making them untestable.

---

## [3.0.5] - 2026-06-16

### Fixed
- **Prediction learning history lost on restart** — heating rates, bias correction, and session history are now reloaded from persistent storage on startup, preserving accumulated learning across restarts (experimental learning and prediction feature)

---

## [3.0.4] - 2026-05-28

### Fixed
- **Prediction bias calculation** — runs with insufficient temperature delta are now filtered out, preventing inaccurate bias corrections

---

## [3.0.3] - 2026-05-06

### Fixed
- **Prediction bias outliers** — outlier ratios are now rejected in the bias calculation to improve estimate accuracy

---

## [3.0.2] - 2026-05-05

### Fixed
- **Deadlock when turning filter off** — the filter-off command could hang indefinitely
- **Rapid polling storm during preheat** — excessive API traffic during the preheat cycle
- **Command confirmation timeout loop** — unconfirmed commands could trigger infinite rapid-poll cycles
- **30-second command latency** — duplicate confirmation mechanisms caused unnecessary delays
- **Silent command failures** — commands acknowledged by the cloud but ignored by the spa are now retried once
- **Rapid command sequences** — sending two commands quickly no longer causes stale retries or conflicting state

### Added
- **3-tier adaptive polling** — polling interval adjusts automatically based on spa activity:
  - **Idle (120s):** nothing running, state stable for 10+ minutes
  - **Active (30s):** heater, filter, bubble or jet is on
  - **Rapid (1s for 15s):** after sending a command from HA
  - **External change (5s for 15s):** when someone uses the physical panel or MSpa Link app
- **Historical prediction bias correction** — past prediction accuracy is used to correct future time-to-target estimates, fading naturally as the rate model improves
- **Device Detail diagnostic sensor** — extended device info fetched once on startup (product images, device UUID, warning code, etc.)
- **Temperature-bucketed heating rates** — heating rate tracked in three temperature ranges for more accurate long-run estimates
- **Session condition scalar** — ambient conditions (hot/cold day) are reflected in the estimate within the first few minutes of heating
- **Prediction accuracy tracker** — logs estimated vs actual heating times for each session (last 10 persisted)
- **Time-decay on stored rates** — stale seasonal data gradually loses influence over time
- New sensor attributes: `heat_rate_cold/mid/hot_deg_per_hour`, `session_condition_scalar`, `prediction_bias`

---

## [3.0.1] - 2026-04-23

### Fixed
- Time to Target sensor now genuinely counts down instead of showing the same value every poll
- "Time to Target" and "Ready At" sensors no longer go out of sync with each other

### Added
- Learned heating/cooling rates are saved and restored across restarts — no more warm-up period after a reload
- Jet switch now shows the `mdi:turbine` icon

### Migration
- The old `sensor.*_heating_time_remaining` entity will appear as unavailable after upgrading. Delete it from **Settings → Devices & Services → Entities** once the new `sensor.*_time_to_target_temperature` is confirmed working.

---

## [3.0.0] - 2026-04-13

### Summary
This release adds multi-device support — you can now add multiple MSpa hot tubs from the same account as separate integrations in Home Assistant. Existing single-device setups are automatically migrated. It also brings dynamic diagnostic sensors, firmware version reporting, multi-device reliability improvements, and a developer demo mode.

### Added
- **Multi-Device Support** - Add multiple MSpa hot tubs from the same account
  - Two-step config flow: enter credentials, then select which device to add
  - Each device gets its own config entry, coordinator, and set of entities
  - Already-configured devices are filtered from the device picker
  - Clear abort message when all devices on the account are already configured
  - Config flow skips the credential step when adding a second spa, reusing credentials from the first entry automatically

- **Translations** - Added `translations/en.json` for reliable UI string display in custom integrations

- **Firmware Version Sensor** - New sensor combining `wifi_version` and `mcu_version` from the device list API into a single `"141-3A1"` style value, matching the format shown in the MSpa app

- **Time to Target Temperature Sensors** *(experimental)* — available on **all** models
  - **Time to Target Temperature** — minutes until the set-point is reached (heating *or* cooling direction)
  - **Ready At** — absolute timestamp of when the spa should be ready
  - Both sensors become **unavailable** once the target temperature is reached, making them easy to use in conditional cards and automations
  - Rate is **self-learned** via an exponential moving average (EMA) of observed 0.5 °C temperature steps — no reliance on device-reported values
  - **Heating rate** sampled during full-heat mode; **cooling rate** sampled passively when heater is off and temperature is dropping
  - Outlier rejection (e.g. adding hot/cold water mid-session) prevents spikes from corrupting the EMA
  - Device-reported `device_heat_perhour` (Oslo series etc.) used as a heating fallback until the EMA has enough data
  - Marked experimental: algorithm is new and needs a few weeks of real-world validation across seasonal conditions — feedback welcome

- **Dynamic Diagnostic Sensors** - Diagnostic sensors are now created automatically from every key in the thing-shadow payload that is not otherwise handled by a structured sensor. New firmware keys appear as new sensors without any code changes; removed keys disappear.

- **Shared Rate Limiter (`_MSpaThrottle`)** - Per-account spike-arrest rate limiter (0.4 s minimum between requests) shared across all coordinators for the same account, preventing API rate-limit errors (code 11000) when two spas start up simultaneously

- **Shared Auth Store** - All coordinators for the same MSpa account share one token and one `asyncio.Lock`, eliminating token-collision races on startup

- **Entry Title Self-Correction** - On each startup the integration checks whether the config entry title matches the device alias returned by the cloud and corrects it if not. Prevents stale titles persisting across restarts without a delete-and-re-add.

- **Block Device-Only Deletion** (`async_remove_config_entry_device`) - Prevents users from deleting the spa device from the device-detail page without removing the integration entry, avoiding ghost entities. HA redirects the user to delete the integration entry instead.

- **Improved Device Info** — device page now shows firmware version (`141-3A1` format), serial number, MAC address, and model ID sourced from the device list API

- **Developer Demo Mode** - Use email `demo@mspa.test` (any password) to add up to three virtual spa devices (Frame / Oslo / Alpine) with no cloud connectivity. Status polls return realistic drifting mock data; commands update mock state in memory. See the README for full details.

### Changed
- **Config Flow** - Redesigned as a two-step flow
  - Step 1: Enter email, password, and region (with auto-detection)
  - Step 2: Select device from your account (auto-selected if only one)
  - Duplicate device prevention via unique_id per physical device

- **Device Identity** - Devices now use the real MSpa device ID as their identifier
  - Enables proper multi-device support in the device registry
  - Existing devices are automatically migrated from the old generic identifier

- **Entity Unique IDs** - Diagnostic sensor unique IDs now include the device ID suffix
  - Prevents entity collisions when multiple devices are configured
  - Existing entities are automatically migrated to the new format

- **Diagnostic Sensor Keys are Verbatim** - The coordinator no longer normalises shadow payload key names (e.g. `wifivertion`, `mcuversion`). Old key names appear as-is so that firmware renames become visible in the UI rather than being silently hidden. The firmware version sensor uses the authoritative values from the device list API.

- **Auth Cleanup on Full Unload** - `async_unload_entry` now correctly removes the shared auth store when the last entry is unloaded.

- **Code Quality** - Extracted helper functions and removed dead code
  - `_build_headers()` and `_obfuscate_email()` helpers in API client
  - `_get_option_int()` and `_calculate_total_power()` helpers in sensor module
  - Removed unused `RAPID_POLL_MAX_ATTEMPTS`, `_update_lock`, and trivial `async_request_refresh` override
  - Fixed `authenticate()` silently returning stale token on failure — now raises `RuntimeError`
  - Config flow distinguishes `invalid_auth` from `cannot_connect` errors

### Fixed
- **Filter Status Unique ID** - Fixed missing underscore in `filter_status` entity unique ID (`mspa_filter_status{id}` → `mspa_filter_status_{id}`)
- **Command Serialisation** - Write commands across two coordinators for the same account are serialised via a shared `api_lock`, preventing interleaved command payloads
- **`MSpaDiagnosticSensor.state`** - Fixed property reading from the internal `_last_data` dict instead of the public coordinator property which can be `None` on first load

### Migration
- **Automatic**: Device identifiers, entity unique IDs, and the filter_status fix are all migrated automatically on first startup after upgrade. No manual action required.
- **If you experience issues**: If entities appear duplicated or missing after upgrading, remove the integration and re-add it. Your device will be rediscovered automatically.

---

## [2.1.0] - 2026-02-16

### Summary
This release significantly improves power cycle detection and state restoration, addressing the MSpa hardware's tendency to reset to Fahrenheit and default settings after power cycles.

For a complete list of all bug fixes, improvements, and detailed feature descriptions, please refer to the [full CHANGELOG on GitHub](https://github.com/DTekNO/mspa-homeassistant/blob/main/CHANGELOG.md).

### Added
- **Enhanced Power Cycle Detection** - Multiple detection methods for reliable power cycle detection
  - is_online transition detection (original method)
  - Multi-parameter change detection (catches quick power cycles)
  - Temperature unit reset detection (monitors F/C unit changes)
  - Improved logging with emoji indicators and detection method information

- **Always Enforce Temperature Unit** - New configuration option
  - Continuously enforces temperature unit on every update
  - For devices that frequently forget unit without full power cycles
  - Independent from power-cycle-only tracking option

- **State Restoration After Power Outage** - Automatic restoration of device settings
  - Saves state before power loss (temperature, heater, filter, ozone, UVC)
  - Optionally restores saved states when power returns
  - Includes delays between commands for reliable execution
  - Detailed logging of each restoration step

### Changed
- **Temperature Unit Management** - Improved temperature unit handling
  - Automatic unit tracking now optional (power-up only)
  - New "always enforce" option for continuous monitoring
  - Works independently - no manual unit selector needed
  - Both options can be used together or separately

- **Logging & Diagnostics** - Enhanced logging for troubleshooting
  - Clear power ON/OFF detection messages with emoji indicators
  - State saving confirmations with values
  - Individual restoration step status
  - Warnings for potential false positives

### Configuration
Three optional settings available in integration configuration:
1. **Track temperature unit**: Set device unit to match HA system unit on power-up
2. **Always enforce unit**: Continuously enforce temperature unit on every update
3. **Restore previous states after power outage**: Restore device states when MSpa powers back on

**Note**: All features are disabled by default to maintain backward compatibility. Visit Settings → Devices & Services → MSpa → Configure to enable new features after upgrading

---

## [2.0.0] - 2026-01

### Summary
This major release introduces multi-region support and comprehensive energy monitoring capabilities for your MSpa hot tub.

### Added
- **Multi-Region Support (Experimental)** - Support for ROW, US, and CH regions
  - Auto-detection based on Home Assistant country setting
  - Manual region override during setup
  - Regional endpoints: ROW (Europe), US (United States/Canada), CH (China/Hong Kong/Macau)
  - Fallback to ROW region for maximum compatibility
  - Region endpoints identified from [openHAB MSpa binding](https://github.com/weymann/openhab-addons/tree/main/bundles/org.openhab.binding.mspa)

- **Power and Energy Monitoring** - Comprehensive power tracking
  - Individual power sensors for pump, bubble blower, and heater
  - Total power sensor with component breakdown in attributes
  - Energy dashboard integration with built-in total energy sensor (kWh)
  - Configurable power values for each component
  - Persistent energy tracking across Home Assistant restarts
  - Trapezoidal integration for accurate energy measurements
  - Default values based on MSpa Comfort C-BE061 specifications

- **Adaptive Polling** - Smart polling frequency
  - Automatically increases to 1 second when changes pending or during preheat
  - Timeout protection returns to 60-second polling after 15 seconds
  - Improved responsiveness for state updates
  - Reduced API load during idle periods

### Changed
- **Offline Detection** - Entities now correctly show as unavailable when hot tub is offline
- **HVAC Action States** - Added `preheating` state to climate entity for better visibility
- **Input Handling** - Strips leading/trailing whitespace from username/password (copy/paste friendly)

### Documentation
- Comprehensive power/energy monitoring documentation with calibration guide
- Multi-region setup instructions with visual guides
- Updated screenshots with meaningful filenames

**Note**: Multi-region support is experimental. ROW region is well-tested; US and CH have had limited testing

---

## [1.0.11] - 2025

### Changed
- **Input Handling** - Improved username and password handling for whitespace from copy/paste
- **Logging** - Enhanced diagnostic logging for authentication and token management

### Fixed
- Minor bug fixes and stability improvements

---

## [1.0.10] - 2025

### Changed
- **Error Handling** - Improved error handling and logging for API failures and connection issues
- **Documentation** - Updated to reflect new features and configuration options

### Fixed
- Minor bug fixes and performance improvements

---

## [1.0.9] - 2025

### Added
- **Diagnostic Sensors** - Filter status, heater timer, and fault sensors (disabled by default)
- **HVAC Actions** - Included `hvac_actions` in climate entity

### Changed
- **Device Info** - Improved device info and entity naming for better Home Assistant integration

### Fixed
- Code cleanup and minor bug fixes

**Note**: If upgrading, review new diagnostic entities in the entity registry and enable if needed

