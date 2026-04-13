# MSpa Custom Component Integration and Installation via HACS

[![hacs][hacs-badge]][hacs-url]
[![Validate with HACS][hacs-validation-badge]][hacs-validation-url]
![Maintenance][maintenance-badge]
[![release][release-badge]][release-url]
![downloads][downloads-badge]



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
- `preheating`: The hot tub is in preheat mode (heat_state = 2), warming up the water before entering full heating mode.
- `heating`: The hot tub is actively heating the water (heat_state = 3).

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
- The mock shadow payload intentionally includes the legacy `wifivertion` key so that pass-through diagnostic sensor behaviour can be verified.

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
