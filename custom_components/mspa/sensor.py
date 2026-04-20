"""Sensor platform for MSpa integration."""
import logging
from datetime import datetime, timezone, timedelta
from homeassistant.components.sensor import SensorStateClass, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import UnitOfPower, UnitOfEnergy, UnitOfTime
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    DEFAULT_PUMP_POWER,
    DEFAULT_BUBBLE_POWER,
    DEFAULT_HEATER_POWER_PREHEAT,
    DEFAULT_HEATER_POWER_HEAT
)
from .entity import MSpaSensorEntity, MSpaBinarySensorEntity

_LOGGER = logging.getLogger(__name__)

SENSOR_TYPES = {
    "water_temperature": ["Water Temperature", "°C", SensorStateClass.MEASUREMENT, SensorDeviceClass.TEMPERATURE]
}

# Keys in coordinator._last_data that are handled by dedicated structured sensors.
# Everything else becomes a MSpaDiagnosticSensor automatically, using the payload
# key name verbatim — new/changed keys appear as new entities.
# NOTE: device_heat_perhour is intentionally NOT listed here — it still produces a
# diagnostic sensor AND is used by the temperature-reach sensors as fallback.
_STRUCTURED_KEYS = frozenset({
    "water_temperature", "target_temperature",
    "heater", "filter", "bubble", "jet", "ozone", "uvc",
    "bubble_level", "fault",
})

# Keys whose values are numeric measurements (affects SensorStateClass).
MEASUREMENT_KEYS = frozenset({
    "temperature_unit",
    "filter_current",
    "heat_state",
    "heat_time",
    "filter_life",
    "device_heat_perhour",
})


def _get_option_int(config_entry, key: str, default: int) -> int:
    """Get a non-negative integer option from a config entry."""
    opts = getattr(config_entry, "options", {}) or {}
    val = opts.get(key, default)
    try:
        ival = int(val)
        if ival < 0:
            raise ValueError
        return ival
    except (TypeError, ValueError):
        return default


def _calculate_total_power(coordinator, config_entry) -> float:
    """Calculate total power consumption in watts from all active components."""
    total_power = 0
    data = coordinator._last_data

    pump_power = _get_option_int(config_entry, "pump_power", DEFAULT_PUMP_POWER)
    bubble_power = _get_option_int(config_entry, "bubble_power", DEFAULT_BUBBLE_POWER)
    heater_preheat_power = _get_option_int(config_entry, "heater_power_preheat", DEFAULT_HEATER_POWER_PREHEAT)
    heater_heat_power = _get_option_int(config_entry, "heater_power_heat", DEFAULT_HEATER_POWER_HEAT)

    if data.get("filter") == "on":
        total_power += pump_power
    if data.get("bubble") == "on":
        total_power += bubble_power
    if data.get("heater") == "on":
        heat_state = data.get("heat_state")
        if heat_state == 2:
            total_power += heater_preheat_power
        elif heat_state == 3:
            total_power += heater_heat_power

    return total_power

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        MSpaSensor(coordinator, key)
        for key in SENSOR_TYPES
    ]

    async_add_entities(entities)
    async_add_entities([MSpaFaultSensor(coordinator)], update_before_add=True)
    async_add_entities([MSpaFilterSensor(coordinator)], update_before_add=True)
    async_add_entities([MSpaHeaterTimerBinarySensor(coordinator)], update_before_add=True)
    async_add_entities([MSpaHeaterTimerTimeSensor(coordinator)], update_before_add=True)
    # Pass the config entry so the power sensors can read per-device options
    async_add_entities([MSpaHeaterPowerSensor(coordinator, entry)], update_before_add=True)
    async_add_entities([MSpaTotalPowerSensor(coordinator, entry)], update_before_add=True)
    # Energy sensors for Energy dashboard
    async_add_entities([MSpaTotalEnergySensor(coordinator, entry)], update_before_add=True)

    # Dynamically create diagnostic sensors for every key that came back in the
    # first status poll, using the payload key name directly.  This means firmware
    # renames (e.g. wifivertion → wifi_version) are followed automatically without
    # any code changes — new keys appear, removed keys disappear.
    diagnostic_sensors = [
        MSpaDiagnosticSensor(coordinator, key, key.replace("_", " ").title())
        for key in sorted(coordinator._last_data.keys())
        if key not in _STRUCTURED_KEYS
    ]

    # Firmware version: combines wifi_version + mcu_version from the device list
    # (matches the format shown in the MSpa app, e.g. "141-3A1").
    if getattr(coordinator, "wifi_version", None) or getattr(coordinator, "mcu_version", None):
        diagnostic_sensors.append(MSpaFirmwareVersionSensor(coordinator))

    async_add_entities(diagnostic_sensors)

    # Temperature-reach sensors are registered for every spa.
    # They show as unavailable until a reliable rate is established
    # (computed EMA for heating/cooling, or device_heat_perhour as fallback for heating).
    async_add_entities([
        MSpaTempReachSensor(coordinator),
        MSpaTempReachReadyAtSensor(coordinator),
    ])


