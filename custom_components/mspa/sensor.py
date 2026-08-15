"""Sensor platform for MSpa integration."""
import logging
from datetime import datetime, timezone, timedelta
from homeassistant.components.sensor import SensorStateClass, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import UnitOfPower, UnitOfEnergy
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DEFAULT_PUMP_POWER,
    DEFAULT_BUBBLE_POWER,
    DEFAULT_HEATER_POWER_PREHEAT,
    DEFAULT_HEATER_POWER_HEAT,
    CONF_SCHEDULE_TARGET_TEMP,
    CONF_SCHEDULE_LOOKAHEAD_DAYS,
    DEFAULT_SCHEDULE_TARGET_TEMP,
    DEFAULT_SCHEDULE_LOOKAHEAD_DAYS,
)
from .entity import MSpaSensorEntity, MSpaBinarySensorEntity
from .predictor import (
    HEAT_BUCKET_T1,
    HEAT_BUCKET_T2,
    HeatPredictor,
    ambient_rate_factor,
)

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

    # Fall back to device-reported rate (1/10 °C per hour).
    # Clamped to 0.5–2.0 °C/h — physically plausible range for MSpa units.
    # Only used as a cold-start seed until the EMA has real observations.
    raw = coordinator._last_data.get("device_heat_perhour", 0)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return None
    if raw > 0:
        return max(0.5, min(2.0, raw / 10.0))

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
_HEAT_BUCKET_T1 = HEAT_BUCKET_T1
_HEAT_BUCKET_T2 = HEAT_BUCKET_T2


def _heat_bucket_rate(coordinator, temp: float) -> float | None:
    """Best learned rate (°C/h) at a water temperature — see predictor.HeatPredictor."""
    return HeatPredictor.from_coordinator(coordinator).bucket_rate(temp)


def _segmented_heating_minutes(from_temp: float, to_temp: float, coordinator) -> float | None:
    """Heating time (minutes) between two temperatures — see predictor.HeatPredictor.

    Single implementation, shared with the coordinator's trigger. These used to be
    two separate copies that had drifted apart; see predictor.py.
    """
    return HeatPredictor.from_coordinator(coordinator).heating_minutes(from_temp, to_temp)


def _anchor_eta_utc(coordinator, target_temp: float, now_utc) -> "datetime | None":
    """Return the anchor-based UTC ETA to reach target_temp when heating, or None.

    Uses the last confirmed temperature anchor (set whenever water_temp or
    target_temp changes), so the ETA is stable between API readings.

    If no temperature update arrives within the expected time for 0.5°C of
    heating plus a 2-minute margin, the anchor is stale and the ETA is pushed
    forward proportionally to communicate slower-than-predicted heating.
    """
    anc_time = coordinator.temp_anchor_time
    anc_temp = coordinator.temp_anchor_temp

    # Measure from whichever is later: the anchor, or the moment heating began.
    #
    # The anchor marks when the temperature last *changed*.  During a cool-down that
    # is a cooling transition, so counting elapsed time from it treats minutes of
    # cooling as if they were heating progress and reports a finish far too early —
    # up to a full step of it.  Once a crossing occurs while heating the anchor
    # becomes the later of the two and takes over again, which restores the reason it
    # exists: an ETA that is stable between readings rather than drifting with the
    # clock.
    #
    # With this, at the instant the heater starts, Ready at and the Heat Schedule
    # both reduce to `now + heating_minutes(current → target)` and agree by
    # construction.
    heating_since = getattr(coordinator, "heating_since", None)
    if anc_time is not None and heating_since is not None and heating_since > anc_time:
        anc_time = heating_since

    if anc_time is None or anc_temp is None or target_temp is None:
        _LOGGER.debug("ready_at anchor: missing data (anc_time=%s anc_temp=%s target=%.1f) → None",
                      anc_time, anc_temp, target_temp or 0)
        return None

    # Within a session, integrate the rate curve frozen at its start and hold the
    # opening estimate until the settle guard passes.
    #
    # Both halves are measured (analysis/settle_time.py, four recorded sessions).
    # Replanning at each 0.5 °C crossing converges to 0 min at the finish, where
    # holding carries its opening error all the way in — 35 min on the worst session,
    # still 35 min wrong at the moment the spa was ready. Before the settle point the
    # opposite holds: a recompute from a partial span is badly wrong, because the plan
    # is only accurate over a full span and the opening crossings measure position
    # within the 0.5 °C band rather than heating.
    #
    # Freezing the rates matters as much as the settle: recomputing with rates that
    # move as they are learned was the shipped behaviour, and it was worse than either
    # (21 min mean against 10 for holding).
    plan = coordinator.session_plan()
    if plan is not None and not coordinator.session_settled(anc_temp, now_utc):
        opening = coordinator.session_opening_eta()
        if opening is not None:
            return opening
    if plan is not None:
        mins = plan.heating_minutes(anc_temp, target_temp)
    else:
        mins = _segmented_heating_minutes(anc_temp, target_temp, coordinator)
    if mins is None:
        _LOGGER.debug("ready_at anchor: no rate for %.1f→%.1f → None", anc_temp, target_temp)
        return None
    if mins == 0.0:
        _LOGGER.debug("ready_at anchor: already at target (%.1f→%.1f)", anc_temp, target_temp)
        return anc_time

    eta_utc = anc_time + timedelta(minutes=mins)
    elapsed_mins = (now_utc - anc_time).total_seconds() / 60.0

    # Staleness: if no temp update for longer than expected 0.5°C step + 2-min margin,
    # push ETA forward proportionally to communicate slower-than-predicted heating.
    step_mins = _segmented_heating_minutes(anc_temp, min(anc_temp + 0.5, target_temp), coordinator) or 1.0
    if elapsed_mins > step_mins + 2.0:
        overrun = elapsed_mins - step_mins
        eta_utc = anc_time + timedelta(minutes=mins + overrun)
        _LOGGER.debug(
            "ready_at anchor: stale (elapsed=%.1fmin > step=%.1fmin+2) overrun=%.1fmin → eta=%s",
            elapsed_mins, step_mins, overrun, eta_utc.isoformat(),
        )
    else:
        _LOGGER.debug(
            "ready_at anchor: anc=%.1f°C target=%.1f°C total=%.1fmin elapsed=%.1fmin → eta=%s",
            anc_temp, target_temp, mins, elapsed_mins, eta_utc.isoformat(),
        )

    return eta_utc


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


