"""Sensor platform for MSpa integration."""
import logging
from datetime import datetime, timezone, timedelta
from homeassistant.components.sensor import SensorStateClass, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import UnitOfPower, UnitOfEnergy, UnitOfTime
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DEFAULT_PUMP_POWER,
    DEFAULT_BUBBLE_POWER,
    DEFAULT_HEATER_POWER_PREHEAT,
    DEFAULT_HEATER_POWER_HEAT,
    CONF_SCHEDULE_DATETIME,
    CONF_SCHEDULE_TARGET_TEMP,
    CONF_SCHEDULE_LOOKAHEAD_DAYS,
    DEFAULT_SCHEDULE_TARGET_TEMP,
    DEFAULT_SCHEDULE_LOOKAHEAD_DAYS,
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
        MSpaReadinessSensor(coordinator),
        MSpaHeatScheduleSensor(coordinator, entry),
    ])

    # Device detail sensor — exposes extended info from /api/device/detail/ as attributes
    if getattr(coordinator, "device_detail", None):
        async_add_entities([MSpaDeviceDetailSensor(coordinator)])


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


# Temperature thresholds separating the three heating-rate buckets (°C).
# Bucket 0: T < _HEAT_BUCKET_T1  — cold, minimal thermal losses, fastest heating
# Bucket 1: _HEAT_BUCKET_T1 ≤ T < _HEAT_BUCKET_T2
# Bucket 2: T ≥ _HEAT_BUCKET_T2  — near setpoint, highest losses, slowest heating
_HEAT_BUCKET_T1 = 30.0
_HEAT_BUCKET_T2 = 37.0


def _heat_bucket_rate(coordinator, temp: float) -> float | None:
    """Return the best heating rate (°C/h) learned at a given water temperature.

    Tries the EMA for the bucket containing `temp`, then adjacent buckets,
    then the global flat EMA / device-reported fallback.  For buckets that have
    not yet received observations in the current heating session, the result is
    scaled by the session scalar (derived from the first-observed bucket) to
    reflect today's ambient conditions.
    """
    buckets = getattr(coordinator, "heat_rate_buckets", [None, None, None])
    session_scalar = getattr(coordinator, "_session_scalar", 1.0)
    fresh = getattr(coordinator, "_session_fresh_buckets", set())
    idx = 0 if temp < _HEAT_BUCKET_T1 else 1 if temp < _HEAT_BUCKET_T2 else 2
    rate = None
    source_idx = None
    if buckets[idx] is not None and buckets[idx] > 0:
        rate = buckets[idx]
        source_idx = idx
    else:
        for offset in (1, -1, 2, -2):
            i = idx + offset
            if 0 <= i < 3 and buckets[i] is not None and buckets[i] > 0:
                rate = buckets[i]
                source_idx = i
                break
    if rate is None:
        rate = _effective_heat_rate(coordinator)
    # Only apply the session scalar to buckets not yet observed this session.
    # Fresh buckets already contain today's real data — scaling them would be
    # double-counting the ambient correction.
    if rate is not None and session_scalar != 1.0 and source_idx not in fresh:
        return rate * session_scalar
    return rate


def _segmented_heating_minutes(from_temp: float, to_temp: float, coordinator) -> float | None:
    """Heating time (minutes) from `from_temp` to `to_temp` using per-bucket rates.

    Splits the range at bucket boundaries so each segment uses the rate observed
    at that temperature range, naturally modelling how thermal losses slow heating
    as the water approaches the setpoint.  Applies a historical bias correction
    derived from past prediction accuracy.  Returns None if no rate data is
    available for any required segment.
    """
    if from_temp >= to_temp:
        return 0.0
    boundaries = [from_temp]
    for threshold in (_HEAT_BUCKET_T1, _HEAT_BUCKET_T2):
        if from_temp < threshold < to_temp:
            boundaries.append(threshold)
    boundaries.append(to_temp)
    total_minutes = 0.0
    for i in range(len(boundaries) - 1):
        seg_start = boundaries[i]
        seg_end   = boundaries[i + 1]
        delta = seg_end - seg_start
        if delta <= 0:
            continue
        rate = _heat_bucket_rate(coordinator, seg_start)
        if rate is None or rate <= 0:
            return None
        total_minutes += (delta / rate) * 60.0
    # Apply historical bias correction from past prediction accuracy.
    bias = getattr(coordinator, "prediction_bias", 1.0)
    return total_minutes * bias