class MSpaSensor(MSpaSensorEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator)

        if not key in SENSOR_TYPES:
            _LOGGER.error("Unknown sensor key: %s", key)
            return
        self._key = key
        self._attr_name = SENSOR_TYPES[key][0]
        self._attr_native_unit_of_measurement = SENSOR_TYPES[key][1]
        self._attr_unique_id = f"mspa_{key}_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_state_class = SENSOR_TYPES[key][2]
        self._attr_device_class = SENSOR_TYPES[key][3]
        self._attr_device_info = self.device_info
        # Water temperature is redundant with climate entity - make it diagnostic and disabled by default
        if key == "water_temperature":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self):
        return self.coordinator._last_data.get(self._key)

# This sensor is used for diagnostic purposes.
# It retrieves various diagnostic information from the MSpa system.
# The keys are defined in the DIAGNOSTIC_KEYS list.
# Each sensor has a unique ID based on its key and the device ID.
# The state class is set to MEASUREMENT for keys that are measurements.
# The entity category is set to DIAGNOSTIC.
# The entity registry is disabled by default for these sensors.
# The entity picture is set to None.
# The state is retrieved from the coordinator's last data based on the key.
# The icon is not set, but can be customized if needed.
# The entity name is derived from the key, replacing underscores with spaces and capitalizing.
# The entity is registered with the Home Assistant entity registry.
# The entity is used to provide diagnostic information about the MSpa system.
# It can be used to monitor the status of the MSpa system and troubleshoot issues.
# The entity is not intended for user interaction, but rather for monitoring and diagnostics.
class MSpaDiagnosticSensor(MSpaSensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator)
        self._attr_device_info = self.device_info

        self.coordinator = coordinator
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"mspa_{key}_{getattr(coordinator, 'device_id', 'unknown')}"
        if key in MEASUREMENT_KEYS:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def state(self):
        return self.coordinator._last_data.get(self._key)

    @property
    def entity_picture(self):
        return None


class MSpaFirmwareVersionSensor(MSpaDiagnosticSensor):
    """Combined firmware version, matching the MSpa app format (e.g. '141-3A1')."""

    def __init__(self, coordinator):
        super().__init__(coordinator, "__firmware_version__", "Firmware Version")
        self._attr_unique_id = f"mspa_firmware_version_{getattr(coordinator, 'device_id', 'unknown')}"

    @property
    def state(self):
        wifi = getattr(self.coordinator, "wifi_version", None) or ""
        mcu = getattr(self.coordinator, "mcu_version", None) or ""
        # Strip the 'mcu-' prefix MSpa uses internally (app shows e.g. '141-3A1', not '141-mcu-3A1')
        if mcu.startswith("mcu-"):
            mcu = mcu[4:]
        if wifi and mcu:
            return f"{wifi}-{mcu}"
        return wifi or mcu or None

# Hysteresis thresholds for the temperature-reach sensors.
# Sensors become unavailable when the water reaches within _NEAR_TARGET_DEACTIVATE
# of the target, and only re-activate once the gap exceeds _NEAR_TARGET_ACTIVATE
# (one full 0.5 °C sensor step).  This prevents flickering during thermostat
# cycling while still showing the sensors during genuine heating/cooling runs.
_NEAR_TARGET_DEACTIVATE = 0.25  # °C  — deactivate (effectively "exactly at target")
_NEAR_TARGET_ACTIVATE   = 0.5   # °C  — reactivate (one full sensor step away)


def _effective_heat_rate(coordinator) -> float | None:
    """Return the best available heating rate in °C/h.

    Preference order:
    1. coordinator.computed_heat_rate  — learned from observed temperature rise
       (updated by coordinator on every poll via EMA with outlier rejection)
    2. device_heat_perhour payload key  — device-reported estimate (some models only)

    Returns None when neither source has a usable value yet.
    """
    # Prefer the learned rate — it adapts to real-world conditions
    computed = getattr(coordinator, "computed_heat_rate", None)
    if computed is not None and computed > 0:
        return computed

    # Fall back to device-reported rate (1/10 °C per hour)
    raw = coordinator._last_data.get("device_heat_perhour", 0)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return None
    if raw > 0:
        return raw / 10.0

    return None