def _relevant_target(coordinator) -> "float | None":
    """The temperature target currently driving the Ready at display.

    The schedule temperature when a schedule is pending or has triggered,
    otherwise the thermostat setpoint — mirroring _compute_ready_at's contexts.
    """
    now_utc = datetime.now(timezone.utc)
    sra = _sra_utc(coordinator)
    if coordinator._schedule_triggered or (sra is not None and sra > now_utc):
        st = coordinator.schedule_target_temp
        if st is not None:
            return float(st)
    try:
        target = float(coordinator._last_data.get("target_temperature") or 0)
    except (TypeError, ValueError):
        return None
    return target or None


def _segmented_effective_rate(coordinator, from_temp, to_temp) -> "float | None":
    """Effective heating rate (°C/h) over a span, as the estimator prices it.

    Integrates the per-bucket rates with all corrections (fresh-bucket /
    session scalar / ambient factor) and the prediction bias — i.e. the rate
    that is actually in effect for the current estimate, unlike the flat EMA.
    """
    if from_temp is None or to_temp is None or to_temp <= from_temp:
        return None
    return HeatPredictor.from_coordinator(coordinator).effective_rate(from_temp, to_temp)


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


# Sentinel: distinguishes "state never yet reported" from None (no data).
_UNSET = object()

# Rate-capped ETA display (Ready at sensor).  Corrections to the live estimate
# — the lump when a temperature crossing re-anchors, the +1 min/min creep when
# the anchor is stale — are followed at a bounded pace instead of jumping, so
# quantization effects land as gentle ramps.  A move larger than the snap
# threshold is a genuine replan (schedule/setpoint change) and is followed
# immediately.
_ETA_SLEW_MIN_PER_MIN = 1.0   # max displayed-ETA movement per wall-clock minute
_ETA_DEADBAND_MIN = 5.0       # ignore raw movement smaller than this
_ETA_ROUND_MIN = 5            # display granularity, minutes

# Why these values.  The first attempt capped movement at 3 min per wall minute
# and snapped anything beyond 30 min, on the theory that a large correction meant
# the user had replanned.  Measured over an 11 h session (2026-08-06/07) that gave
# 166 display changes, 39 in the worst hour, 14 direction reversals, and the cap
# saturated on 30% of steps — it was acting as a speed limiter pegged at its
# limit, not as a smoother.  Three things were wrong:
#
#   * 3 min/min is 1.5 min per 30 s poll, so the cap almost never bound on the
#     ±1-2 min jitter that produced most of the churn.
#   * There was no deadband, so every wiggle — including reversals — rendered.
#   * Snapping on magnitude caught the wrong events.  Both large jumps that
#     session (-68 min, +38 min) came from rate-learning samples, not from the
#     user: a single sample early in a 16 h heat-up moves the ETA by more than
#     30 min, so precisely the corrections most in need of smoothing bypassed it.
#
# Snapping is now driven by cause rather than size (see _replan_key), and the
# display is rounded because minute precision on an estimate hours out is
# spurious: it guarantees visible churn however the slew is tuned.

# Displayed schedule start (Heat Schedule sensor).  Same lesson, applied to the
# other sensor: hold the shown start steady against drift that carries no
# information.  See MSpaHeatScheduleSensor._slew_start for why cause rather than
# size does the separating.
_START_DRIFT_EARLIER_MIN = 10     # drift bringing the start forward
_START_DRIFT_LATER_MIN = 60       # drift pushing it back — see _slew_start
_START_TRACK_WITHIN_MIN = 45      # this close to starting, always show the live value


def _fmt_local(dt_utc: "datetime") -> str:
    """Format a UTC datetime as a local-time string, e.g. '14:30' or '14:30 +1d'."""
    local = dt_utc.astimezone()
    days  = (local.date() - datetime.now().date()).days
    return f"{local.strftime('%H:%M')}{f' +{days}d' if days > 0 else ''}"


def _sra_utc(coordinator) -> "datetime | None":
    """Return scheduled_ready_at as a UTC-aware datetime, or None."""
    sra = coordinator.scheduled_ready_at
    if sra is None:
        return None
    return sra.astimezone(timezone.utc) if sra.tzinfo else sra.replace(tzinfo=timezone.utc)


