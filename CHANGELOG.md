# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