def _minutes_to_target(coordinator) -> int | None:
    """Return minutes remaining until the spa reaches its target temperature.

    Returns 0 when near target, None when no rate data is available.
    Shared by MSpaTempReachSensor and MSpaReadinessSensor.
    """
    if coordinator.near_target:
        return 0
    anchor_time   = coordinator.temp_anchor_time
    anchor_temp   = coordinator.temp_anchor_temp
    anchor_target = coordinator.temp_anchor_target
    if anchor_time is None or anchor_temp is None or anchor_target is None:
        return None
    if anchor_target > anchor_temp:
        anchor_minutes = _segmented_heating_minutes(anchor_temp, anchor_target, coordinator)
    elif anchor_target < anchor_temp:
        rate = _effective_cool_rate(coordinator)
        if rate is None:
            return None
        anchor_minutes = (abs(anchor_target - anchor_temp) / rate) * 60
    else:
        return 0
    if anchor_minutes is None:
        return None
    ready_at = anchor_time + timedelta(minutes=anchor_minutes)
    remaining = (ready_at - datetime.now(timezone.utc)).total_seconds() / 60
    return max(0, round(remaining))


def _spa_direction(coordinator) -> str | None:
    """Return 'heating', 'cooling', 'at_target', or None if data is unavailable."""
    data = coordinator._last_data
    try:
        water_temp = float(data.get("water_temperature"))
        target_temp = float(data.get("target_temperature"))
    except (TypeError, ValueError):
        return None
    if target_temp > water_temp:
        return "heating"
    if target_temp < water_temp:
        return "cooling"
    return "at_target"


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

    def _compute_minutes(self) -> int | None:
        """Return minutes to target, counting down each poll. None when unavailable."""
        return _minutes_to_target(self.coordinator)

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
        else:
            effective = None
        direction = _spa_direction(self.coordinator)

        computed_heat = getattr(self.coordinator, "computed_heat_rate", None)
        computed_cool = getattr(self.coordinator, "computed_cool_rate", None)
        raw = data.get("device_heat_perhour", 0)
        try:
            device_rate = int(raw) / 10.0 if int(raw) > 0 else None
        except (TypeError, ValueError):
            device_rate = None

        buckets = getattr(self.coordinator, "heat_rate_buckets", [None, None, None])
        session_scalar = getattr(self.coordinator, "_session_scalar", 1.0)
        prediction_bias = getattr(self.coordinator, "prediction_bias", 1.0)
        return {
            "direction": direction,
            "effective_rate_deg_per_hour": round(effective, 3) if effective is not None else None,
            "computed_heat_rate_deg_per_hour": round(computed_heat, 3) if computed_heat is not None else None,
            "computed_cool_rate_deg_per_hour": round(computed_cool, 3) if computed_cool is not None else None,
            "heat_rate_cold_deg_per_hour": round(buckets[0], 3) if buckets[0] is not None else None,
            "heat_rate_mid_deg_per_hour": round(buckets[1], 3) if buckets[1] is not None else None,
            "heat_rate_hot_deg_per_hour": round(buckets[2], 3) if buckets[2] is not None else None,
            "session_condition_scalar": round(session_scalar, 3),
            "prediction_bias": round(prediction_bias, 3),
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

    def _compute_ready_at(self):
        """Return ready-at timestamp, counting down each poll. None when unavailable."""
        if self.coordinator.near_target:
            return None
        anchor_time   = self.coordinator.temp_anchor_time
        anchor_temp   = self.coordinator.temp_anchor_temp
        anchor_target = self.coordinator.temp_anchor_target
        if anchor_time is None or anchor_temp is None or anchor_target is None:
            return None
        if anchor_target > anchor_temp:
            anchor_minutes = _segmented_heating_minutes(anchor_temp, anchor_target, self.coordinator)
        elif anchor_target < anchor_temp:
            rate = _effective_cool_rate(self.coordinator)
            if rate is None:
                return None
            anchor_minutes = (abs(anchor_target - anchor_temp) / rate) * 60
        else:
            return None
        if anchor_minutes is None:
            return None
        return anchor_time + timedelta(minutes=anchor_minutes)

    @property
    def available(self):
        return super().available and self._compute_ready_at() is not None

    @property
    def native_value(self):
        return self._compute_ready_at()


class MSpaReadinessSensor(MSpaSensorEntity):
    """Human-readable spa readiness sensor.

    State is 'Ready' when at or within 5 minutes of target temperature,
    otherwise the expected ready-at time in local time (e.g. '19:45' or
    '19:45 +1d').  Icon reflects direction: fire=heating, snowflake=cooling,
    hot-tub=ready.  No device_class or state_class — plain text sensor.
    """

    name = "Readiness"
    _attr_icon = "mdi:hot-tub"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_readiness_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info

    @property
    def available(self):
        return super().available and _minutes_to_target(self.coordinator) is not None

    @property
    def native_value(self):
        mins = _minutes_to_target(self.coordinator)
        if mins is None:
            return None
        if mins <= 5:
            return "Ready"
        rounded = round(mins / 5) * 5
        ready_at = datetime.now(timezone.utc) + timedelta(minutes=rounded)
        local_ready = ready_at.astimezone()
        days = (local_ready.date() - datetime.now().date()).days
        suffix = f" +{days}d" if days > 0 else ""
        return f"{local_ready.strftime('%H:%M')}{suffix}"

    @property
    def icon(self):
        return "mdi:hot-tub"

    @property
    def extra_state_attributes(self):
        direction = _spa_direction(self.coordinator)
        color = (
            "red" if direction == "heating"
            else "light-blue" if direction == "cooling"
            else "green"
        )
        return {
            "direction": direction,
            "minutes_remaining": _minutes_to_target(self.coordinator),
            "color": color,
        }


class MSpaHeatScheduleSensor(MSpaSensorEntity):
    """input_datetime-driven spa conditioning schedule sensor.

    Reads the target ready time from a configured input_datetime helper and
    uses the integration's learned rates to compute when conditioning must
    start to reach the target temperature by that time.  Works for both
    heating (target > current) and cooling (target < current).

    State: "Not scheduled" / "Ready" / "Start now" / "Start at HH:MM [+Nd]"
    Attributes: target_time, start_at (ISO 8601), target_temperature
    """

    name = "Heat Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        device_id = getattr(coordinator, "device_id", "unknown")
        self._attr_unique_id = f"mspa_heat_schedule_{device_id}"
        self._attr_device_info = self.device_info

    def _schedule_data(self):
        """Return (target_dt_utc, target_temp, start_at_utc) or a sentinel string."""
        options = self._config_entry.options
        dt_entity = options.get(CONF_SCHEDULE_DATETIME)
        if not dt_entity:
            return None

        dt_state = self.hass.states.get(dt_entity)
        if dt_state is None or dt_state.state in ("unknown", "unavailable"):
            return None

        target_dt = dt_util.parse_datetime(dt_state.state)
        if target_dt is None:
            return None
        if target_dt.tzinfo is None:
            target_dt = dt_util.as_local(target_dt)
        target_utc = dt_util.as_utc(target_dt)
        now_utc = dt_util.utcnow()

        if target_utc <= now_utc:
            return None

        days_until = (target_utc - now_utc).total_seconds() / 86400
        lookahead = int(options.get(CONF_SCHEDULE_LOOKAHEAD_DAYS, DEFAULT_SCHEDULE_LOOKAHEAD_DAYS))
        if days_until > lookahead:
            return None

        target_temp = float(options.get(CONF_SCHEDULE_TARGET_TEMP, DEFAULT_SCHEDULE_TARGET_TEMP))

        data = self.coordinator._last_data
        try:
            current_temp = float(data.get("water_temperature"))
        except (TypeError, ValueError):
            return None

        if abs(current_temp - target_temp) <= 1.0:
            return "ready"

        if target_temp > current_temp:
            minutes_needed = _segmented_heating_minutes(current_temp, target_temp, self.coordinator)
        else:
            rate = _effective_cool_rate(self.coordinator)
            if rate is None:
                return None
            minutes_needed = ((current_temp - target_temp) / rate) * 60.0

        if minutes_needed is None:
            return None

        start_at_utc = target_utc - timedelta(minutes=minutes_needed)
        return (target_utc, target_temp, start_at_utc)

    @property
    def native_value(self):
        result = self._schedule_data()
        if result is None:
            return "Not scheduled"
        if result == "ready":
            return "Ready"

        _, _, start_at_utc = result
        now_utc = dt_util.utcnow()

        if now_utc >= start_at_utc:
            return "Start now"

        local_start = dt_util.as_local(start_at_utc)
        days = (local_start.date() - dt_util.now().date()).days
        suffix = f" +{days}d" if days > 0 else ""
        return f"Start at {local_start.strftime('%H:%M')}{suffix}"

    @property
    def extra_state_attributes(self):
        result = self._schedule_data()
        if not isinstance(result, tuple):
            return {}
        target_utc, target_temp, start_at_utc = result
        return {
            "target_time": target_utc.isoformat(),
            "start_at": start_at_utc.isoformat(),
            "target_temperature": target_temp,
        }


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


class MSpaDeviceDetailSensor(MSpaSensorEntity):
    """Diagnostic sensor exposing extended device detail from /api/device/detail/.

    Fetched once on init. The state is the product series; all other detail
    fields are exposed as attributes so you can inspect them from the UI.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_name = "Device Detail"
        self._attr_unique_id = f"mspa_device_detail_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._attr_icon = "mdi:information-slab-circle-outline"

    @property
    def native_value(self):
        detail = getattr(self.coordinator, "device_detail", None) or {}
        return detail.get("product_series") or getattr(self.coordinator, "series", None)

    @property
    def extra_state_attributes(self):
        detail = getattr(self.coordinator, "device_detail", None) or {}
        if not detail:
            return {}
        # Expose the interesting fields; skip certificate (very long) and redundant IDs
        skip_keys = {"certificate", "enduser_id", "app_id"}
        attrs = {}
        for k, v in detail.items():
            if k in skip_keys:
                continue
            # Convert unix epoch to ISO timestamp
            if k == "warning_updated_at" and isinstance(v, (int, float)) and v > 0:
                v = datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
            attrs[k] = v
        return attrs