def _compute_ready_at(coordinator) -> "tuple[str, datetime | None]":
    """Return the Ready at state as (kind, dt).

    kind is one of:
      'ready' — the spa is at the temperature that matters (dt is None)
      'none'  — no meaningful display (dt is None)
      'sched' — dt is the scheduled ready time; shown verbatim.  It moves only
                when the user moves the schedule, so it is never slewed.
      'eta'   — dt is a live anchor-based estimate; the Ready at sensor slews
                this for display so corrections land as bounded ramps.

    Context determines which target drives the display:

      SCHEDULE context (sra_future, not triggered):
        - If spa is within 1°C of sched_temp  → ready (already there)
        - Otherwise                            → scheduled time
        near_target/latch are thermostat-relative and irrelevant here —
        the schedule may target a higher temperature than the current setpoint.

      SCHEDULED HEATING (triggered):
        - Anchor-based ETA to sched_temp (real-time, ignores original plan)

      FREE context (no schedule, not triggered):
        - near_target OR latched OR at_target  → ready
        - direction=cooling                    → none (no ETA for cooling)
        - direction=heating AND heater on      → anchor ETA to thermostat
        - otherwise                            → none
    """
    now_utc    = datetime.now(timezone.utc)
    latched    = coordinator.ready_latched
    near       = coordinator.near_target
    direction  = _spa_direction(coordinator)
    triggered  = coordinator._schedule_triggered
    sra        = _sra_utc(coordinator)
    sra_future = sra is not None and sra > now_utc
    heater_on  = coordinator._last_data.get("heater") == "on"
    sched_temp = coordinator.schedule_target_temp

    try:
        water = float(coordinator._last_data.get("water_temperature") or 0)
    except (TypeError, ValueError):
        water = None

    _LOGGER.debug(
        "ready_at eval: latched=%s near=%s direction=%s triggered=%s "
        "sra_future=%s heater=%s sched_temp=%s water=%s",
        latched, near, direction, triggered, sra_future, heater_on, sched_temp, water,
    )

    # ── SCHEDULE PENDING ──────────────────────────────────────────────────────
    # Schedule is the display driver when set and trigger not yet fired.
    # near_target is thermostat-based and may refer to a lower maintenance setpoint,
    # so we only use proximity to the schedule target here.
    if sra_future and not triggered:
        near_sched = (
            sched_temp is not None and water is not None
            and abs(water - sched_temp) <= 0.5
        )
        if near_sched:
            _LOGGER.debug(
                "ready_at → Ready (schedule_pending spa at sched_temp=%.1f water=%.1f)",
                sched_temp, water,
            )
            return ("ready", None)
        _LOGGER.debug("ready_at → %s (schedule_pending)", sra.isoformat())
        return ("sched", sra)

    # ── SCHEDULED HEATING ─────────────────────────────────────────────────────
    # Trigger fired — predict ETA to schedule target from current anchor.
    # Tracks real heating progress; independent of the original planned lead time.
    if triggered and sched_temp is not None:
        eta = _anchor_eta_utc(coordinator, sched_temp, now_utc)
        if eta is not None:
            remaining = (eta - now_utc).total_seconds()
            if remaining <= 300:
                _LOGGER.debug("ready_at → Ready (scheduled_heating ≤5min)")
                return ("ready", None)
            _LOGGER.debug(
                "ready_at → %s (scheduled_heating sched_temp=%.1f remaining=%.0fs)",
                eta.isoformat(), sched_temp, remaining,
            )
            return ("eta", eta)
        _LOGGER.debug("ready_at → None (scheduled_heating no anchor/rate)")
        return ("none", None)

    # ── FREE CONTEXT ──────────────────────────────────────────────────────────
    # No schedule pending or triggered.  Use thermostat-relative state.

    # DELIBERATE,  please read before "fixing":
    #
    # The latch keeps "Ready" showing after you turn the thermostat DOWN while
    # the water is still hot.  That is the after-a-soak case: you have used the
    # tub, dropped the setpoint to save energy, and the water is temperature is
    # still usable.  Ready at should
    # keep saying "Ready", because the tub genuinely is.
    #
    # The consequence is that the display depends on latch history: water above
    # setpoint reads "Ready" when the spa recently reached its setpoint, and
    # unknown when it did not (that path falls through to the cooling branch
    # below).  That asymmetry looks like a bug when you sit and play with the
    # thermostat, but the latched behaviour is as designed.
    # Reviewed and kept 2026-08-03; see tests/test_schedule_scenarios.py
    # TestLatchReleaseOnSetpointRaise for the boundaries.
    #
    # The latch does NOT last forever — coordinator.py releases it when:
    #   * the setpoint moves more than _NEW_SESSION_DELTA above the water
    #     (a real heating session is needed),
    #   * the water cools _LATCH_COOL_OFF below the warmest point reached while
    #     latched — the tub is no longer dip-warm whatever the thermostat says,
    #     which is what stops "Ready" outliving the heat it advertises,
    #   * a schedule expires, a schedule triggers, or a new schedule is set.

    # READY: coordinator confirms near target or spa is exactly at target
    if latched or near or direction == "at_target":
        reason = "latched" if latched else ("near_target" if near else "at_target")
        _LOGGER.debug("ready_at → Ready (free ctx: %s)", reason)
        return ("ready", None)

    # COOLING: spa above setpoint, no useful ETA
    if direction in ("cooling", None):
        _LOGGER.debug("ready_at → None (free ctx: direction=%s)", direction)
        return ("none", None)

    # FREE HEATING: heater on, predict ETA to thermostat setpoint
    if heater_on:
        try:
            thermostat = float(coordinator._last_data.get("target_temperature") or 0)
        except (TypeError, ValueError):
            thermostat = None
        if thermostat is not None:
            eta = _anchor_eta_utc(coordinator, thermostat, now_utc)
            if eta is not None:
                remaining = (eta - now_utc).total_seconds()
                if remaining <= 300:
                    _LOGGER.debug("ready_at → Ready (free_heating ≤5min)")
                    return ("ready", None)
                _LOGGER.debug(
                    "ready_at → %s (free_heating thermostat=%.1f remaining=%.0fs)",
                    eta.isoformat(), thermostat, remaining,
                )
                return ("eta", eta)
        _LOGGER.debug("ready_at → None (free_heating no anchor/rate/thermostat)")
        return ("none", None)

    _LOGGER.debug("ready_at → None (free ctx: heater_off no applicable state)")
    return ("none", None)