def _effective_cool_rate(coordinator) -> float | None:
    """Return the best available passive cooling rate in °C/h (positive).

    Only uses the computed EMA — there is no device-reported cooling rate fallback.
    Returns None until enough cooling samples have been gathered.
    """
    computed = getattr(coordinator, "computed_cool_rate", None)
    if computed is not None and computed > 0:
        return computed
    return None


def _effective_rate(coordinator, water_temp: float, target_temp: float) -> float | None:
    """Return the appropriate rate (°C/h, positive) for the current direction.

    - target_temp > water_temp  →  heating: uses _effective_heat_rate
    - target_temp < water_temp  →  cooling: uses _effective_cool_rate
    - equal                     →  None (already at target)
    """
    if target_temp > water_temp:
        return _effective_heat_rate(coordinator)
    if target_temp < water_temp:
        return _effective_cool_rate(coordinator)
    return None


class MSpaTempReachSensor(MSpaSensorEntity):
    """Estimated minutes until the spa reaches its target temperature.

    Works in both directions: counts down while heating up *or* while cooling
    down to a lower target.  Uses the best available rate for the current
    direction — the coordinator's learned EMA first, then the device-reported
    `device_heat_perhour` value as a heating fallback.
    Returns unavailable until a rate has been established.
    """

    name = "Time to Target Temperature"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_time_to_target_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._near_target = False  # hysteresis state

    def _compute_minutes(self) -> int | None:
        """Return minutes to target, counting down each poll. None when unavailable."""
        data = self.coordinator._last_data
        try:
            water_temp = float(data.get("water_temperature"))
            target_temp = float(data.get("target_temperature"))
        except (TypeError, ValueError):
            return None
        degrees_remaining = target_temp - water_temp
        # Hysteresis: deactivate when at target, reactivate only after a full sensor step
        if abs(degrees_remaining) < _NEAR_TARGET_DEACTIVATE:
            self._near_target = True
        elif abs(degrees_remaining) >= _NEAR_TARGET_ACTIVATE:
            self._near_target = False
        if self._near_target:
            return None
        rate_per_hour = _effective_rate(self.coordinator, water_temp, target_temp)
        if rate_per_hour is None:
            return None
        # Compute ready_at from coordinator anchor, then derive remaining minutes.
        # The anchor was set when temp/target last changed, so this counts down
        # between temperature steps without per-sensor state.
        anchor_time   = self.coordinator.temp_anchor_time
        anchor_temp   = self.coordinator.temp_anchor_temp
        anchor_target = self.coordinator.temp_anchor_target
        if anchor_time is None or anchor_temp is None or anchor_target is None:
            return None
        anchor_minutes = (abs(anchor_target - anchor_temp) / rate_per_hour) * 60
        ready_at = anchor_time + timedelta(minutes=anchor_minutes)
        remaining = (ready_at - datetime.now(timezone.utc)).total_seconds() / 60
        return max(0, round(remaining))

    @property
    def available(self):
        return super().available and self._compute_minutes() is not None

    @property
    def native_value(self):
        return self._compute_minutes()

    @property
    def extra_state_attributes(self):
        data = self.coordinator._last_data
        try:
            water_temp = float(data.get("water_temperature"))
            target_temp = float(data.get("target_temperature"))
        except (TypeError, ValueError):
            water_temp = target_temp = None

        if water_temp is not None and target_temp is not None:
            effective = _effective_rate(self.coordinator, water_temp, target_temp)
            if target_temp > water_temp:
                direction = "heating"
            elif target_temp < water_temp:
                direction = "cooling"
            else:
                direction = "at_target"
        else:
            effective = None
            direction = None

        computed_heat = getattr(self.coordinator, "computed_heat_rate", None)
        computed_cool = getattr(self.coordinator, "computed_cool_rate", None)
        raw = data.get("device_heat_perhour", 0)
        try:
            device_rate = int(raw) / 10.0 if int(raw) > 0 else None
        except (TypeError, ValueError):
            device_rate = None

        return {
            "direction": direction,
            "effective_rate_deg_per_hour": round(effective, 3) if effective is not None else None,
            "computed_heat_rate_deg_per_hour": round(computed_heat, 3) if computed_heat is not None else None,
            "computed_cool_rate_deg_per_hour": round(computed_cool, 3) if computed_cool is not None else None,
            "device_rate_deg_per_hour": device_rate,
            "current_temperature": water_temp,
            "target_temperature": target_temp,
        }


