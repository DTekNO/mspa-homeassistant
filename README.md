# MSpa Custom Component Integration and Installation via HACS

[![hacs][hacs-badge]][hacs-url]
[![Validate with HACS][hacs-validation-badge]][hacs-validation-url]
![Maintenance][maintenance-badge]
[![release][release-badge]][release-url]
![GitHub Downloads (all assets, all releases)][downloads-badge]
![GitHub Downloads (all assets, latest release)][downloads-latest]



This repository contains a custom Home Assistant component. You can easily install it using [HACS](https://hacs.xyz/).

## Overview

This custom Home Assistant integration implements a device to control an MSPA hot tub.  
It allows users to monitor and control various functions of their MSPA hot tub directly from Home Assistant, enabling automation and remote management.

Key features:
- Turn the hot tub on or off
- Adjust temperature settings
- Control bubbles and filtration
- Monitor current status and temperature

Refer to the installation and configuration instructions below to get started.

## Installation

1. **Install via HACS:**
    - In Home Assistant, go to **HACS** > **Integrations**.
    - Search for **MSpa** or **MSpa Hot Tub**.
    - Click on the integration to open it, then click **Download**.

2. **Restart Home Assistant:**
    - Go to **Settings** > **System** > **Restart** to apply the changes.
    - Alternatively, Home Assistant will provide a "repair" in settings that you may click on to restart Home Assistant. 

3. **Configure the Integration:**
    - Follow the documentation or configuration instructions specific to this component below.



## Configuration

After installation, you will need to configure the integration in Home Assistant. Before carrying out these steps it is recommended to 
create a guest account on the MSPA Link app to avoid using your main account credentials. Refer to the article here 
to create a guest account: [Creating a Guest Account in the MSPA Link App](MSPA_LINK.md).

To configure the MSPA integration in Home Assistant:

1. Go to **Settings** > **Devices & Services**.
2. Click on **Add Integration**.
3. Search for **mspa** and select it.
4. Enter the required information:
    - `email`: Your guest email for the MSPA account.
    - `password`: The MSPA account password for the guest user.
    - `region`: The integration will auto-detect your region based on your Home Assistant country setting. You can override this if needed:
      - **ROW (Rest of World/Europe)**: For European and other global regions (api.iot.the-mspa.com)
      - **US**: For United States and Canada (api.usiot.the-mspa.com)
      - **CH**: For China, Hong Kong, and Macau (api.mspa.mxchip.com.cn)

    ![Configuration dialog showing email, password, and region selection](img/config-dialog.png)
    
    > **Note on Multi-Region Support**: Multi-region support is **new and experimental**. While the ROW (Europe) region is well-tested, the US and CH regions have had limited testing. The region endpoints were identified from the [openHAB MSpa binding](https://github.com/weymann/openhab-addons/tree/main/bundles/org.openhab.binding.mspa). If you use the US or CH regions, please provide feedback on whether the integration works correctly in your region by opening an issue on GitHub.

5. Click **Submit** to complete the configuration.
6. If the registration is successful, you will see your device and some entities for monitoring and controlling it.
7. You can now add the entities to your dashboard or use them in automations.

   - **Example Entities:**
     - `switch.mspa_hot_tub_heater`: To turn the hot tub on or off.
     - `sensor.mspa_hot_tub_water_temperature`: To monitor the current temperature.
     - `sensor.mspa_hot_tub_heater_power`: To monitor the current power consumption.
     - `switch.mspa_hot_tub_bubbles`: To control the bubbles.
     - `switch.mspa_hot_tub_filter`: To control the filtration system.
     - `sensor.mspa_hot_tub_fault`: To monitor the current fault status.

## Integration page

![MSpa integration page showing device and entities](img/integration-page.png)

## Device page

![MSpa device page showing controls and sensors](img/device-page.png)

## Enabling the Filter status Sensor

If your MSpa device supports filter status monitoring, a `Filter status` sensor will be available in Home Assistant after installing or upgrading this integration.  
By default, diagnostic sensors like the Filter state sensor are disabled in the entity registry. It should state in
the manual for your mspa whether your mspa supports filter status monitoring.

To enable it:

1. Go to **Settings** > **Devices & Services** > **Entities** in Home Assistant.
2. Search for `Filter status` under your MSpa device.
3. Click the `Filter status` sensor and enable it.

The Filter status sensor will show `OK` when the filter is clean, and `Dirty` if the filter needs to be changed (when the warning code is `A0`).

## Heating action (hvac_action)

The integration also provides `hvac_action` as part of the climate sensor that indicates the current heating state of the hot tub.
The climate entity will show the following states:
- `off`: The hot tub is turned off.
- `idle`: The hot tub is on but not actively heating. This would normally be the state when the water is at or above the desired temperature.
- `preheating`: The hot tub is warming up before entering full heating mode.
- `heating`: The hot tub is actively heating the water.

## Smart Adaptive Polling

The integration uses a 3-tier adaptive polling system to balance responsiveness against API load. Aa Home Assistant integration runs 24/7 — so polling is automatically reduced when nothing is happening.

| Tier | Interval | Trigger |
|------|----------|---------|
| **Idle** | 120 s | Nothing running (heater/filter/bubble/jet all off) and state stable for 10+ minutes |
| **Active** | 30 s | Any component is running (heater, filter, bubble, or jet) |
| **Rapid** | 1 s for 15 s | After sending a command from HA (to confirm the spa accepted it) |
| **External change** | 5 s for 15 s | When the spa's state changes unexpectedly (someone used the physical panel or MSpa Link app) |

**What this means in practice:**
- When the spa is idle overnight, the integration polls only once every 2 minutes — reducing API traffic by ~50% compared to a fixed 60s interval.
- When the spa is actively heating or filtering, polling is every 30 seconds so temperature updates and state changes are reflected quickly.
- When you toggle something from the HA dashboard, the integration polls every second for 15 seconds to confirm the command took effect (with one automatic retry if the spa doesn't respond).
- If someone presses a button on the spa's control panel, the integration detects the unexpected change on the next active/idle poll and temporarily speeds up to catch any follow-up changes (e.g. someone configuring multiple settings).

> **Note:** There is no way to receive push notifications from the MSpa cloud — all integrations must poll. This adaptive system minimises unnecessary traffic while keeping the UI responsive during active use.

## Time to Target Temperature Sensors *(Experimental)*

> ⚠️ **These sensors are experimental.** The self-learning rate algorithm is new and has not yet been validated across a full seasonal range of conditions. Treat the estimates as a rough guide rather than a precise prediction. Feedback on accuracy is very welcome — please open an issue with your model and observations.

The integration provides two sensors that estimate when the spa will reach its set-point temperature. They work in **both directions** — counting down while heating up *and* while cooling down to a lower target.

| Sensor | Description |
|---|---|
| **Time to Target Temperature** | Minutes until the set-point is reached |
| **Ready At** | Absolute timestamp (clock time) when the spa should be ready |

### How the rate is learned

The integration learns the heating and cooling rates by observing actual temperature changes over time:

- **Heating rate** — sampled while the climate entity is in the `heating` action (full-heat mode). The first valid sample is taken after the water temperature has changed by 0.5 °C (one sensor step) and at least 3 minutes have elapsed.
- **Cooling rate** — sampled passively whenever the heater is off and the temperature is actually dropping. Same minimum step and time requirements.

Rates are fed into an **exponential moving average (EMA)** so that the estimate adapts gradually as conditions change (ambient temperature, water volume, lid on/off). Outliers — for example from adding hot or cold water — are rejected before they can distort the EMA.

Both sensors show as **unavailable** until at least one valid rate sample has been collected. On a typical heating cycle this means the sensors become active after the first 0.5 °C step that takes longer than 3 minutes (usually well within the first 30 minutes of heating).

#### Temperature-bucketed heating rates

A spa heats faster when the water is cold (small thermal losses) and slower near the set-point (large losses). To model this, the integration tracks the heating rate in three temperature buckets:

| Bucket | Range | Typical behaviour |
|---|---|---|
| Cold | < 30 °C | Fastest — minimal heat loss to surroundings |
| Mid | 30–37 °C | Moderate — increasing loss as water warms |
| Hot | ≥ 37 °C | Slowest — highest thermal loss, near set-point |

When estimating time-to-target, the remaining temperature delta is split at the 30 °C and 37 °C boundaries, and each segment is calculated at its own observed rate. This gives substantially more accurate estimates for long heating runs (e.g. 20 °C → 40 °C) compared to using a single flat rate.

#### Ambient condition correction

At the start of each new heating session (heater engages with delta > 2 °C), a **session scalar** is reset to 1.0. As soon as the first temperature bucket receives a live observation, the measured rate is compared to the stored base rate for that bucket to derive a ratio. This ratio is smoothed and applied to all other bucket predictions that have not yet been observed in the current session.

The effect is that a particularly cold or warm day is reflected across the whole estimate within the first few minutes of heating, even before the spa has passed through all three temperature ranges.

#### Time-decay on stored rates

Bucket rates loaded from storage are decayed toward the global flat EMA over time (≈ 2 % per day, floor 40 % weight). This prevents stale seasonal data — for example rates learned in summer — from anchoring winter predictions indefinitely. After about two weeks of inactivity, stored buckets carry roughly 75 % of their original weight.

### Sensor attributes

The **Time to Target Temperature** sensor exposes diagnostic attributes that are useful while the algorithm is still bedding in:

| Attribute | Description |
|---|---|
| `direction` | `heating`, `cooling`, or `at_target` |
| `effective_rate_deg_per_hour` | Rate being used for the current estimate |
| `computed_heat_rate_deg_per_hour` | Learned EMA heating rate (`null` until first sample) |
| `computed_cool_rate_deg_per_hour` | Learned EMA cooling rate (`null` until first sample) |
| `heat_rate_cold_deg_per_hour` | Bucket rate for < 30 °C (`null` until first sample in range) |
| `heat_rate_mid_deg_per_hour` | Bucket rate for 30–37 °C |
| `heat_rate_hot_deg_per_hour` | Bucket rate for ≥ 37 °C |
| `session_condition_scalar` | Current ambient-condition correction factor (1.0 = neutral) |
| `prediction_bias` | Historical bias correction (1.0 = no correction, >1.0 = predictions were too optimistic) |
| `device_rate_deg_per_hour` | Heating rate reported by the device itself (some models only) |
| `current_temperature` | Current water temperature |
| `target_temperature` | Current set-point |

You can inspect these in **Developer Tools → States** to see how the algorithm is performing.

### Availability

Both sensors become **unavailable** once the target temperature is reached (or no rate data is available yet). This makes them easy to use in conditional cards and automations:

**Show a card only while heating or cooling to target:**
```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.mspa_ready_at
    state_not: unavailable
card:
  type: entity
  entity: sensor.mspa_ready_at
```

**Send a notification when the spa is ready:**
```yaml
trigger:
  - platform: state
    entity_id: sensor.mspa_ready_at
    to: unavailable
action:
  - service: notify.mobile_app
    data:
      message: "The spa has reached its target temperature!"
```

> **Note on accuracy**: Estimates improve over time as the EMA accumulates more samples. Accuracy is affected by ambient temperature, water fill level, and whether the lid is on. The rate is re-learned each heating or cooling cycle. The integration automatically tracks prediction accuracy — grep for `PREDICTION_RESULT` in the Home Assistant log to see estimated vs actual times for each heating session.

### Dashboard card example

The example below uses the [Mushroom Template Card](https://github.com/piitaya/lovelace-mushroom) (available via HACS). It shows a countdown while heating or cooling, switches to "Your spa is ready!" once the target is reached, and hides itself entirely when the sensors are unavailable (heater off, no rate learned yet).

Replace `mspa_oslouvc` with your own entity name prefix.

```yaml
type: custom:mushroom-template-card
primary: >-
  {% set mins = states('sensor.mspa_oslouvc_time_to_target_temperature') | int(-1) %}
  {%- if mins <= 5 -%}
    Your spa is ready!
  {%- else -%}
    Spa ready at
  {%- endif %}
secondary: >-
  {% set t = states('sensor.mspa_oslouvc_ready_at') %}
  {% set mins = states('sensor.mspa_oslouvc_time_to_target_temperature') | int(-1) %}
  {%- if mins > 5 and t not in ('unavailable', 'unknown') -%}
    {{ (as_datetime(t) | as_local).strftime('%H:%M') }}
  {%- endif %}
features_position: bottom
icon: mdi:hot-tub
visibility:
  - condition: state
    entity: sensor.mspa_oslouvc_time_to_target_temperature
    state_not: unavailable
color: >-
  {% set d = state_attr('sensor.mspa_oslouvc_time_to_target_temperature', 'direction') %}
  {% set mins = states('sensor.mspa_oslouvc_time_to_target_temperature') | int(-1) %}
  {% if mins <= 5 %}
    green
  {% elif d == 'heating' %}
    red
  {% elif d == 'cooling' %}
    blue
  {% else %}
    green
  {% endif %}
grid_options:
  columns: full
  rows: 1
```

![example card](img/spa_ready_card.png)

## Readiness Sensor

The **Readiness** sensor gives a single, human-readable answer to "is the spa ready, and if not, when?".

| State | Meaning |
|-------|---------|
| `Ready` | Temperature is within 5 minutes of target |
| `HH:MM` | Estimated time to reach target today |
| `HH:MM +Nd` | Estimated time with a day offset (e.g. `+1d` = tomorrow) |

### Attributes

| Attribute | Description |
|-----------|-------------|
| `direction` | `heating`, `cooling`, or `at_target` |
| `minutes_remaining` | Integer countdown (null when ready or unavailable) |
| `color` | `green` (ready/at_target), `red` (heating), `light-blue` (cooling) |

The `color` attribute is intended for use with a **Mushroom Template Card**:

```yaml
type: custom:mushroom-template-card
primary: "{{ states('sensor.mspa_readiness') }}"
icon: mdi:hot-tub
color: "{{ state_attr('sensor.mspa_readiness', 'color') }}"
```

## Heat Schedule Sensor

The **Heat Schedule** sensor tells you when to start conditioning the spa for your next planned session. Give it a target ready time via an `input_datetime` helper and it works out the start time automatically — for both heating up and cooling down.

### Configuration

1. Create an `input_datetime` helper in Home Assistant (**Settings → Devices & Services → Helpers → Add Helper → Date and/or time**). Enable both **date** and **time**. Name it something like *Hot tub ready at*.

2. In the integration options (**Settings → Devices & Services → MSpa → ⚙️ Configure**):
   - **Schedule: Ready at** — select your `input_datetime` helper
   - **Schedule: Target Temperature** — the temperature to reach by the target time (default 40 °C)

3. Update the helper whenever you plan a session. The sensor automatically calculates the conditioning start time.

### States

| State | Meaning |
|-------|---------|
| `Not scheduled` | No target time set, or the set time has passed |
| `Ready` | The spa is already within 1 °C of the target temperature |
| `Start now` | Conditioning should begin immediately to reach the target in time |
| `Start at HH:MM` | Conditioning should start at this time today |
| `Start at HH:MM +Nd` | Conditioning should start in N days at this time |

The sensor works in both directions: if the spa needs to heat up it uses the learned bucket heating rates; if it needs to cool down it uses the learned cooling rate.

### Attributes

| Attribute | Description |
|-----------|-------------|
| `target_time` | ISO 8601 timestamp of the planned ready time |
| `start_at` | ISO 8601 timestamp when conditioning should begin |
| `target_temperature` | The configured target temperature (°C) |

### Automation example

Trigger the spa automatically when it is time to start conditioning for the next planned session.

The `Start now` state is the clean trigger — the sensor transitions to it at exactly the right moment based on the learned rates, so no polling or conditions are needed.

```yaml
alias: "MSpa – Start conditioning for planned session"
mode: single
trigger:
  - platform: state
    entity_id: sensor.mspa_heat_schedule
    to: "Start now"
  - platform: homeassistant
    event: start
condition:
  - condition: state
    entity_id: sensor.mspa_heat_schedule
    state: "Start now"
action:
  - service: climate.set_hvac_mode
    target:
      entity_id: climate.mspa
    data:
      hvac_mode: heat
  - service: climate.set_temperature
    target:
      entity_id: climate.mspa
    data:
      temperature: "{{ state_attr('sensor.mspa_heat_schedule', 'target_temperature') }}"
  - service: notify.mobile_app
    data:
      message: >
        MSpa conditioning started.
        Ready at: {{ state_attr('sensor.mspa_heat_schedule', 'target_time') | as_datetime | as_local | strftime('%H:%M') }}
```

Replace `climate.mspa` and `sensor.mspa_heat_schedule` with your actual entity IDs.

> **Two triggers, one condition**: The `state` trigger handles the normal case — it fires once when the sensor transitions to `Start now`. The `homeassistant.start` trigger is a safety net: if HA restarts while the sensor is already `Start now`, the state never *changes* so the first trigger won't fire; `homeassistant.start` catches that. The condition on both triggers ensures the action only runs if the sensor is genuinely in the `Start now` state. `mode: single` prevents both triggers from double-firing in edge cases.

> **How the timing is calculated**: The sensor uses the same temperature-bucketed rate model as the Time to Target Temperature sensor, accounting for how heating rate varies across the temperature range and applying the historical prediction bias correction.

## Power and Energy Monitoring

The integration provides comprehensive power and energy monitoring for your hot tub, including individual component sensors and total power/energy tracking that can be added directly to Home Assistant's Energy dashboard.

### Power Sensors

The integration provides the following power sensors that report real-time power consumption in watts:

- **Pump Power**: Reports pump power consumption (default: 60W when running)
- **Bubble Power**: Reports bubble blower power consumption (default: 900W when running)
- **Heater Power**: Reports heater power consumption based on heating state:
  - Preheat mode: 1500W (default)
  - Heating mode: 2000W (default)
  - Idle/off: 0W
- **Total Power**: Automatically calculates the sum of all active components and provides a breakdown in the sensor attributes

### Energy Sensor (Energy Dashboard Compatible)

The integration includes a **Total Energy** sensor that:
- Tracks cumulative energy consumption in kWh
- Uses the **Energy** device class for direct compatibility with Home Assistant's Energy dashboard
- Persists across Home Assistant restarts
- Calculates energy using trapezoidal integration for accuracy
- Can be added to the Energy dashboard under individual devices

To add the energy sensor to your Energy dashboard:
1. Go to **Settings** > **Dashboards** > **Energy**
2. Click **Add Consumption** under "Individual Devices"
3. Select your `Total Energy` sensor from the MSpa device
4. Click **Save**

### Calibrating Power Consumption Values

The default power consumption values are based on typical MSpa specifications, but actual power usage may vary by model and region. You can calibrate these values to match your specific hot tub:

1. **Find your MSpa specifications**: Check your hot tub's manual or specification plate for the actual power ratings of:
   - Filter pump (typically 40-80W)
   - Bubble blower (typically 800-1000W)
   - Heater during preheat (typically 1200-1500W)
   - Heater during normal heating (typically 1800-2200W)

2. **Adjust the values**:
   - Go to **Settings** > **Devices & Services** > **MSpa**
   - Click the **⚙️ cog wheel button** (Configure) on your MSpa integration
   - Enter the power consumption values for your specific model:
     - **Pump Power** (default: 60W)
     - **Bubble Power** (default: 900W)
     - **Heater Power (Preheat)** (default: 1500W)
     - **Heater Power (Heat)** (default: 2000W)
   - Click **Submit**
   
   ![Power consumption calibration dialog](img/power-calibration.png)

3. **Fine-tune based on experience**: If you have a way to measure actual power consumption (e.g., smart plug with power monitoring), you can further refine these values based on real-world measurements.

**Note**: The MSpa Comfort C-BE061 specifications show:
- Filter pump: 60W
- Bubble blower: 900W
- Heater: 1500W (preheat) / 2000W (heating)

These are used as defaults, but your model may differ.

## Temperature Unit Control

The MSpa hardware defaults to Fahrenheit when powered on, which can be inconvenient for users with Celsius-based systems. The integration provides automatic temperature unit management through configuration options.

### Display vs Device Temperature Units

**Display**: The integration always displays temperatures in your Home Assistant unit system (Settings → System → General). Home Assistant handles the conversion automatically.

**Device**: The MSpa device's physical display unit can be managed in two ways:
1. **Manual control**: Change the unit directly on the MSpa device or in the MSpa Link app
2. **Automatic management**: Enable the "Track temperature unit" option (see below) to automatically set the device to match your HA system on power-up

### Configuration Options

Two separate options are available in **Settings** > **Devices & Services** > **MSpa** > **⚙️ Configure**:

#### 1. Track Temperature Unit (Optional)
When enabled, the integration will automatically set the MSpa device's temperature unit to match your Home Assistant unit system whenever the device powers on:
- HA uses Metric (Celsius) → MSpa device set to Celsius
- HA uses Imperial (Fahrenheit) → MSpa device set to Fahrenheit

This eliminates the annoyance of the MSpa resetting to Fahrenheit after power outages.

**Note**: This only affects the MSpa device's physical display. The integration always displays in your HA system unit regardless of this setting.

#### 2. Restore Previous States After Power Outage (Optional)
When enabled, the integration will attempt to detect power cycles and restore device states (see State Restoration section below).

### State Restoration After Power Outage

The MSpa hardware resets to default values (Fahrenheit, 40°C target temperature, all features off) when power cycled. The integration can attempt to automatically restore your previous settings:

1. Go to **Settings** > **Devices & Services** > **MSpa**
2. Click the **⚙️ cog wheel button** (Configure)
3. Enable **"Restore previous states after power outage"**
4. Click **Submit**

When enabled, the integration will attempt to:
- Detect when the MSpa powers off and on (using multiple detection methods)
- Save the current state before power loss
- Automatically restore the following when power returns:
  - Target temperature
  - Heater state
  - Filter state
  - Ozone state
  - UVC state

**Notes**: 
- Power cycle detection uses multiple methods but may not catch every scenario (e.g., very brief power interruptions)
- Check the Home Assistant logs for power cycle detection confirmations
- This option works independently of "Track temperature unit". You can enable one, both, or neither based on your preferences.

## Upgrading to v3.0.0 (Multi-Device Support)

Version 3.0.0 introduces multi-device support. When upgrading from an earlier version:

1. **Upload the new version** and restart Home Assistant.
2. **Automatic migration** — your existing device and entities will be migrated automatically. No manual action is needed in most cases.
3. **To add a second hot tub** — go to **Settings** > **Devices & Services** > **Add Integration** > **MSpa**. Enter the same account credentials, and the config flow will show only devices that are not yet configured.

### Troubleshooting Migration Issues

If you experience any of the following after upgrading:
- Duplicate entities (old and new)
- Missing entities or entities stuck as "unavailable"
- Device showing without entities

**Resolution**: Remove the MSpa integration and re-add it. Your device will be rediscovered automatically and entities will be recreated cleanly.

## Thermostat popup

![Climate entity thermostat control popup](img/thermostat-popup.png)

## Example dashboard using mushroom cards:

![Dashboard example with mushroom cards showing hot tub controls](img/dashboard-example.png)

## Limitations

- **Multi-Region Support**: The ROW (Europe) region is well-tested. US and CH regions are experimental — see the [CHANGELOG](CHANGELOG.md) for details.
- It is not currently possible to determine which features your specific MSpa hot tub supports. If you find that some features, such as jet or ozone, do not work, it may be due to the specific model of your hot tub. You can disable the relevant entities in the Home Assistant UI.
- The safety lock feature is not available in this integration. You can still operate the safety lock through the MSpa Link app.


## Troubleshooting

- Make sure you are running the latest version of HACS.
- Check the Home Assistant logs for any errors if the component does not load.
- Ensure that you have created and are using a guest account for Home Assistant with its own email and password in the MSpa Link app.
- If upgrading from a version before 3.0.0 and entities are not working correctly, remove and re-add the integration (see [Upgrading to v3.0.0](#upgrading-to-v300-multi-device-support)).


## Developer Demo Mode

A built-in demo mode lets you add virtual spa devices without any physical hardware or cloud account. This is useful for testing the integration, developing dashboards, or exercising multi-device behaviour.

### How to activate

1. Go to **Settings** > **Devices & Services** > **Add Integration** > **MSpa**.
2. Enter the following credentials:
   - **Email**: `demo@mspa.test`
   - **Password**: anything (it is ignored)
   - **Region**: any
3. Three virtual devices are available to add one at a time:

| Device alias | Series | Model |
|---|---|---|
| DemoSpa Frame | FRAME | F-TU062W |
| DemoSpa Oslo | OSLOUVC | F-OS063WAP |
| DemoSpa Alpine | ALPINE | F-AL052D |

Repeat the "Add Integration" flow to add additional demo devices (already-configured ones are filtered out automatically).

### Behaviour

- **No network calls are made** — authentication, device list, status polling and commands are all handled locally.
- **Status polls** return realistic mock data. The water temperature drifts toward the target each poll so the climate entity looks alive.
- **Commands work** — toggling heater/filter/bubble, adjusting the target temperature etc. update the mock state immediately and are reflected on the next poll.
- The mock data intentionally includes some legacy diagnostic keys so that pass-through diagnostic sensor behaviour can be verified.

### Removing demo devices

Delete each demo entry the same way as a real one: **Settings** > **Devices & Services** > **MSpa** > three-dot menu > **Delete**.


## Support

For issues or feature requests, please open an issue in this repository.

<!-- Badge definitions -->
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-orange.svg
[hacs-url]: https://github.com/DTekNO/mspa-homeassistant
[hacs-validation-badge]: https://github.com/DTekNO/mspa-homeassistant/actions/workflows/validate.yaml/badge.svg
[hacs-validation-url]: https://github.com/DTekNO/mspa-homeassistant/actions/workflows/validate.yaml
[maintenance-badge]: https://img.shields.io/maintenance/yes/2026.svg
[release-badge]: https://img.shields.io/github/release/DTekNO/mspa-homeassistant.svg
[release-url]: https://github.com/DTekNO/mspa-homeassistant/releases
[downloads-badge]: https://img.shields.io/github/downloads/DTekNO/mspa-homeassistant/total
[downloads-latest]: https://img.shields.io/github/downloads/DTekNO/mspa-homeassistant/latest/total