def _compute_ready_at_value(coordinator) -> "str | None":
    """Return the Ready at display value (unsmoothed).

    Formatting wrapper over _compute_ready_at.  Used directly by the Heat
    Schedule sensor for the shared readiness definition; the Ready at sensor
    itself goes through _compute_ready_at so it can slew the live ETA.
    """
    kind, dt = _compute_ready_at(coordinator)
    if kind == "ready":
        return "Ready"
    if dt is not None:
        return _fmt_local(dt)
    return None


def _rate_diagnostics(coordinator, water_f, span_target) -> str:
    """Compact rate diagnostics for log lines.

    eff     — the segmented effective rate actually driving the estimate
              (buckets + corrections + bias over the span to the display target)
    ema     — the flat EMA, for contrast (it drives nothing)
    buckets — cold/mid/hot rates; '*' marks buckets observed this session
              (used verbatim, superseding scalar and ambient corrections)
    scalar / amb / bias — the corrections in force

    Designed so a diagnostics log alone answers rate questions without
    needing the sensor attributes.
    """
    eff = _segmented_effective_rate(coordinator, water_f, span_target)
    ema = _effective_heat_rate(coordinator)
    buckets = getattr(coordinator, "heat_rate_buckets", [None, None, None])
    fresh = getattr(coordinator, "_session_fresh_buckets", set()) or set()
    b_str = "/".join(
        (f"{b:.2f}" + ("*" if i in fresh else "")) if b is not None else "-"
        for i, b in enumerate(buckets)
    )
    scalar = getattr(coordinator, "_session_scalar", 1.0)
    bias = getattr(coordinator, "prediction_bias", 1.0)
    if water_f is not None:
        idx = 0 if water_f < _HEAT_BUCKET_T1 else 1 if water_f < _HEAT_BUCKET_T2 else 2
        amb = ambient_rate_factor(
            idx,
            getattr(coordinator, "ambient_temp", None),
            getattr(coordinator, "ambient_baseline", None),
        )
        amb_str = f"{amb:.2f}"
    else:
        amb_str = "n/a"
    eff_str = f"{eff:.2f}°C/h" if eff is not None else "n/a"
    ema_str = f"{ema:.2f}" if ema is not None else "n/a"
    return (f"eff={eff_str} ema={ema_str} buckets={b_str} "
            f"scalar={scalar:.2f} amb={amb_str} bias={bias:.2f}")


def _log_readiness_change(coordinator, old_state: object, new_state: "str | None") -> None:
    """Emit one INFO line when the Ready at sensor value transitions."""
    data = coordinator._last_data
    try:
        water_f = float(data.get("water_temperature"))
        water = f"{water_f:.1f}"
    except (TypeError, ValueError):
        water_f = None
        water = "?"
    try:
        setpoint = f"{float(data.get('target_temperature')):.1f}"
    except (TypeError, ValueError):
        setpoint = "?"

    sched_at = getattr(coordinator, "scheduled_ready_at", None)
    sched_temp = getattr(coordinator, "schedule_target_temp", None)
    if sched_at is not None and sched_temp is not None:
        sched_ctx = f"sched={sched_temp:.1f}°C@{sched_at.strftime('%H:%M')}"
    elif sched_temp is not None:
        sched_ctx = f"sched={sched_temp:.1f}°C"
    else:
        sched_ctx = "no-sched"

    heater = data.get("heater", "?")
    latched = getattr(coordinator, "ready_latched", False)
    near = getattr(coordinator, "near_target", False)
    triggered = getattr(coordinator, "_schedule_triggered", False)
    old_label = "BOOT" if old_state is _UNSET else repr(old_state)

    _LOGGER.info(
        "Ready at: %s → %s  [water=%s  sp=%s  %s  heater=%s  %s  "
        "latched=%s  near=%s  triggered=%s]",
        old_label, repr(new_state),
        water, setpoint, sched_ctx, heater,
        _rate_diagnostics(coordinator, water_f, _relevant_target(coordinator)),
        latched, near, triggered,
    )


def _temperature_basis(coordinator) -> "tuple[bool, str]":
    """Whether the reported water temperature describes the tub, and what it describes.

    The probe sits in the external pump housing rather than in the tub.  While the
    circulation pump runs, tub water passes over it and the reading is the tub
    temperature.  With the pump off it measures a small, separate, stagnant volume
    which loses heat faster than the insulated tub, so it reads at or below the
    real tub temperature — and by an amount that depends on how long it has been
    standing and on the weather, so it cannot be calibrated out.

    An estimate built on a reading that runs cold asks for more heating than is
    needed and therefore starts early, which is the harmless direction.  So this
    is reported, not corrected: no modelling recovers a temperature the hardware
    is not measuring, and a correction would only trade a safe error for a
    confident wrong one.

    Deliberately *not* surfaced by making the sensor unavailable.  The estimate is
    still useful and still errs safe, and dropping the state would break history
    and any automation reading it — an attribute says "treat this as a lower
    bound" without taking the value away.
    """
    circulating = coordinator._last_data.get("filter") == "on"
    return circulating, ("tub water" if circulating
                         else "stagnant water in the external pump housing")