class MSpaTempReachReadyAtSensor(MSpaSensorEntity):
    """Timestamp at which the spa is expected to reach its target temperature.

    Works in both directions (heating up or cooling down).  Returns `None`
    (→ unavailable) once the target is already reached, making it trivial to
    conditionally show/hide the sensor in dashboards and automations using the
    standard `is_unavailable` / `is_available` tests.
    """

    name = "Ready At"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_heating_ready_at_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._near_target = False  # hysteresis state

    def _compute_ready_at(self):
        """Return ready-at timestamp, counting down each poll. None when unavailable."""
        data = self.coordinator._last_data
        try:
            water_temp = float(data.get("water_temperature"))
            target_temp = float(data.get("target_temperature"))
        except (TypeError, ValueError):
            return None
        degrees_remaining = target_temp - water_temp
        # Hysteresis: deactivate when at target, reactivate only after a full sensor step
        if abs(degrees_remaining) < _NEAR_TARGET_DEACTIVATE:
            self._near_target = True
        elif abs(degrees_remaining) >= _NEAR_TARGET_ACTIVATE:
            self._near_target = False
        if self._near_target:
            return None
        rate_per_hour = _effective_rate(self.coordinator, water_temp, target_temp)
        if rate_per_hour is None:
            return None
        anchor_time   = self.coordinator.temp_anchor_time
        anchor_temp   = self.coordinator.temp_anchor_temp
        anchor_target = self.coordinator.temp_anchor_target
        if anchor_time is None or anchor_temp is None or anchor_target is None:
            return None
        anchor_minutes = (abs(anchor_target - anchor_temp) / rate_per_hour) * 60
        return anchor_time + timedelta(minutes=anchor_minutes)

    @property
    def available(self):
        return super().available and self._compute_ready_at() is not None

    @property
    def native_value(self):
        return self._compute_ready_at()


# This sensor is used to indicate faults or warnings in the MSpa system.
class MSpaFaultSensor(MSpaDiagnosticSensor):
    name = "Fault"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "fault", "Fault")
        self._attr_unique_id = f"mspa_fault_{getattr(coordinator, 'device_id', 'unknown')}"

    @property
    def state(self):
        return self.coordinator._last_data.get("fault", "OK")

    @property
    def icon(self):
        return "mdi:alert" if self.state != "OK" else "mdi:check-circle"

# This sensor monitors the filter status of the MSpa device.
# It checks the "warning" key in the coordinator's last data.
# If the warning is "A0", it indicates that the filter is dirty.
# Otherwise, it indicates that the filter is OK.
class MSpaFilterSensor(MSpaDiagnosticSensor):
    name = "Filter status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "filter_status", "Filter status")
        self._attr_unique_id = f"mspa_filter_status_{getattr(coordinator, 'device_id', 'unknown')}"

    @property
    def state(self):
        warning = self.coordinator._last_data.get("warning", "")
        return "Dirty" if warning == "A0" else "OK"

    @property
    def icon(self):
        return "mdi:filter-remove" if self.state == "Dirty" else "mdi:filter"

class MSpaHeaterTimerBinarySensor(MSpaBinarySensorEntity):
    name = "Heater timer"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_heater_timer_enabled_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info

    @property
    def is_on(self):
        return bool(self.coordinator._last_data.get("heat_time_switch", 0))

    @property
    def icon(self):
        return "mdi:timer-outline" if self.is_on else "mdi:timer-off-outline"

class MSpaHeaterTimerTimeSensor(MSpaSensorEntity):
    name = "Heater timer remaining"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_heater_timer_remaining_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info

    @property
    def native_value(self):
        return self.coordinator._last_data.get("heat_time", 0)

class MSpaHeaterPowerSensor(MSpaSensorEntity):
    """Sensor to report current heater power consumption based on heat state and per-device options."""
    name = "Heater Power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_heater_power_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._config_entry = config_entry

    @property
    def native_value(self):
        """Return the power consumption based on heat state using per-device options."""
        heat_state = self.coordinator._last_data.get("heat_state")
        heater_on = self.coordinator._last_data.get("heater") == "on"

        if not heater_on:
            return 0

        preheat_w = _get_option_int(self._config_entry, "heater_power_preheat", DEFAULT_HEATER_POWER_PREHEAT)
        heat_w = _get_option_int(self._config_entry, "heater_power_heat", DEFAULT_HEATER_POWER_HEAT)

        if heat_state == 2:
            return preheat_w
        elif heat_state == 3:
            return heat_w
        elif heat_state == 4:
            return 0
        else:
            return 0

    @property
    def icon(self):
        power = self.native_value
        return "mdi:lightning-bolt" if power > 0 else "mdi:power-plug-off"