def _day_offset_suffix(dt_utc) -> str:
    """' +Nd' when dt_utc falls N local days after today, else ''."""
    local = dt_util.as_local(dt_utc)
    days = (local.date() - dt_util.now().date()).days
    return f" +{days}d" if days > 0 else ""


def _compute_schedule_value(result) -> str:
    """Return the Heat Schedule display value from a _schedule_data() result."""
    if result is None:
        return "Not scheduled"
    if result == "ready":
        return "Ready"
    if result == "triggered":
        return "Heating"
    if isinstance(result, tuple) and len(result) == 2:
        # Schedule set, but too far out to compute a meaningful start time.
        _, target_utc = result
        return f"Scheduled{_day_offset_suffix(target_utc)}"
    _, _, start_at_utc = result
    now_utc = dt_util.utcnow()
    if now_utc >= start_at_utc:
        return "Start now"
    local_start = dt_util.as_local(start_at_utc)
    return f"Start at {local_start.strftime('%H:%M')}{_day_offset_suffix(start_at_utc)}"


def _log_schedule_change(
    coordinator, old_state: object, new_state: str, schedule_data
) -> None:
    """Emit one INFO line when the Heat Schedule sensor value transitions."""
    data = coordinator._last_data
    try:
        water_f = float(data.get("water_temperature"))
        water = f"{water_f:.1f}"
    except (TypeError, ValueError):
        water_f = None
        water = "?"

    sched_at = getattr(coordinator, "scheduled_ready_at", None)
    sched_temp = getattr(coordinator, "schedule_target_temp", None)
    if sched_at is not None and sched_temp is not None:
        sched_ctx = f"sched={sched_temp:.1f}°C@{sched_at.strftime('%H:%M')}"
    elif sched_temp is not None:
        sched_ctx = f"sched={sched_temp:.1f}°C"
    else:
        sched_ctx = "no-sched"

    rate_str = _rate_diagnostics(coordinator, water_f, sched_temp)
    triggered = getattr(coordinator, "_schedule_triggered", False)

    if isinstance(schedule_data, tuple) and len(schedule_data) == 3:
        _, _, start_at_utc = schedule_data
        try:
            start_local = start_at_utc.astimezone()
            extra = f"start={start_local.strftime('%H:%M')}"
        except Exception:
            extra = "start=?"
    elif isinstance(schedule_data, tuple) and len(schedule_data) == 2:
        extra = "start=beyond-horizon"
    else:
        extra = f"data={schedule_data!r}"

    old_label = "BOOT" if old_state is _UNSET else repr(old_state)
    _LOGGER.info(
        "Heat Schedule: %s → %s  [water=%s  %s  %s  %s  triggered=%s]",
        old_label, repr(new_state),
        water, sched_ctx, extra, rate_str, triggered,
    )


class MSpaReadinessSensor(MSpaSensorEntity):
    """Human-readable spa readiness sensor.

    State is 'Ready' when at or within 5 minutes of target temperature,
    otherwise the expected ready-at time in local time (e.g. '19:45' or
    '19:45 +1d').  Icon reflects direction: fire=heating, snowflake=cooling,
    hot-tub=ready.  No device_class or state_class — plain text sensor.
    """

    name = "Ready at"
    _attr_icon = "mdi:hot-tub"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_readiness_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_device_info = self.device_info
        self._eta_display: "datetime | None" = None   # slewed ETA shown to the user
        self._eta_wall: "datetime | None" = None      # wall clock of last slew step
        self._eta_plan_key = None                     # plan identity; a change snaps
        self._eta_closing = False                     # mid-correction hysteresis latch

    @property
    def available(self):
        if not super().available:
            return False
        # Available whenever we have enough data to determine a state.
        return _spa_direction(self.coordinator) is not None or self.coordinator.ready_latched

    def _replan_key(self):
        """Identity of the plan being estimated.

        A change here means the user moved something — the schedule time, the
        schedule target, or the thermostat — and the ETA should jump to the new
        answer rather than crawl to it.  Everything else, however large, is the
        model revising its own estimate and gets slewed.
        """
        c = self.coordinator
        try:
            setpoint = float(c._last_data.get("target_temperature"))
        except (TypeError, ValueError):
            setpoint = None
        return (c.scheduled_ready_at, c.schedule_target_temp, setpoint)

    def _slew_eta(self, raw_eta, now_utc=None):
        """Move the displayed ETA toward raw_eta, smoothly and coarsely.

        Three mechanisms, in order:
          * replan snap — if the plan itself changed (_replan_key), adopt the new
            estimate immediately; the old one is answering a different question.
          * deadband with hysteresis — a gap under _ETA_DEADBAND_MIN never *starts*
            movement, so jitter and small reversals are ignored; but once moving,
            the gap is closed completely rather than stalling at the deadband
            edge, which would otherwise leave a permanent lag of up to
            _ETA_DEADBAND_MIN behind the real estimate.
          * rate cap — otherwise close the gap at no more than
            _ETA_SLEW_MIN_PER_MIN per wall-clock minute.

        The returned value is rounded to _ETA_ROUND_MIN for display.  The
        unrounded position is kept internally so repeated small steps still
        accumulate instead of being rounded away each time.
        """
        now = now_utc or datetime.now(timezone.utc)
        key = self._replan_key()
        replanned = (self._eta_plan_key is not None and key != self._eta_plan_key)

        if self._eta_display is None or self._eta_wall is None or replanned:
            if replanned:
                _LOGGER.debug("ETA slew: plan changed, snapping to %s", raw_eta.isoformat())
            self._eta_display = raw_eta
            self._eta_closing = False
        else:
            delta_min = (raw_eta - self._eta_display).total_seconds() / 60.0
            if abs(delta_min) >= _ETA_DEADBAND_MIN:
                self._eta_closing = True
            elif abs(delta_min) < 0.5:
                self._eta_closing = False
            if self._eta_closing:
                dt_min = max((now - self._eta_wall).total_seconds() / 60.0, 0.0)
                cap = _ETA_SLEW_MIN_PER_MIN * dt_min
                step = min(max(delta_min, -cap), cap)
                self._eta_display = self._eta_display + timedelta(minutes=step)

        self._eta_wall = now
        self._eta_plan_key = key
        return self._round_eta(self._eta_display)

    @staticmethod
    def _round_eta(dt):
        """Round to the nearest _ETA_ROUND_MIN for display."""
        q = _ETA_ROUND_MIN * 60
        secs = dt.hour * 3600 + dt.minute * 60 + dt.second
        shift = round(secs / q) * q - secs
        return (dt + timedelta(seconds=shift)).replace(second=0, microsecond=0)

    @property
    def native_value(self):
        kind, dt = _compute_ready_at(self.coordinator)
        if kind == "eta" and dt is not None:
            val = _fmt_local(self._slew_eta(dt))
        else:
            # Ready / none / scheduled time: exact displays — no slewing, and
            # the slew state resets so the next ETA regime starts fresh.
            self._eta_display = None
            self._eta_wall = None
            self._eta_plan_key = None
            self._eta_closing = False
            val = ("Ready" if kind == "ready"
                   else _fmt_local(dt) if dt is not None else None)
        prev = getattr(self, "_logged_state", _UNSET)
        if val != prev:
            _log_readiness_change(self.coordinator, prev, val)
            self._logged_state = val
        return val

    @property
    def icon(self):
        return "mdi:hot-tub"

    @property
    def extra_state_attributes(self):
        direction = _spa_direction(self.coordinator)
        latched = self.coordinator.ready_latched
        cooling = direction == "cooling"
        mins = _minutes_to_target(self.coordinator)
        # Keep the attributes consistent with the slewed state display: when a
        # smoothed ETA is being shown, minutes_remaining and ready_at derive
        # from it rather than from the raw anchor estimate.
        smoothed = (self._round_eta(self._eta_display)
                    if self._eta_display is not None else None)
        if smoothed is not None and not latched and not cooling:
            mins = max(0, round(
                (smoothed - datetime.now(timezone.utc)).total_seconds() / 60
            ))
        rounded = (round(mins / 5) * 5) if (mins is not None and mins > 5 and not cooling) else (None if cooling else mins)
        if latched:
            color = "green"
        elif direction == "heating":
            color = "red"
        elif cooling:
            color = "light-blue"
        else:
            color = "green"
        data = self.coordinator._last_data
        try:
            water_temp = float(data.get("water_temperature"))
            target_temp = float(data.get("target_temperature"))
        except (TypeError, ValueError):
            water_temp = target_temp = None

        # Single source of truth: the attribute has to agree with the state.
        # This used to re-derive the time independently, which disagreed with the
        # display whenever a schedule was driving it — with a schedule pending and
        # the spa cooling toward its maintenance setpoint the state showed
        # "13:00 +1d" while ready_at reported nothing, because the old branch bailed
        # out on `cooling`.  Deriving both from _compute_ready_at() removes the
        # second opinion.
        kind, kind_dt = _compute_ready_at(self.coordinator)
        if kind == "sched":
            # Scheduled times are shown verbatim and never slewed.
            ready_at_utc = kind_dt
        elif kind == "eta":
            # Match the slewed display rather than the raw anchor estimate.
            ready_at_utc = smoothed if smoothed is not None else kind_dt
        else:
            ready_at_utc = None     # 'ready' / 'none' — there is no time to report
        ready_at_ts = ready_at_utc.isoformat() if ready_at_utc is not None else None

        # The rate actually in effect for the current estimate: segmented over
        # the span to the display-driving target, with all corrections and the
        # bias — NOT the flat EMA (which is exposed separately below).
        rel_target = _relevant_target(self.coordinator)
        if (water_temp is not None and rel_target is not None
                and rel_target > water_temp):
            effective = _segmented_effective_rate(self.coordinator, water_temp, rel_target)
        elif water_temp is not None and target_temp is not None:
            effective = _effective_rate(self.coordinator, water_temp, target_temp)
        else:
            effective = None
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

        # Ambient (weather-model) correction visibility.  ambient_factor is the
        # multiplier currently applied to the bucket the water is in, so a value
        # < 1.0 means colder-than-baseline air is slowing the estimate.
        ambient_temp = getattr(self.coordinator, "ambient_temp", None)
        ambient_baseline = getattr(self.coordinator, "ambient_baseline", None)
        if water_temp is not None:
            amb_idx = 0 if water_temp < _HEAT_BUCKET_T1 else 1 if water_temp < _HEAT_BUCKET_T2 else 2
            ambient_factor = round(ambient_rate_factor(amb_idx, ambient_temp, ambient_baseline), 3)
        else:
            ambient_factor = None

        circulating, temperature_basis = _temperature_basis(self.coordinator)

        # How the session is running against its own opening plan, in minutes: positive
        # is behind, negative ahead.  Deliberately separate from the ETA rather than
        # folded into it — the estimate answers "when", this answers "how is it going",
        # and mixing them is what made the old ETA chase every sample.
        anchor = getattr(self.coordinator, "temp_anchor_temp", None)
        deviation = self.coordinator.session_progress_deviation(anchor)
        settled = self.coordinator.session_settled(anchor)

        return {
            "direction": direction,
            "minutes_remaining": rounded,
            "color": color,
            # Null until the session settles: the opening crossings measure band
            # position rather than heating, so a deviation computed then is meaningless.
            "progress_deviation": deviation,
            "plan_settled": settled,
            # Whether the temperature this estimate is built on is the tub's at all.
            # False means the probe is sitting in stagnant water in the pump
            # housing, which reads at or below tub temperature — so the estimate
            # is a safe over-estimate of the work remaining, not a wrong one.
            "circulating": circulating,
            "temperature_basis": temperature_basis,
            # ISO 8601 UTC, or None when the state is "Ready"/unknown.  Templates:
            #   {{ state_attr('sensor..._ready_at','ready_at') | as_datetime | as_local }}
            "ready_at": ready_at_ts,
            # What that timestamp means, since it has two sources: 'sched' is the
            # time you asked for, 'eta' is the live prediction.  'ready'/'none'
            # accompany a null ready_at.
            "ready_at_kind": kind,
            "effective_rate_deg_per_hour": round(effective, 3) if effective is not None else None,
            "computed_heat_rate_deg_per_hour": round(computed_heat, 3) if computed_heat is not None else None,
            "computed_cool_rate_deg_per_hour": round(computed_cool, 3) if computed_cool is not None else None,
            "heat_rate_cold_deg_per_hour": round(buckets[0], 3) if buckets[0] is not None else None,
            "heat_rate_mid_deg_per_hour": round(buckets[1], 3) if buckets[1] is not None else None,
            "heat_rate_hot_deg_per_hour": round(buckets[2], 3) if buckets[2] is not None else None,
            "session_condition_scalar": round(session_scalar, 3),
            "prediction_bias": round(prediction_bias, 3),
            "ambient_temp_deg_c": round(ambient_temp, 1) if ambient_temp is not None else None,
            "ambient_baseline_deg_c": round(ambient_baseline, 2) if ambient_baseline is not None else None,
            "ambient_factor": ambient_factor,
            "device_rate_deg_per_hour": device_rate,
            "current_temperature": water_temp,
            "target_temperature": target_temp,
        }