class MSpaTotalPowerSensor(MSpaSensorEntity):
    """Sensor to report total power consumption based on all active components."""
    name = "Total Power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_total_power_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._config_entry = config_entry

    @property
    def native_value(self):
        """Calculate total power consumption based on active components."""
        return _calculate_total_power(self.coordinator, self._config_entry)

    @property
    def icon(self):
        power = self.native_value
        if power == 0:
            return "mdi:power-plug-off"
        elif power < 500:
            return "mdi:gauge-low"
        elif power < 1500:
            return "mdi:gauge"
        else:
            return "mdi:gauge-full"

    @property
    def extra_state_attributes(self):
        """Return additional attributes showing breakdown of power usage."""
        pump_power = _get_option_int(self._config_entry, "pump_power", DEFAULT_PUMP_POWER)
        bubble_power = _get_option_int(self._config_entry, "bubble_power", DEFAULT_BUBBLE_POWER)
        heater_preheat_power = _get_option_int(self._config_entry, "heater_power_preheat", DEFAULT_HEATER_POWER_PREHEAT)
        heater_heat_power = _get_option_int(self._config_entry, "heater_power_heat", DEFAULT_HEATER_POWER_HEAT)
        
        filter_on = self.coordinator._last_data.get("filter") == "on"
        bubble_on = self.coordinator._last_data.get("bubble") == "on"
        heater_on = self.coordinator._last_data.get("heater") == "on"
        heat_state = self.coordinator._last_data.get("heat_state")
        
        return {
            "pump_power": pump_power if filter_on else 0,
            "bubble_power": bubble_power if bubble_on else 0,
            "heater_power": (heater_preheat_power if heat_state == 2 else heater_heat_power if heat_state == 3 else 0) if heater_on else 0,
            "configured_pump_power": pump_power,
            "configured_bubble_power": bubble_power,
            "configured_heater_preheat_power": heater_preheat_power,
            "configured_heater_heat_power": heater_heat_power,
        }


class MSpaTotalEnergySensor(MSpaSensorEntity, RestoreEntity):
    """Sensor to track total energy consumption in kWh for Energy dashboard."""
    name = "Total Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_total_energy_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._config_entry = config_entry
        self._total_energy = 0.0  # Total energy in kWh
        self._last_update_time = None
        self._last_power = 0

    async def async_added_to_hass(self):
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last known energy value
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._total_energy = float(last_state.state)
                _LOGGER.debug(f"Restored energy sensor state: {self._total_energy} kWh")
            except (ValueError, TypeError):
                _LOGGER.warning(f"Could not restore energy sensor state: {last_state.state}")
                self._total_energy = 0.0
        
        self._last_update_time = datetime.now()

    @property
    def native_value(self):
        """Return the total energy consumption in kWh."""
        # Calculate energy since last update using trapezoidal rule (average power * time)
        current_time = datetime.now()
        current_power = _calculate_total_power(self.coordinator, self._config_entry)
        
        if self._last_update_time is not None:
            time_diff_hours = (current_time - self._last_update_time).total_seconds() / 3600
            # Use average of last and current power (trapezoidal integration)
            avg_power = (self._last_power + current_power) / 2
            energy_increment = (avg_power * time_diff_hours) / 1000  # Convert Wh to kWh
            self._total_energy += energy_increment
            
            if energy_increment > 0:
                _LOGGER.debug(
                    f"Energy increment: {energy_increment:.6f} kWh "
                    f"(avg power: {avg_power:.1f}W over {time_diff_hours*3600:.1f}s)"
                )
        
        self._last_update_time = current_time
        self._last_power = current_power
        
        return round(self._total_energy, 3)

    @property
    def icon(self):
        return "mdi:lightning-bolt"

    @property
    def extra_state_attributes(self):
        """Return additional attributes."""
        current_power = _calculate_total_power(self.coordinator, self._config_entry)
        return {
            "current_power_w": current_power,
            "last_reset": None,  # Total increasing sensor, never resets
        }
        