class MSpaHeatScheduleSensor(MSpaSensorEntity):
    """input_datetime-driven spa conditioning schedule sensor.

    Reads the target ready time from a configured input_datetime helper and
    uses the integration's learned rates to compute when conditioning must
    start to reach the target temperature by that time.  Works for both
    heating (target > current) and cooling (target < current).

    State: "Not scheduled" / "Ready" / "Start now" / "Start at HH:MM [+Nd]"
    Attributes: target_time, start_at (ISO 8601), target_temperature

    The state's "HH:MM" is held steady by _slew_start; the `start_at` attribute is
    always the live plan, because an automation acting on it needs the real time
    rather than a stable one.  They can therefore differ by up to
    _START_DRIFT_DEADBAND_MIN while the start is still far off.
    """

    name = "Heat Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        device_id = getattr(coordinator, "device_id", "unknown")
        self._attr_unique_id = f"mspa_heat_schedule_{device_id}"
        self._attr_device_info = self.device_info
        self._start_shown = None      # displayed start, held against ambient drift
        self._start_key = None        # plan + reading it was computed from

    def _plan_key(self):
        """Plan identity, plus the reading that drives real movement in the start.

        The water temperature belongs in the key because a change of reading is
        exactly what makes the start time move for a reason: it is a 0.5 °C band
        crossing, worth a band's heating either way.  Everything else that moves
        the estimate — chiefly the ambient factor rescaling it — leaves the
        reading alone, and is drift.
        """
        c = self.coordinator
        try:
            water = float(c._last_data.get("water_temperature"))
        except (TypeError, ValueError):
            water = None
        return (c.scheduled_ready_at, c.schedule_target_temp, water)

    def _slew_start(self, raw_start, now_utc):
        """Hold the displayed start steady against drift that carries no information.

        The start is recomputed every poll, and it moves in two distinct ways:

          * a lump of about a band's worth of heating each time the water reading
            crosses a 0.5 °C boundary — always earlier while cooling, and real:
            the plan genuinely does need to start sooner.
          * a smaller drift as the ambient factor rescales the whole estimate.
            This one grows with lead time, because it is a percentage of a long
            span: a 1.5 °C outdoor rise moved a 9 h estimate by 28 min on
            2026-08-12, and reversed 2 min later when the water crossed a band.

        Size cannot separate them — at the cold bucket rate a crossing is worth
        about 15 min and at the hot bucket about 40, which straddles the drift —
        so this discriminates on cause, as _slew_eta does. A change of reading is
        adopted at once; drift within one reading has to clear a wide deadband.

        The deadband is asymmetric, because during a cool-down drift in the two
        directions means different things. The water is cooling, so the start is
        marching earlier and every crossing moves it earlier again. Drift that
        pushes the start *back* is therefore running against the plan's own trend
        and the next crossing will more than cancel it: a band is worth 27-44 min
        of heating across the bucket rates, so any later-drift smaller than that
        is transient by construction. On 2026-08-12 holding at 15:45 through a
        +40 min ambient drift was closer to the eventual 15:52 than adopting the
        16:25 it briefly computed. So later-drift has to clear a full band's worth
        before it is believed; earlier-drift, which is where the plan is heading
        anyway, is adopted sooner.

        Once the start is within _START_TRACK_WITHIN_MIN the live value is shown
        regardless, so the displayed time is exact when it is close enough to act
        on, and only approximate hours out where the churn was.

        Deliberately no rate cap and no wall-clock term: unlike the ETA there is
        nothing here to ramp smoothly toward, and staying a pure function of
        (raw, held, now) keeps it idempotent — `native_value` and
        `extra_state_attributes` both evaluate the schedule on the same update.
        """
        key = self._plan_key()
        shown = self._start_shown

        if shown is None or key != self._start_key:
            shown = raw_start                     # new plan, or the reading moved
        elif raw_start - now_utc <= timedelta(minutes=_START_TRACK_WITHIN_MIN):
            shown = raw_start                     # close enough to act on
        else:
            drift_min = (raw_start - shown).total_seconds() / 60.0
            limit = (_START_DRIFT_LATER_MIN if drift_min > 0
                     else _START_DRIFT_EARLIER_MIN)
            if abs(drift_min) >= limit:
                shown = raw_start                 # too large to keep hiding

        self._start_shown = shown
        self._start_key = key
        return shown

    def _schedule_data(self):
        """Return (target_dt_utc, target_temp, start_at_utc) or a sentinel string."""
        target_dt = self.coordinator.scheduled_ready_at
        if target_dt is None:
            return None

        target_utc = dt_util.as_utc(target_dt)
        now_utc = dt_util.utcnow()

        if target_utc <= now_utc:
            return None

        target_temp = float(self.coordinator.schedule_target_temp)

        # The same estimate the trigger plans from, so the displayed start and the
        # moment it actually fires cannot disagree — the defect c5010d3 fixed for
        # Ready at, kept fixed here.  Degrades to the reported reading on its own.
        current_temp = self.coordinator.scheduling_temp()
        if current_temp is None:
            return None

        # Single source of truth: the Heat Schedule is "ready" exactly when the
        # Ready at sensor is — same code path, so the two always converge (during
        # heating this uses Ready-at's tight ETA<=5min rule, not a loose band).
        # Checked before the lookahead horizon so the two sensors cannot disagree
        # about readiness just because the session is a long way off.
        if _compute_ready_at_value(self.coordinator) == "Ready":
            return "ready"

        # Once the trigger has fired the heater is on and working toward the
        # schedule target.  Return a stable sentinel so the sensor shows
        # "Heating" rather than oscillating between "Start now" / "Start at
        # HH:MM" as the temperature fluctuates ±0.5°C during thermostat cycling.
        if self.coordinator._schedule_triggered:
            return "triggered"

        days_until = (target_utc - now_utc).total_seconds() / 86400
        options = self._config_entry.options
        lookahead = int(options.get(CONF_SCHEDULE_LOOKAHEAD_DAYS, DEFAULT_SCHEDULE_LOOKAHEAD_DAYS))
        if days_until > lookahead:
            # Beyond the horizon, deliberately do not project a start time: the
            # water temperature and the learned rates will both have moved by
            # then.  But still confirm that a schedule *exists* — reporting
            # "Not scheduled" here is indistinguishable from having forgotten to
            # set one, which is the opposite situation.
            return ("beyond", target_utc)

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
        # Render from the held start, not the live one.  Inside the tracking window
        # _slew_start returns the live value anyway, so "Start now" still fires at
        # the moment the coordinator's own recompute triggers the heater.
        if isinstance(result, tuple) and len(result) == 3:
            target_utc, target_temp, raw_start = result
            result = (target_utc, target_temp,
                      self._slew_start(raw_start, dt_util.utcnow()))
        val = _compute_schedule_value(result)
        prev = getattr(self, "_logged_state", _UNSET)
        if val != prev:
            _log_schedule_change(self.coordinator, prev, val, result)
            self._logged_state = val
        return val

    @property
    def extra_state_attributes(self):
        result = self._schedule_data()
        if not isinstance(result, tuple):
            return {}
        circulating, temperature_basis = _temperature_basis(self.coordinator)
        if len(result) == 2:
            # Beyond the lookahead horizon — the target is known, the start is not.
            _, target_utc = result
            return {
                "target_time": target_utc.isoformat(),
                "start_at": None,
                "target_temperature": getattr(
                    self.coordinator, "schedule_target_temp", None
                ),
                "circulating": circulating,
                "temperature_basis": temperature_basis,
            }
        target_utc, target_temp, start_at_utc = result
        return {
            "target_time": target_utc.isoformat(),
            "start_at": start_at_utc.isoformat(),
            "target_temperature": target_temp,
            # The planned start is computed from the water temperature, so it
            # inherits whatever that reading describes.  With the pump off it is
            # built on stagnant housing water and plans to start early.
            "circulating": circulating,
            "temperature_basis": temperature_basis,
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
