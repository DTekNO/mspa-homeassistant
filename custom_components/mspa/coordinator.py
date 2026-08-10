"""DataUpdateCoordinator for MSpa integration."""
import logging
from datetime import timedelta, datetime, timezone
from .mspa_api import MSpaApiClient

from typing import Any, Dict
import asyncio

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.storage import Store
from homeassistant.const import UnitOfTemperature
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    IDLE_SCAN_INTERVAL,
    ACTIVE_SCAN_INTERVAL,
    RAPID_SCAN_INTERVAL,
    RAPID_POLL_TIMEOUT,
    EXTERNAL_CHANGE_INTERVAL,
    EXTERNAL_CHANGE_TIMEOUT,
    IDLE_STABLE_THRESHOLD,
    CONF_TRACK_TEMPERATURE_UNIT,
    CONF_RESTORE_STATE,
    CONF_ALWAYS_ENFORCE_UNIT,
    CONF_WEATHER_ENTITY,
    CONF_SCHEDULE_TARGET_TEMP,
    DEFAULT_SCHEDULE_TARGET_TEMP,
    AMBIENT_BASELINE_ALPHA,
    AMBIENT_BASELINE_DEFAULT,
    ambient_rate_factor,
    BIAS_EMA_ALPHA,
    BIAS_CLAMP_MIN,
    BIAS_CLAMP_MAX,
    BIAS_MIN_DELTA_C,
    BIAS_RATIO_MIN,
    BIAS_RATIO_MAX,
)

import time
from homeassistant.const import ATTR_STATE, ATTR_TEMPERATURE


_LOGGER = logging.getLogger(__name__)

# Rate-sampling constants (heating and cooling trackers).
# Bounds are intentionally generous: even a tiny spa at max power can't exceed
# ~5 °C/h, and below the minimum the signal is just sensor noise / drift.
_MIN_RATE_SAMPLE_HOURS = 3 / 60   # 3 minutes minimum between samples

# A scheduled ready time older than this is treated as abandoned rather than as a
# window that has just opened, so it is cleared without commanding the heater.
# Generous enough not to disturb the deliberate "fire as soon as the window opens"
# behaviour, which tests pin at 5 and 10 minutes late.
_SCHEDULE_STALE_AFTER = timedelta(hours=1)

# Soft start: the circulation pump must be running before the heater is commanded.
# The spa refuses to heat without flow, and the MSpa Link app never issues a bare
# heater-on — it starts the pump first.  Our climate entity, the heater switch, the
# set_heater service and the scheduler all could, so the ordering is enforced in
# set_feature_state, the one point they all pass through.
#
# The command ack confirms the cloud accepted it, not that water is moving, so a
# short settle follows.  The API throttle already spaces consecutive commands by
# 0.4 s; this makes the margin explicit and tunable.  Set to 0 to rely on the ack
# alone.
_PUMP_SETTLE_SECONDS = 1.5


# Temperature-bucket boundaries, mirroring sensor._HEAT_BUCKET_T1/_T2.  Defined
# here too so the coordinator does not import from the sensor platform.
_HEAT_BUCKET_T1 = 30.0
_HEAT_BUCKET_T2 = 37.0


def _heat_bucket_index(temp: float) -> int:
    """Bucket index for a water temperature: 0 cold, 1 mid, 2 near-setpoint."""
    return 0 if temp < _HEAT_BUCKET_T1 else 1 if temp < _HEAT_BUCKET_T2 else 2
_MIN_HEAT_RATE = 0.05             # °C/h — below this is noise / flat
_MAX_HEAT_RATE = 3.0              # °C/h — above this is an outlier
_EMA_ALPHA = 0.25                 # smoothing factor (lower = slower to adapt)

# A gap this large between water and setpoint defines a genuine heating
# session, as opposed to thermostat cycling near the setpoint (±0.5–1 °C).
# Used to start prediction tracking, to reset the session scalar, and to
# release the readiness latch when the user raises the setpoint.
_NEW_SESSION_DELTA = 2.0          # °C

# How far the water may cool below the temperature at which it latched before
# "Ready" is withdrawn.  The latch advertises "still warm enough to use
# without waiting"; once the water has given up this much heat that claim is
# no longer credible, however the thermostat happens to be set.  Larger than
# _NEW_SESSION_DELTA so a lowered thermostat followed by ordinary cycling
# doesn't withdraw Ready prematurely.
_LATCH_COOL_OFF = 3.0             # °C

# Full-heat mode: the device reports heat_state 3 while actually heating
# (0 = off, 2 = preheat).  Rate sampling and prediction tracking both key off
# this so they start and stop on the same signal.
_HEAT_STATE_FULL = 3

# Maps coordinator _pending_changes keys (transformed names) to their raw API
# command dict keys.  Used to prune _pending_raw_command incrementally as each
# pending change is confirmed, so that a retry payload never re-sends fields
# that were already acknowledged by the spa.
_PENDING_TO_RAW_KEY: dict[str, str] = {
    "heater": "heater_state",
    "filter": "filter_state",
    "jet": "jet_state",
    "ozone": "ozone_state",
    "uvc": "uvc_state",
    "bubble": "bubble_state",
    "bubble_level": "bubble_level",
    "target_temperature": "temperature_setting",
    "temperature_unit": "temperature_unit",
}

# Status payload keys that are explicitly restructured/renamed in transformed_data.
# All other keys from the payload are passed through verbatim.
_STRUCTURED_STATUS_KEYS = frozenset({
    "water_temperature", "temperature_setting",
    "heater_state", "filter_state", "bubble_state",
    "jet_state", "ozone_state", "uvc_state",
    "bubble_level", "fault",
})

def _read_weather_entity(hass: HomeAssistant, entity_id: str | None) -> tuple[float | None, float | None]:
    """Read temperature (°C) and wind speed (m/s) from a HA weather entity.

    Returns (temp_celsius, wind_m_s).  Either value is None when unavailable.
    Normalises temperature and wind speed to SI units regardless of the HA
    unit system, so the Gaussian kernel uses consistent scales.
    """
    if not entity_id:
        return None, None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None, None

    attrs = state.attributes

    # --- Temperature → °C ---
    temp = attrs.get("temperature")
    if temp is not None:
        try:
            temp = float(temp)
            unit = attrs.get("temperature_unit", "°C")
            if unit in ("°F", "F"):
                temp = (temp - 32) * 5 / 9
        except (ValueError, TypeError):
            temp = None

    # --- Wind speed → m/s ---
    # Prefer wind_gust_speed over wind_speed: gusts disrupt the thermal
    # boundary layer around the cover more aggressively than steady wind,
    # making them the dominant driver of convective heat loss.
    # Falls back to wind_speed when gust data isn't provided by the integration.
    raw_wind = attrs.get("wind_gust_speed") or attrs.get("wind_speed")
    wind = None
    if raw_wind is not None:
        try:
            wind = float(raw_wind)
            unit = attrs.get("wind_speed_unit", "m/s")
            if unit in ("km/h", "kph"):
                wind = wind / 3.6
            elif unit in ("mph",):
                wind = wind * 0.44704
            elif unit in ("kn", "knot", "knots"):
                wind = wind * 0.514444
            # "m/s" needs no conversion
        except (ValueError, TypeError):
            wind = None

    return temp, wind


class MSpaUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from MSpa Hot Tub."""

    def __init__(self, hass: HomeAssistant, config_entry: Dict[str, Any]) -> None:
        """Initialize."""
        # Obfuscate sensitive data for logging
        def obfuscate_value(key, value):
            if key == "password":
                return "***"
            elif key == "account_email" and value:
                parts = value.split("@")
                if len(parts) == 2 and parts[0]:
                    return f"{parts[0][:3]}***@{parts[1]}"
                return "***"
            return value

        safe_data = {k: obfuscate_value(k, v) for k, v in config_entry.data.items()}
        _LOGGER.debug(f"MSpaUpdateCoordinator initializing {safe_data}")
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.config = config_entry.data
        self.account_email = self.config["account_email"]
        self.password = self.config["password"]  # Already MD5 hashed
        self.region = self.config.get("region", "ROW")  # Default to ROW for safety

        self._last_data = {}
        self.api = MSpaApiClient(
            hass=hass,
            account_email=self.account_email,
            password=self.password,
            coordinator=self,
            region=self.region,
            device_id=self.config.get("device_id"),
        )
        self._rapid_poll_until = None  # Timestamp when to stop rapid polling
        self._external_change_until = None  # Timestamp when to stop external-change polling
        self._pending_changes = {}  # Track expected changes (transformed keys)
        self._pending_raw_command = {}  # Raw API payload for retrying unconfirmed commands
        self._command_retry_count = 0  # Number of retries attempted for current command
        self._last_heat_state = None  # Track heat state changes
        self._last_is_online = None  # Track power on/off transitions
        self._saved_state = {}  # Store state before power off for restoration
        self._last_snapshot = {}  # Store last known state for change detection
        self._last_state_change_time: float = 0.0  # monotonic time of last detected state change
        self._power_cycle_detected = False  # Flag to track if we just detected a power cycle

        # Computed heating rate tracking (°C / hour, exponential moving average).
        # Updated on every poll where the heater is in full-heat mode (heat_state == 3)
        # and at least _MIN_RATE_SAMPLE_HOURS have elapsed since the last valid sample.
        # Outliers (caused by adding hot/cold water, sensor noise etc.) are rejected.
        self.computed_heat_rate: float | None = None  # °C/h, None until enough data
        self._rate_last_temp: float | None = None     # °C at the window anchor
        self._rate_prev_temp: float | None = None     # °C at the previous poll
        self._rate_last_time: float | None = None     # monotonic seconds at last sample
        # True while the rate anchor is phase-uncertain: the temperature reports
        # in 0.5 °C bands, so at heater-on the water sits at an unknown position
        # inside its band.  The FIRST crossing after heater-on measures that
        # random phase, not the heating rate (it reads ~2x fast on average), so
        # it re-anchors only; learning starts from the second crossing.
        self._rate_first_step: bool = False

        # Per-temperature-bucket heating EMAs for improved long-range accuracy.
        # Bucket 0: T < 30°C  (cold water, minimal thermal losses — fastest)
        # Bucket 1: 30 ≤ T < 37°C
        # Bucket 2: T ≥ 37°C  (near setpoint, highest losses — slowest)
        # Each bucket is updated only by observations made in that temperature
        # range, so predictions integrate the actual observed slow-down curve.
        self.heat_rate_buckets: list[float | None] = [None, None, None]

        # Session-level ambient condition scalar.
        # Reset to 1.0 at the start of each genuine heating session (delta > 2°C).
        # Updated from the *first bucket* to receive observations in each session
        # (whichever bucket that is — not hardcoded to bucket-0), comparing
        # observed rate vs the stored base rate for that bucket.  Applied to
        # bucket predictions for segments not yet observed this session so that
        # a cold/warm day is reflected immediately across the whole estimate.
        self._session_scalar: float = 1.0
        self._session_scalar_bucket: int | None = None  # which bucket drives the scalar this session
        self._session_fresh_buckets: set[int] = set()   # buckets that received data this session
        self._heat_was_active: bool = False  # previous-poll heater state

        # Computed passive cooling rate (°C/h, stored positive).
        # Sampled when the heater is off and water temperature is actually dropping.
        self.computed_cool_rate: float | None = None  # °C/h, None until enough data
        self._cool_last_temp: float | None = None     # °C at last accepted sample
        self._cool_first_step: bool = False           # phase-uncertain anchor guard
        self._cool_last_time: float | None = None     # monotonic seconds at last sample

        # Persist learned rates across reloads / HA restarts.
        device_id = self.config.get("device_id", "unknown")
        self._rates_store = Store(hass, version=1, key=f"{DOMAIN}_rates_{device_id}")
        self._rates_loaded = False  # load once on first poll

        # Anchor for time-to-target sensors.
        # Set whenever water_temperature or target_temperature changes so both
        # sensors can count down to a fixed future timestamp between temp steps.
        self.temp_anchor_time: datetime | None = None    # UTC datetime of last temp/target change
        self.temp_anchor_temp: float | None = None       # water_temp at that moment
        self.temp_anchor_target: float | None = None     # target_temp at that moment

        # Shared hysteresis flag for the time-to-target sensors.
        # Deactivates when within _NEAR_TARGET_DEACTIVATE of target;
        # reactivates only when _NEAR_TARGET_ACTIVATE away, preventing flicker.
        self.near_target: bool = False
        # Latched True on the False→True transition of near_target (spa first reaches
        # temperature).  Cleared ONLY when the user sets a new scheduled_ready_at.
        # Never cleared automatically by temperature changes — the spa staying warm
        # between sessions must not re-arm predictions.
        self.ready_latched: bool = False
        # Water temperature when the latch was set (peak while at target).  The
        # cool-off release measures against this, so "Ready" is withdrawn once
        # the spa is no longer as warm as it was when it arrived.
        self.ready_latched_temp: float | None = None
        self.scheduled_ready_at: datetime | None = None  # set by MSpaScheduledReadyAt entity
        # Target temperature the scheduler should heat to.  Exposed as a number entity
        # so the user can adjust it from the device panel without entering options.
        self.schedule_target_temp: float = float(
            config_entry.options.get(CONF_SCHEDULE_TARGET_TEMP, DEFAULT_SCHEDULE_TARGET_TEMP)
        )
        # True once the autonomous heat-start command has been sent for the current
        # schedule.  Prevents re-triggering on every poll.  Reset when schedule clears.
        self._schedule_triggered: bool = False
        # Last computed autonomous start time, tracked only to log meaningful shifts
        # in the planned start as outdoor conditions change while waiting.
        self._last_computed_start_at: datetime | None = None

        # Current ambient conditions read from optional weather sensors.
        # None until the first successful sensor read.
        self.ambient_temp: float | None = None
        self.ambient_wind: float | None = None

        # Learned baseline outdoor temperature under which the heating rates were
        # observed.  Slow EMA updated on each accepted heat-rate sample.  Used by
        # the ambient correction to know what "normal" outdoor conditions look
        # like for this spa, so colder-than-baseline nights slow the estimate.
        self.ambient_baseline: float | None = None

        # Prediction accuracy tracker.  Records the initial estimate at the
        # start of a big heating session and compares it to the actual elapsed
        # time when the target is reached.  Results are logged at INFO level
        # (grep for "PREDICTION_RESULT") and the last 10 are persisted in the
        # rates store under the key "prediction_history".
        self._prediction: dict | None = None   # active prediction (see _start_prediction)
        self._prediction_history: list[dict] = []  # last 10 completed predictions

        # Historical bias correction for heating predictions.
        # Derived from average error_percent in prediction_history.
        # A value > 1.0 means predictions have been too optimistic (actual > estimated).
        self.prediction_bias: float = 1.0

        # Extended device detail fetched once on init from /api/device/detail/
        self.device_detail: dict = {}

    @staticmethod
    def _bias_ratio(record: dict) -> float | None:
        """actual/estimated for one session, or None if unusable for the bias.

        The ratio is taken against the *raw* segmented estimate, not the biased
        one, so the bias converges on the rate model's true error rather than
        chasing its own previous output.
        """
        est    = record.get("estimated_minutes") or 0
        actual = record.get("actual_minutes") or 0
        start  = record.get("start_temp") or 0
        target = record.get("target_temp") or 0

        if (target - start) < BIAS_MIN_DELTA_C:
            return None
        if est <= 0 or actual <= 0:
            return None
        ratio = actual / est
        if not (BIAS_RATIO_MIN <= ratio <= BIAS_RATIO_MAX):
            return None
        return ratio

    def _apply_bias_sample(self, ratio: float) -> None:
        """Fold one completed session's accuracy ratio into prediction_bias.

        An incremental EMA, so the bias is monotone with respect to evidence: a
        ratio below the current bias always pulls it down and vice versa.  The
        previous implementation recomputed a weather-weighted mean over the last
        10 sessions on every call, which could move the bias *up* after a session
        that should have lowered it — and changed it on restart with no new data.
        """
        updated = self.prediction_bias + BIAS_EMA_ALPHA * (ratio - self.prediction_bias)
        self.prediction_bias = max(BIAS_CLAMP_MIN, min(BIAS_CLAMP_MAX, updated))

    def _seed_prediction_bias_from_history(self) -> None:
        """Rebuild the bias by replaying stored history through the EMA.

        Used only on the upgrade path, when persisted history exists but no
        persisted bias does.  Replaying in chronological order preserves the
        accumulated learning while applying the current algorithm to it.
        """
        self.prediction_bias = 1.0
        n = 0
        for record in self._prediction_history:
            ratio = self._bias_ratio(record)
            if ratio is not None:
                self._apply_bias_sample(ratio)
                n += 1
        _LOGGER.debug(
            "Prediction bias seeded from %d historical sessions → %.3f",
            n, self.prediction_bias,
        )

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via direct function call."""
        try:
            # Use cached status if available
            if self.api._last_status is not None:
                status_data = self.api._last_status
                self.api._last_status = None  # Clear after use
            else:
                status_data = await self.api.get_hot_tub_status()

            fault_value = status_data.get("fault", "")
            transformed_data = {
                "water_temperature": float(status_data.get("water_temperature", 0))/2,
                "target_temperature": float(status_data.get("temperature_setting", 0))/2,
                "heater": "on" if status_data.get("heater_state", 0) else "off",
                "filter": "on" if status_data.get("filter_state", 0) else "off",
                "bubble": "on" if status_data.get("bubble_state", 0) else "off",
                "jet": "on" if status_data.get("jet_state", 0) else "off",
                "ozone": "on" if status_data.get("ozone_state", 0) else "off",
                "uvc": "on" if status_data.get("uvc_state", 0) else "off",
                "bubble_level": status_data.get("bubble_level", 1),
                "fault": fault_value if fault_value else "OK",
            }

            # Pass all remaining payload keys through verbatim so new/changed
            # firmware keys appear immediately as diagnostic sensors.
            for key, value in status_data.items():
                if key not in _STRUCTURED_STATUS_KEYS:
                    transformed_data[key] = value

            self._last_data = transformed_data
            _LOGGER.debug("Fetched MSpa transformed data: %s", transformed_data)

            # Read optional weather entity for ambient-condition bias.
            self.ambient_temp, self.ambient_wind = _read_weather_entity(
                self.hass, self.config_entry.options.get(CONF_WEATHER_ENTITY)
            )

            # Update anchor whenever water_temp or target_temp changes.
            # Sensors use (anchor_time, anchor_temp, anchor_target) to count down
            # between temperature steps without needing per-sensor state.
            new_temp   = transformed_data.get("water_temperature")
            new_target = transformed_data.get("target_temperature")
            if new_temp != self.temp_anchor_temp or new_target != self.temp_anchor_target:
                self.temp_anchor_time   = datetime.now(timezone.utc)
                self.temp_anchor_temp   = new_temp
                self.temp_anchor_target = new_target

            # --- Prediction accuracy tracking ---
            # Start tracking when a big heating session begins (delta > 2°C).
            # Only start after rates have been loaded from storage to avoid using
            # the unreliable device_heat_perhour fallback on the first poll.
            # Gated on full-heat mode, the same signal the cancellation below
            # uses.  Without it, a setpoint change during rapid polling created
            # and immediately cancelled a prediction on every 1 s poll, because
            # the device drops out of heat_state 3 while it recalculates.
            if (self._rates_loaded
                    and new_temp is not None and new_target is not None
                    and new_target > new_temp
                    and (new_target - new_temp) > _NEW_SESSION_DELTA
                    and transformed_data.get("heat_state") == _HEAT_STATE_FULL
                    and self._prediction is None):
                # Lazy import to reuse the sensor's segmented calculation.
                from .sensor import _segmented_heating_minutes
                est_minutes = _segmented_heating_minutes(new_temp, new_target, self)
                if est_minutes is not None:
                    # Store the raw estimate (without bias) for history tracking.
                    # The bias is derived from raw vs actual, so storing the biased
                    # value would create a feedback loop that erases the correction.
                    raw_minutes = est_minutes / self.prediction_bias if self.prediction_bias else est_minutes
                    self._prediction = {
                        "start_time": datetime.now(timezone.utc).isoformat(),
                        "start_temp": new_temp,
                        "target_temp": new_target,
                        "estimated_minutes": round(raw_minutes, 1),
                        "estimated_minutes_biased": round(est_minutes, 1),
                        "prediction_bias": round(self.prediction_bias, 3),
                        "session_scalar": self._session_scalar,
                        "ambient_temp": self.ambient_temp,
                        "ambient_wind": self.ambient_wind,
                        "ambient_baseline": self.ambient_baseline,
                        # Hot-bucket (near-setpoint) ambient correction at start —
                        # the dominant factor; the others are derivable from
                        # ambient_temp/baseline via ambient_rate_factor().
                        "ambient_factor_hot": round(
                            ambient_rate_factor(2, self.ambient_temp, self.ambient_baseline), 3
                        ),
                    }
                    buckets_str = "/".join(
                        f"{b:.2f}" if b is not None else "-"
                        for b in self.heat_rate_buckets
                    )
                    _LOGGER.info(
                        "PREDICTION_START: %.1f°C → %.1f°C, raw=%.0f min, biased=%.0f min"
                        " (bias=%.3f, scalar=%.3f, buckets=%s, ambient=%.1f°C %.1f m/s)",
                        new_temp, new_target, raw_minutes, est_minutes,
                        self.prediction_bias, self._session_scalar, buckets_str,
                        self.ambient_temp or 0.0, self.ambient_wind or 0.0,
                    )

            # Finish tracking when near_target is newly reached.
            #
            # near_target is updated later in this same poll, so the value read
            # here is the PREVIOUS poll's.  That matters when the setpoint has
            # just been raised: a prediction created moments ago would be
            # "completed" against a stale near_target from before the change,
            # recording a zero-duration session (observed: 31.5 → 35.0 °C
            # started and finished in the same millisecond, actual 0 min,
            # error -5910280273.8%).  A real session cannot complete inside the
            # minimum sample window, so require at least that much elapsed
            # before a completion is believed.
            if (self.near_target and self._prediction is not None
                    and (datetime.now(timezone.utc)
                         - datetime.fromisoformat(self._prediction["start_time"])
                         ).total_seconds() / 3600 >= _MIN_RATE_SAMPLE_HOURS):
                start_iso = self._prediction["start_time"]
                start_dt = datetime.fromisoformat(start_iso)
                actual_minutes = (datetime.now(timezone.utc) - start_dt).total_seconds() / 60
                est = self._prediction["estimated_minutes"]
                error_minutes = actual_minutes - est
                # Guard the ratio too: a sub-minute duration would produce a
                # meaningless percentage even if it got this far.
                error_pct = (error_minutes / actual_minutes * 100) if actual_minutes >= 1 else 0
                est_biased = self._prediction.get("estimated_minutes_biased", est)
                error_minutes_biased = actual_minutes - est_biased
                result = {
                    **self._prediction,
                    "actual_minutes": round(actual_minutes, 1),
                    "error_minutes": round(error_minutes, 1),
                    "error_percent": round(error_pct, 1),
                    "error_minutes_biased": round(error_minutes_biased, 1),
                    "end_time": datetime.now(timezone.utc).isoformat(),
                }
                self._prediction_history.append(result)
                self._prediction_history = self._prediction_history[-10:]  # keep last 10
                self._prediction = None
                ratio = self._bias_ratio(result)
                if ratio is not None:
                    self._apply_bias_sample(ratio)
                _LOGGER.info(
                    "PREDICTION_RESULT: %.1f°C → %.1f°C | estimated %.0f min, actual %.0f min | error %+.0f min (%+.1f%%) | bias=%.3f",
                    result["start_temp"], result["target_temp"],
                    est, actual_minutes, error_minutes, error_pct, self.prediction_bias,
                )
            # Clear prediction if target changed mid-session.
            if (self._prediction is not None
                    and new_target is not None
                    and new_target != self._prediction.get("target_temp")):
                _LOGGER.debug("Prediction cancelled — target changed from %.1f to %.1f",
                              self._prediction.get("target_temp", 0), new_target)
                self._prediction = None

            # Update shared near-target hysteresis flag.
            _NEAR_TARGET_DEACTIVATE = 0.25
            _NEAR_TARGET_ACTIVATE   = 0.5
            if new_temp is not None and new_target is not None:
                delta = abs(new_target - new_temp)
                if delta < _NEAR_TARGET_DEACTIVATE:
                    if not self.near_target:   # latch only on the False→True transition
                        self.ready_latched = True
                        # Remember how warm the water was when it latched, so the
                        # cool-off release below can tell how much heat has since
                        # been given up.
                        self.ready_latched_temp = new_temp
                        _LOGGER.debug("ready_latched set (near_target True, delta=%.2f°C)", delta)
                    self.near_target = True
                elif delta >= _NEAR_TARGET_ACTIVATE:
                    self.near_target = False

                # Track the warmest water seen while latched.  Thermostat cycling
                # nudges the temperature either side of the setpoint — including
                # into the hysteresis dead band, where neither branch above runs —
                # so the cool-off must measure from the peak reached, not from
                # whatever the reading happened to be at the moment it latched.
                if (self.ready_latched
                        and (self.ready_latched_temp is None
                             or new_temp > self.ready_latched_temp)):
                    self.ready_latched_temp = new_temp

                # Cool-off release: withdraw "Ready" once the water has given up
                # _LATCH_COOL_OFF degrees from where it latched.  Without this the
                # latch outlives the warmth it advertises — drop the thermostat to
                # 20 °C with the water at 40 °C and, two days later, the water is
                # 24 °C, still "above target", and the sensor would happily claim
                # the tub is ready for a dip.  Checked regardless of direction,
                # because with a lowered thermostat the spa is technically cooling
                # yet still above setpoint.
                if (self.ready_latched
                        and self.ready_latched_temp is not None
                        and (self.ready_latched_temp - new_temp) >= _LATCH_COOL_OFF):
                    self.ready_latched = False
                    _LOGGER.info(
                        "ready_latched released (water cooled %.1f°C from %.1f°C "
                        "to %.1f°C — no longer warm enough to call ready)",
                        self.ready_latched_temp - new_temp,
                        self.ready_latched_temp, new_temp,
                    )
                    self.ready_latched_temp = None
                    # Release the readiness latch once a genuine heating gap
                    # opens.  The latch exists to hold "Ready" steady once the
                    # spa has arrived, but raising the setpoint means there is
                    # real heating to do and the sensor must follow the
                    # thermostat again — otherwise it stays pinned on "Ready"
                    # with degrees to go.
                    #
                    # Heating direction only: water ABOVE the setpoint (the
                    # thermostat was lowered while the spa is warm) is still
                    # legitimately "Ready".  Using the new-session threshold
                    # rather than the hysteresis one means thermostat cycling
                    # cannot flicker the latch off and on.
                    if (self.ready_latched
                            and (new_target - new_temp) > _NEW_SESSION_DELTA):
                        self.ready_latched = False
                        self.ready_latched_temp = None
                        _LOGGER.debug(
                            "ready_latched released (setpoint %.1f°C is %.1f°C "
                            "above water %.1f°C — new heating session)",
                            new_target, new_target - new_temp, new_temp,
                        )
            # else: no temp data — leave flags unchanged

            # Load persisted rates on the very first poll after startup/reload.
            if not self._rates_loaded:
                self._rates_loaded = True
                stored = await self._rates_store.async_load()
                if stored:
                    self.computed_heat_rate = stored.get("heat_rate")
                    self.computed_cool_rate = stored.get("cool_rate")
                    self.ambient_baseline = stored.get("ambient_baseline")
                    # Restore the temperature anchor so a restart mid-step doesn't
                    # discard the elapsed heating time and jump the ETA forward.
                    # A fresh "now" anchor was already set earlier this poll; only
                    # override it back to the persisted time when the water temp
                    # and target still match — otherwise the step advanced while
                    # HA was down and the fresh anchor is the correct one.
                    anchor_iso = stored.get("temp_anchor_time")
                    if (anchor_iso is not None
                            and stored.get("temp_anchor_temp") == new_temp
                            and stored.get("temp_anchor_target") == new_target):
                        try:
                            self.temp_anchor_time   = datetime.fromisoformat(anchor_iso)
                            self.temp_anchor_temp   = new_temp
                            self.temp_anchor_target = new_target
                            _LOGGER.debug(
                                "Restored temp anchor: %.1f°C → %.1f°C at %s",
                                new_temp or 0.0, new_target or 0.0, anchor_iso,
                            )
                        except (ValueError, TypeError):
                            pass
                    stored_buckets = stored.get("heat_rate_buckets")
                    self._prediction_history = stored.get("prediction_history", [])
                    # Restore the bias as a stored value — never recompute it here.
                    # Recomputing on load made it depend on the weather at startup,
                    # so a restart alone could change every subsequent prediction.
                    stored_bias = stored.get("prediction_bias")
                    if stored_bias is not None:
                        self.prediction_bias = max(
                            BIAS_CLAMP_MIN, min(BIAS_CLAMP_MAX, float(stored_bias))
                        )
                    else:
                        self._seed_prediction_bias_from_history()
                    # Restore in-progress prediction so a restart mid-heatup doesn't
                    # drop the session from the learning history.
                    self._prediction = stored.get("active_prediction")
                    # Restore the schedule trigger state so a restart during a
                    # scheduled heat-up resumes as "Heating" rather than
                    # re-planning a start for a heater that is already running.
                    # If the schedule turns out to be gone or expired, the
                    # datetime entity's restore path clears this again.
                    self._schedule_triggered = bool(
                        stored.get("schedule_triggered", False)
                    )
                    if self._prediction:
                        _LOGGER.info(
                            "Restored in-progress prediction: %.1f°C → %.1f°C (started %s)",
                            self._prediction.get("start_temp", 0),
                            self._prediction.get("target_temp", 0),
                            self._prediction.get("start_time", "unknown"),
                        )
                    if isinstance(stored_buckets, list) and len(stored_buckets) == 3:
                        save_ts = stored.get("bucket_save_ts")
                        if save_ts is not None:
                            days_elapsed = (time.time() - save_ts) / 86400
                            # Decay each bucket toward the global flat EMA over time so
                            # stale seasonal rates don't anchor predictions indefinitely.
                            # With a weather entity, ambient_rate_factor explains some
                            # of the seasonal variation directly, so the stored rates
                            # stay useful for longer and can decay more slowly.
                            # Without weather: 0.98/day → ~75% weight at 14 days.
                            # With weather:    0.994/day → ~92% weight at 14 days.
                            has_weather = bool(
                                self.config_entry.options.get(CONF_WEATHER_ENTITY)
                            )
                            decay_rate = 0.994 if has_weather else 0.98
                            decay = max(0.4, decay_rate ** days_elapsed)
                            global_rate = self.computed_heat_rate
                            self.heat_rate_buckets = [
                                (float(b) * decay + global_rate * (1 - decay))
                                if b is not None and global_rate is not None
                                else (float(b) * decay if b is not None else None)
                                for b in stored_buckets
                            ]
                            _LOGGER.debug(
                                "Loaded buckets: %.1f days old, decay=%.3f (weather=%s) → %s",
                                days_elapsed, decay, has_weather, self.heat_rate_buckets,
                            )
                        else:
                            self.heat_rate_buckets = [
                                float(b) if b is not None else None for b in stored_buckets
                            ]
                    _LOGGER.debug(
                        "Restored rates from storage: heat=%.3f cool=%.3f",
                        self.computed_heat_rate or 0,
                        self.computed_cool_rate or 0,
                    )

            # --- Observed heating rate tracking (see _track_heating_rate) ---
            curr_temp = transformed_data.get("water_temperature")
            heat_state = transformed_data.get("heat_state")
            now_mono = time.monotonic()

            # Detect new heating session.  Only reset the session scalar when
            # the heater newly engages AND delta-to-target is large (> 2°C).
            # This avoids false resets from thermostat cycling near setpoint.
            heater_now_active = (heat_state == _HEAT_STATE_FULL)
            if heater_now_active and not self._heat_was_active:
                delta_to_target = abs((new_target or 0) - (curr_temp or 0))
                if delta_to_target > _NEW_SESSION_DELTA:
                    self._session_scalar = 1.0
                    self._session_scalar_bucket = None
                    self._session_fresh_buckets = set()
                    _LOGGER.debug(
                        "MSpa: new heating session (delta %.1f°C) — session scalar reset",
                        delta_to_target,
                    )
            self._heat_was_active = heater_now_active

            # If the heater stops while we're still significantly below target, the
            # session has been interrupted (maintenance, power cycle, user manually
            # turning off the heater).  Cancel any active prediction so that when
            # circulation later mixes the water and near_target triggers, a spurious
            # short result isn't recorded in the history.
            if (not heater_now_active
                    and self._prediction is not None
                    and not self.near_target):
                _LOGGER.info(
                    "PREDICTION_CANCELLED: heater stopped at %.1f°C with %.1f°C to go "
                    "— session interrupted (maintenance / power cycle / manual off)",
                    curr_temp or 0,
                    abs((new_target or 0) - (curr_temp or 0)),
                )
                self._prediction = None

            self._track_heating_rate(curr_temp, heat_state, now_mono)

            # --- Observed cooling rate tracking (see _track_cooling_rate) ---
            self._track_cooling_rate(curr_temp, heat_state, now_mono)
            # --- end rate tracking ---

            # Persist updated rates so they survive reloads and HA restarts.
            # _prediction is included so an in-progress heatup is not lost on restart.
            await self._rates_store.async_save({
                "heat_rate": self.computed_heat_rate,
                "cool_rate": self.computed_cool_rate,
                "heat_rate_buckets": self.heat_rate_buckets,
                "bucket_save_ts": time.time(),
                "prediction_history": self._prediction_history,
                "prediction_bias": self.prediction_bias,
                "active_prediction": self._prediction,
                # Persisted so a restart mid-scheduled-heating resumes as
                # "Heating" instead of dropping back to pending and re-firing.
                "schedule_triggered": self._schedule_triggered,
                "ambient_baseline": self.ambient_baseline,
                "temp_anchor_time": (
                    self.temp_anchor_time.isoformat()
                    if self.temp_anchor_time is not None else None
                ),
                "temp_anchor_temp": self.temp_anchor_temp,
                "temp_anchor_target": self.temp_anchor_target,
            })

            # Trigger heating autonomously when the schedule window opens.
            # Must run BEFORE the auto-clear so the trigger fires even when the
            # schedule target is already reached and minutes_needed == 0 (the
            # start_at == target_utc edge-case that the auto-clear would otherwise
            # pre-empt by clearing scheduled_ready_at first).
            await self._check_schedule_trigger(new_temp, new_target)

            # Auto-clear the schedule once its time has passed.
            self._clear_schedule_if_expired(new_temp)

            # Check for power cycle and restore state if enabled
            await self._check_power_cycle(transformed_data)
            
            # Handle temperature unit enforcement (if always_enforce_unit is enabled)
            await self._enforce_temperature_unit(transformed_data)
            
            # Check if we need to adjust polling based on heat state or pending changes
            await self._check_adaptive_polling(transformed_data)
            
            return transformed_data

        except Exception as err:
            _LOGGER.error("Error updating MSpa data: %s", str(err))
            raise UpdateFailed(f"Update failed: {str(err)}")

    def _clear_schedule_if_expired(self, current_temp) -> None:
        """Retire the schedule once its ready time has passed.

        Clears the trigger state AND the readiness latch: the latch exists so
        "Ready" holds steady through the scheduled session, but once the
        session is over the Ready at sensor must be released to follow the
        thermostat again — otherwise it stays pinned on "Ready" while the
        water cools or the setpoint is lowered, until the next schedule or a
        reload happens to reset it.  If the spa genuinely is at the thermostat
        target after clearing, near_target keeps the sensor on "Ready" on its
        own merit.
        """
        if (self.scheduled_ready_at is not None
                and dt_util.utcnow() >= dt_util.as_utc(self.scheduled_ready_at)):
            self.clear_schedule("session complete", current_temp)

    def clear_schedule(self, reason: str, current_temp: float | None = None) -> None:
        """Retire the schedule and all state derived from it.

        Deliberately does NOT touch the heater or the setpoint: cancelling a plan
        for later says nothing about whether the spa should be heating now.  A
        user who wants it off turns it off.

        Shared by the expiry auto-clear and the Cancel heat schedule button so the
        two cannot drift — the latch release in particular is easy to omit, and
        forgetting it once already shipped as a bug where Ready at stayed pinned
        on "Ready" indefinitely after a session.
        """
        if self.scheduled_ready_at is None:
            return
        _LOGGER.info(
            "Heat schedule: %s — clearing  "
            "[water=%.1f°C  sched=%.1f°C  triggered=%s  latched=%s]",
            reason, current_temp or 0.0, self.schedule_target_temp or 0.0,
            self._schedule_triggered, self.ready_latched,
        )
        self.scheduled_ready_at = None
        self._schedule_triggered = False
        self._last_computed_start_at = None
        self.ready_latched = False
        self.ready_latched_temp = None

    def _track_heating_rate(self, curr_temp, heat_state, now_mono: float) -> None:
        """Sample the observed heating rate over a growing per-bucket window.

        Only samples in full-heat mode (heat_state == 3) to avoid preheat and
        cooling skew.

        The first crossing after the heater engages is phase-uncertain: the
        temperature reports in 0.5 °C bands, so the anchor set at heater-on
        sits at an unknown position inside its band and the time to the first
        crossing measures that random phase, not the rate (~2x fast on
        average, arbitrarily fast when the water started near a boundary).
        That crossing re-anchors only — the anchor is then exactly on a band
        boundary — and learning starts from the second crossing.

        From there the anchor is *held*, so each sample measures from the first
        boundary reached in this bucket to the current one rather than from the
        previous crossing.  Every crossing lands exactly on a band boundary, so
        any boundary-to-boundary span is equally phase-exact — a wider span is
        no more biased, and much less noisy, because rate is delta/elapsed and a
        few minutes of report-timing jitter matters far less over hours than
        over one 30-minute step.

        Measured on the 2026-08-06 session (22 samples): per-step sampling gave
        18% noise in the cold bucket and dragged its stored rate to 0.98 °C/h
        against a realised 1.14; the growing window gives 7% and 1.08.  In the
        mid bucket, 3% becomes 1% (0.4% once the span reaches 2 °C), converging
        on 0.958 °C/h — exactly that bucket's realised rate.  It also absorbs
        paired reporting glitches that per-step sampling cannot: that session
        contained 27.5→28.0 at 0.51 °C/h immediately followed by 28.0→28.5 at
        29.5 °C/h — one late report then one early — where the out-of-bounds
        second is rejected and the spuriously slow first is learned.

        The window closes when the water leaves the anchor's bucket (each bucket
        must be measured on its own, since they model different loss regimes) or
        when a sample is rejected (a bad span is never re-used).  Heater-off
        resets it entirely via the branch below, so a window never spans an
        interruption.

        Successive estimates from a growing window share most of their data, so
        the per-crossing EMA double-counts the early samples and its effective
        time constant is shorter than nominal.  That is harmless here — the
        sequence it averages is smooth and converging rather than noisy — but it
        is why the EMA must not be re-tuned on the assumption that samples are
        independent.
        """
        if heat_state == _HEAT_STATE_FULL and curr_temp is not None:
            if self._rate_last_temp is None:
                # First poll in heat mode — set a phase-uncertain anchor.
                self._rate_last_temp = curr_temp
                self._rate_last_time = now_mono
                self._rate_prev_temp = curr_temp
                self._rate_first_step = True
            elif curr_temp != self._rate_prev_temp:
                # A genuine change since the previous poll.  This must be tested
                # against the previous *reading*, not against the window anchor:
                # the anchor is deliberately held across several crossings, so
                # comparing to it stays true on every poll until the next change
                # and re-learns the same span once per poll with an ever-growing
                # elapsed time.  Observed 2026-08-07: one 36.5→37.0 crossing
                # re-learned every 30 s for twelve minutes, walking bucket[2]
                # from 1.040 down to 0.822.
                self._rate_prev_temp = curr_temp
                if self._rate_first_step:
                    # First crossing: position information only.  Keep the
                    # existing learned rates driving the estimates.
                    _LOGGER.info(
                        "Heat rate: first crossing %.1f→%.1f°C after heater-on "
                        "is phase-uncertain — anchored, not learned",
                        self._rate_last_temp, curr_temp,
                    )
                    self._rate_first_step = False
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
                    return
                # Temperature has changed — elapsed time since the window anchor
                # is the true duration of the whole boundary-to-boundary span.
                elapsed_hours = (now_mono - self._rate_last_time) / 3600
                anchor_bucket = _heat_bucket_index(self._rate_last_temp)
                accepted = False
                if elapsed_hours >= _MIN_RATE_SAMPLE_HOURS:
                    delta = curr_temp - self._rate_last_temp
                    rate = delta / elapsed_hours  # guarded: elapsed_hours > 0
                    if _MIN_HEAT_RATE <= rate <= _MAX_HEAT_RATE:
                        accepted = True
                        if self.computed_heat_rate is None:
                            self.computed_heat_rate = rate
                        else:
                            self.computed_heat_rate = (
                                _EMA_ALPHA * rate
                                + (1 - _EMA_ALPHA) * self.computed_heat_rate
                            )
                        # Also update the temperature bucket for this observation.
                        # The window anchor is the *start* temperature of the span.
                        _bi = anchor_bucket
                        _bp = self.heat_rate_buckets[_bi]
                        self.heat_rate_buckets[_bi] = (
                            _EMA_ALPHA * rate + (1 - _EMA_ALPHA) * _bp
                        ) if _bp is not None else rate
                        self._session_fresh_buckets.add(_bi)
                        # Track the baseline outdoor temperature under which
                        # rates are being learned, so the ambient correction
                        # knows what "normal" looks like for this spa.  Slow
                        # EMA so it reflects the seasonal norm, not one night.
                        # Seed from a neutral default (not the first, possibly
                        # cold, reading) so a cold night registers as
                        # below-normal immediately rather than after the EMA
                        # slowly drifts up.
                        if self.ambient_temp is not None:
                            base = (
                                self.ambient_baseline
                                if self.ambient_baseline is not None
                                else AMBIENT_BASELINE_DEFAULT
                            )
                            self.ambient_baseline = (
                                AMBIENT_BASELINE_ALPHA * self.ambient_temp
                                + (1 - AMBIENT_BASELINE_ALPHA) * base
                            )
                        # The first bucket to receive data in this session becomes
                        # the scalar source.  Its observations are compared against
                        # the stored base to derive an ambient-condition factor that
                        # is applied to all *other* buckets that haven't been
                        # observed yet this session.
                        if self._session_scalar_bucket is None and _bp is not None:
                            self._session_scalar_bucket = _bi
                        if _bi == self._session_scalar_bucket and _bp is not None:
                            ratio = max(0.5, min(2.0, rate / _bp))
                            self._session_scalar = 0.4 * ratio + 0.6 * self._session_scalar
                            _LOGGER.debug(
                                "Session scalar → %.3f (bucket[%d] ratio actual/base=%.3f)",
                                self._session_scalar, _bi, ratio,
                            )
                        _LOGGER.info(
                            "Heat rate sample %.3f °C/h over %.1f °C / %.1f h "
                            "(EMA → %.3f °C/h, bucket[%d] → %.3f °C/h)",
                            rate, delta, elapsed_hours, self.computed_heat_rate,
                            _bi, self.heat_rate_buckets[_bi],
                        )
                    else:
                        _LOGGER.info(
                            "Heat rate sample %.3f °C/h rejected (out of bounds)", rate
                        )
                # Close the window only when it must be: the water has left the
                # anchor's bucket (the next bucket models a different loss regime
                # and is measured on its own), or the sample was rejected (a bad
                # span must never be re-used).  Otherwise hold the anchor so the
                # span keeps widening and the estimate keeps improving.
                if not accepted or _heat_bucket_index(curr_temp) != anchor_bucket:
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
            # else: temperature unchanged — let elapsed time accumulate, don't touch anchor
        else:
            # Heater off/preheat — reset anchor so next on-cycle starts fresh
            # (and the next first crossing is treated as phase-uncertain again).
            self._rate_last_temp = None
            self._rate_last_time = None
            self._rate_prev_temp = None
            self._rate_first_step = False

    def _track_cooling_rate(self, curr_temp, heat_state, now_mono: float) -> None:
        """Sample the passive cooling rate from temperature drops.

        Subject to the same phase-uncertainty rule as heating: the anchor set
        when the heater stops is a 0.5 °C band value at an unknown internal
        position, so the first crossing re-anchors only and learning starts
        from the second.  This matters more here than for heating, because
        thermostat cycling re-arms the anchor on every heater cycle — each
        cycle previously injected one phase-biased (fast) sample into the
        cooling EMA.
        """
        _MIN_COOL_RATE = 0.01  # °C/h — below this is sensor drift
        _MAX_COOL_RATE = 3.0   # °C/h — above this is unusual for an insulated spa

        if heat_state not in (2, _HEAT_STATE_FULL) and curr_temp is not None:
            if self._cool_last_temp is None:
                # First poll in cooling mode — set a phase-uncertain anchor.
                self._cool_last_temp = curr_temp
                self._cool_last_time = now_mono
                self._cool_first_step = True
            elif curr_temp != self._cool_last_temp and self._cool_first_step:
                # First crossing since the heater stopped: position only.
                _LOGGER.info(
                    "Cool rate: first crossing %.1f→%.1f°C after heater-off "
                    "is phase-uncertain — anchored, not learned",
                    self._cool_last_temp, curr_temp,
                )
                self._cool_first_step = False
                self._cool_last_temp = curr_temp
                self._cool_last_time = now_mono
            elif curr_temp != self._cool_last_temp:
                # Temperature has changed — compute rate only for drops.
                elapsed_hours = (now_mono - self._cool_last_time) / 3600
                if elapsed_hours >= _MIN_RATE_SAMPLE_HOURS:
                    delta = curr_temp - self._cool_last_temp
                    if delta < 0:  # temperature actually dropped
                        rate = -delta / elapsed_hours  # positive °C/h
                        if _MIN_COOL_RATE <= rate <= _MAX_COOL_RATE:
                            if self.computed_cool_rate is None:
                                self.computed_cool_rate = rate
                            else:
                                self.computed_cool_rate = (
                                    _EMA_ALPHA * rate
                                    + (1 - _EMA_ALPHA) * self.computed_cool_rate
                                )
                            _LOGGER.info(
                                "Cool rate sample %.3f °C/h (EMA → %.3f °C/h)",
                                rate, self.computed_cool_rate,
                            )
                        else:
                            _LOGGER.debug(
                                "Cool rate sample %.3f °C/h rejected (out of bounds)", rate
                            )
                # Always advance anchor on any temperature change (rise or drop).
                self._cool_last_temp = curr_temp
                self._cool_last_time = now_mono
            # else: temperature unchanged — let elapsed time accumulate
        else:
            # Actively heating — reset cooling anchor so next off-cycle starts
            # fresh (its first crossing will again be phase-uncertain).
            self._cool_last_temp = None
            self._cool_last_time = None
            self._cool_first_step = False

    async def _check_schedule_trigger(
        self, current_temp: float | None, current_target: float | None
    ) -> None:
        """Start the heater autonomously when the schedule window opens.

        Fires at most once per schedule (guarded by _schedule_triggered).
        Skips if the heater is already running at the right temperature.
        Also handles the case where the spa is at or above the schedule target
        (cooling direction): minutes_needed == 0, so start_at == target_utc and
        the trigger fires at the scheduled time to ensure the setpoint and heater
        state are correct for maintenance once the water reaches target.
        """
        if self.scheduled_ready_at is None or self._schedule_triggered:
            return
        if current_temp is None:
            return

        target_temp = self.schedule_target_temp
        target_utc  = dt_util.as_utc(self.scheduled_ready_at)
        now_utc     = dt_util.utcnow()

        # A target well in the past is a stale plan, not a window that just opened.
        # Firing on it starts the heater for a session the user no longer wants —
        # reported 2026-08-08, where editing the schedule's *date* to a past day
        # (the only way to clear it before the Cancel button existed) turned the
        # heater on.  Inside the grace period the existing behaviour stands: a
        # target a few minutes ago should still confirm its setpoint, since that
        # is a window opening rather than an abandoned plan.
        if now_utc - target_utc > _SCHEDULE_STALE_AFTER:
            _LOGGER.info(
                "Heat schedule: target %s is %.1f h in the past — abandoning "
                "without starting the heater",
                target_utc.isoformat(),
                (now_utc - target_utc).total_seconds() / 3600,
            )
            self.clear_schedule("stale target", current_temp)
            return

        minutes_needed = self._compute_heating_minutes(current_temp, target_temp)
        if minutes_needed is None:
            # No learned rate data yet.  If the scheduled time has already arrived,
            # fire immediately with 0 lead time — raising the setpoint now is
            # preferable to missing the window entirely.  If the time hasn't arrived
            # yet we can't know when to start, so wait for rate data.
            if now_utc >= target_utc:
                minutes_needed = 0.0
            else:
                return  # Will retry on next poll

        start_at = target_utc - timedelta(minutes=minutes_needed)

        # Log a meaningful shift in the planned start time (e.g. as the outdoor
        # temperature changes while waiting), so the ambient-driven rescheduling
        # is visible.  Recomputed every poll; only shifts > 15 min are logged.
        prev_start = self._last_computed_start_at
        if prev_start is None or abs((start_at - prev_start).total_seconds()) > 900:
            if prev_start is not None:
                eff = (
                    (self.schedule_target_temp - current_temp) / (minutes_needed / 60.0)
                    if minutes_needed and current_temp is not None
                    and self.schedule_target_temp is not None else None
                )
                _LOGGER.info(
                    "Heat schedule: start time moved %s → %s (%.0f min heat @%s, "
                    "outdoor=%s°C, baseline=%s°C)",
                    prev_start.isoformat(timespec="minutes"),
                    start_at.isoformat(timespec="minutes"),
                    minutes_needed,
                    f"{eff:.2f}°C/h" if eff is not None else "n/a",
                    f"{self.ambient_temp:.1f}" if self.ambient_temp is not None else "n/a",
                    f"{self.ambient_baseline:.1f}" if self.ambient_baseline is not None else "n/a",
                )
            self._last_computed_start_at = start_at

        if now_utc < start_at:
            return  # Not yet time

        heater_on = self._last_data.get("heater") == "on"

        _LOGGER.info(
            "Heat schedule: confirming setpoint %.1f°C (water %.1f°C, %.0f min before %s)",
            target_temp, current_temp, minutes_needed, target_utc.isoformat(),
        )
        # Set the flag BEFORE the first await so that if the debounce commit for a
        # new schedule fires during the API call (resetting the flag to False and
        # updating scheduled_ready_at), the new schedule is not accidentally marked
        # as already triggered once the awaits return.
        self._schedule_triggered = True
        # A new heating session is starting — clear the latch so the Ready at
        # sensor counts down to the new target rather than showing "Ready"
        # immediately from a previous session.
        self.ready_latched = False
        self.ready_latched_temp = None
        self.near_target   = False
        try:
            # Always confirm the setpoint at the scheduled time regardless of current
            # state — the setpoint must match the schedule target even when no heating
            # was needed (spa already warm, or warm-day epsilon case).
            await self.api.set_temperature_setting(target_temp)
            if not heater_on:
                await self.set_feature_state("heater", "on")
        except Exception as err:
            self._schedule_triggered = False  # allow retry on next poll
            _LOGGER.error("Heat schedule: failed to start conditioning: %s", err)

    def _compute_heating_minutes(self, from_temp: float, to_temp: float) -> float | None:
        """Heating time (minutes) from from_temp to to_temp using learned bucket rates.

        Mirrors the sensor's _segmented_heating_minutes so the coordinator can
        compute start times without a circular import.
        Returns 0.0 when from_temp is within 0.5°C of to_temp (the near-target
        hysteresis band) so the trigger fires at the scheduled time even when
        rate data is absent or when the spa barely cooled on a warm day.
        """
        if from_temp >= to_temp or (to_temp - from_temp) < 0.5:
            return 0.0
        _T1, _T2 = 30.0, 37.0
        boundaries = [from_temp]
        for t in (_T1, _T2):
            if from_temp < t < to_temp:
                boundaries.append(t)
        boundaries.append(to_temp)

        total = 0.0
        for i in range(len(boundaries) - 1):
            seg_start = boundaries[i]
            seg_end   = boundaries[i + 1]
            delta     = seg_end - seg_start
            if delta <= 0:
                continue
            rate = self._bucket_rate_at(seg_start)
            if rate is None or rate <= 0:
                return None
            total += (delta / rate) * 60.0

        bias = getattr(self, "prediction_bias", 1.0)
        return total * bias

    def _bucket_rate_at(self, temp: float) -> float | None:
        """Best available heating rate (°C/h) for the bucket containing temp."""
        _T1, _T2 = 30.0, 37.0
        buckets        = getattr(self, "heat_rate_buckets", [None, None, None])
        session_scalar = getattr(self, "_session_scalar", 1.0)
        fresh          = getattr(self, "_session_fresh_buckets", set())

        idx = 0 if temp < _T1 else 1 if temp < _T2 else 2
        rate, source_idx = None, idx
        if buckets[idx] is not None:
            rate = buckets[idx]
        else:
            for i in range(3):
                if buckets[i] is not None:
                    rate = buckets[i]
                    source_idx = i
                    break

        if rate is None:
            raw = self._last_data.get("device_heat_perhour", 0)
            try:
                raw = int(raw)
                if raw > 0:
                    return max(0.5, min(2.0, raw / 10.0))
            except (TypeError, ValueError):
                pass
            return None

        # Correction precedence for the bucket the water is actually in (idx):
        #   1. A bucket observed this session already reflects today's real
        #      conditions — use it verbatim.
        #   2. Otherwise the empirical session scalar (observed vs. base rate)
        #      supersedes the weather model when it is active.
        #   3. Otherwise apply the ambient (weather-model) correction, which is
        #      what drives the pre-start estimate before any observation exists.
        if source_idx in fresh:
            return rate
        if session_scalar != 1.0:
            return rate * session_scalar
        return rate * ambient_rate_factor(idx, self.ambient_temp, self.ambient_baseline)

    # Map of features to their respective API methods
    FEATURE_API_MAP = {
        "heater": "set_heater_state",
        "filter": "set_filter_state",
        "bubble": "set_bubble_state",
        "jet": "set_jet_state",
        "ozone": "set_ozone_state",
        "uvc": "set_uvc_state",
    }

    async def _start_pump_before_heater(self) -> bool:
        """Bring the circulation pump up before the heater is commanded.

        Returns True when a pump command was actually sent, so the caller can fold
        the filter into the rapid-poll expectations and the retry payload.

        Raises if the pump will not start — deliberately, because commanding the
        heater is then the one thing we must not do.  Callers surface it: the
        scheduler clears its trigger flag and retries on the next poll, and the
        climate/switch paths report the failure to the user.
        """
        if self._last_data.get("filter") == "on":
            return False        # already circulating — nothing to do

        _LOGGER.info("Heater requested: starting the circulation pump first")
        response = await self.api.set_filter_state(1)
        # send_device_command returns the parsed payload; anything other than
        # SUCCESS means the spa did not accept it.
        if not (isinstance(response, dict) and response.get("message") == "SUCCESS"):
            raise RuntimeError(
                f"pump did not start (response: {response!r}) — not starting the heater"
            )
        self._last_data["filter"] = "on"
        if _PUMP_SETTLE_SECONDS:
            await asyncio.sleep(_PUMP_SETTLE_SECONDS)
        return True

    async def set_feature_state(self, feature: str, state: str) -> None:
        """Set a feature state using the API map."""
        _LOGGER.debug(f"Setting MSpa feature {feature} to {state}")
        try:
            if state.lower() not in ["on", "off"]:
                raise ValueError("State must be 'on' or 'off'")
            numerical_state = 1 if state.lower() == "on" else 0
            api_method = getattr(self.api, self.FEATURE_API_MAP[feature])

            # Heating without flow is refused by the spa, so the pump leads.
            pump_started = False
            if feature == "heater" and numerical_state == 1:
                pump_started = await self._start_pump_before_heater()

            # Build raw command for potential retry
            if feature == "bubble":
                bubble_level = max(1, self._last_data.get("bubble_level") or 1)
                self._last_data["bubble_level"] = bubble_level
                await api_method(numerical_state, bubble_level)
                raw_command = {"bubble_state": numerical_state, "bubble_level": bubble_level}
            else:
                await api_method(numerical_state)
                raw_key = _PENDING_TO_RAW_KEY.get(feature)
                raw_command = {raw_key: numerical_state} if raw_key else {}

            # Turning off the filter also forces the heater off (handled in the API
            # layer).  Register that implicit change here so _pending_changes stays
            # consistent and the retry payload doesn't accidentally resend a stale
            # heater-on command from an earlier user action.
            expected_changes = {feature: state}
            if feature == "filter" and numerical_state == 0:
                expected_changes["heater"] = "off"
                raw_command["heater_state"] = 0
            # A soft start changed the filter too.  Registering it keeps the rapid
            # poll honest, and putting it in raw_command means a heater retry
            # re-asserts the pump rather than resending a bare heater-on.
            if pump_started:
                expected_changes["filter"] = "on"
                raw_command["filter_state"] = 1

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(expected_changes, raw_command)
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set %s to %s: %s", feature, state, str(err))
            raise

    async def set_temperature(self, service: ServiceCall) -> None:
        """Set the target temperature."""
        try:
            temperature = service.data.get(ATTR_TEMPERATURE)
            _LOGGER.debug("Setting temperature to %s", temperature)
            await self.api.set_temperature_setting(temperature)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"target_temperature": temperature},
                {"temperature_setting": int(temperature * 2)},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set temperature: %s", str(err))
            raise

    async def set_bubble(self, service: ServiceCall) -> None:
        """Set the bubble state."""
        try:
            bubble_state = service.data.get(ATTR_STATE)
            _LOGGER.debug("Setting bubble state to %s", bubble_state)
            numerical_state = 1 if bubble_state.lower() == "on" else 0
            bubble_level = max(1, self._last_data.get("bubble_level") or 1)
            await self.api.set_bubble_state(numerical_state, bubble_level)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"bubble": bubble_state},
                {"bubble_state": numerical_state, "bubble_level": bubble_level},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set bubble state: %s", str(err))
            raise

    async def set_bubble_level(self, service: ServiceCall) -> None:
        try:
            bubble_level = service.data.get("level")
            _LOGGER.debug("Setting bubble level to %s", bubble_level)
            self._last_data["bubble_level"] = bubble_level
            # Setting a level activates the bubbles on the device — send state+level
            # together so HA's pending-change tracking stays consistent.
            await self.api.set_bubble_state(1, bubble_level)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"bubble_level": bubble_level, "bubble": "on"},
                {"bubble_state": 1, "bubble_level": bubble_level},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set bubble level: %s", str(err))
            raise

    async def set_temperature_unit(self, unit: int) -> None:
        """Set temperature unit (0=Celsius, 1=Fahrenheit)."""
        try:
            _LOGGER.debug("Setting temperature unit to %s", unit)
            await self.api.set_temperature_unit(unit)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"temperature_unit": unit},
                {"temperature_unit": unit},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set temperature unit: %s", str(err))
            raise

    # Generic service handler for features
    async def handle_feature_service(self, service: ServiceCall) -> None:
        feature = service.service.replace("set_", "")
        state = service.data.get(ATTR_STATE)
        await self.set_feature_state(feature, state)

    # Register these as service handlers in __init__.py:
    # set_heater, set_filter, set_bubble, set_jet, set_ozone, set_uvc
    set_heater = handle_feature_service
    set_filter = handle_feature_service
    # set_bubble = handle_feature_service
    set_jet = handle_feature_service
    set_ozone = handle_feature_service
    set_uvc = handle_feature_service

    async def _check_adaptive_polling(self, data: dict) -> None:
        """3-tier adaptive polling: idle → active → rapid.

        Tiers:
          RAPID (1s, 15s window)  — after sending a command, to confirm it took effect
          EXTERNAL (5s, 15s)     — after detecting an unexpected state change (panel/app)
          ACTIVE (30s)           — something is running (heater, filter, bubble, jet)
          IDLE (120s)            — nothing running and state stable for 10+ minutes

        Transitions between active/idle happen automatically based on device state.
        Rapid/external are time-boxed bursts that fall back to active/idle when done.
        """
        current_time = time.monotonic()

        # --- Phase 1: Handle pending command confirmations (rapid tier) ---
        if self._pending_changes:
            confirmed = []
            for key, expected_value in list(self._pending_changes.items()):
                if data.get(key) == expected_value:
                    _LOGGER.debug("Pending change confirmed: %s = %s", key, expected_value)
                    confirmed.append(key)

            for key in confirmed:
                del self._pending_changes[key]
                raw_key = _PENDING_TO_RAW_KEY.get(key)
                if raw_key:
                    self._pending_raw_command.pop(raw_key, None)

            if confirmed and not self._pending_changes:
                self._command_retry_count = 0

        # --- Phase 2: Check rapid poll timeout / retry ---
        if self._rapid_poll_until and time.time() > self._rapid_poll_until:
            if self._pending_changes:
                if self._command_retry_count < 1 and self._pending_raw_command:
                    self._command_retry_count += 1
                    _LOGGER.warning(
                        "Spa did not confirm %s — retrying command %s (attempt %d)",
                        self._pending_changes,
                        self._pending_raw_command,
                        self._command_retry_count,
                    )
                    try:
                        await self.api.send_device_command(self._pending_raw_command)
                    except Exception as err:
                        _LOGGER.error("Command retry failed: %s", err)
                    self._rapid_poll_until = time.time() + RAPID_POLL_TIMEOUT
                else:
                    _LOGGER.warning(
                        "Spa did not confirm changes after retry: %s — giving up",
                        self._pending_changes,
                    )
                    self._pending_changes.clear()
                    self._pending_raw_command.clear()
                    self._command_retry_count = 0
                    self._rapid_poll_until = None
            else:
                self._rapid_poll_until = None

        # --- Phase 3: Check external-change timeout ---
        if self._external_change_until and time.time() > self._external_change_until:
            self._external_change_until = None

        # --- Phase 4: Detect unexpected external state changes ---
        # Compare control keys against last snapshot; if something changed that
        # we didn't command, someone used the physical panel or MSpa Link app.
        _CONTROL_KEYS = ("heater", "filter", "bubble", "jet", "ozone", "uvc", "target_temperature")
        if self._last_snapshot and not self._pending_changes:
            for key in _CONTROL_KEYS:
                old_val = self._last_snapshot.get(key)
                new_val = data.get(key)
                if old_val is not None and new_val is not None and old_val != new_val:
                    _LOGGER.info(
                        "External change detected: %s changed %s → %s (not commanded by HA)",
                        key, old_val, new_val,
                    )
                    self._external_change_until = time.time() + EXTERNAL_CHANGE_TIMEOUT
                    self._last_state_change_time = current_time
                    break

        # --- Phase 5: Track heat state transitions for logging ---
        current_heat_state = data.get("heat_state")
        if self._last_heat_state != current_heat_state:
            _LOGGER.debug("Heat state changed: %s -> %s", self._last_heat_state, current_heat_state)
            self._last_heat_state = current_heat_state
            self._last_state_change_time = current_time

        # --- Phase 6: Determine appropriate tier and set interval ---
        if self._rapid_poll_until or self._pending_changes:
            # RAPID tier: confirming a command we sent
            target_interval = RAPID_SCAN_INTERVAL
            tier = "rapid"
        elif self._external_change_until:
            # EXTERNAL tier: someone used the panel/app, watch for follow-up changes
            target_interval = EXTERNAL_CHANGE_INTERVAL
            tier = "external"
        elif self._is_spa_active(data):
            # ACTIVE tier: heater, filter or bubble/jet running
            target_interval = ACTIVE_SCAN_INTERVAL
            tier = "active"
            self._last_state_change_time = current_time
        else:
            # IDLE tier: nothing running, check if stable long enough
            time_since_change = current_time - self._last_state_change_time if self._last_state_change_time else IDLE_STABLE_THRESHOLD + 1
            if time_since_change >= IDLE_STABLE_THRESHOLD:
                target_interval = IDLE_SCAN_INTERVAL
                tier = "idle"
            else:
                # Recently stopped — stay at active rate briefly
                target_interval = ACTIVE_SCAN_INTERVAL
                tier = "active"

        # Only log and update when the interval actually changes
        current_interval = self.update_interval.total_seconds()
        if current_interval != target_interval:
            self.update_interval = timedelta(seconds=target_interval)
            _LOGGER.info("Polling tier: %s (%ds interval)", tier, target_interval)

    @staticmethod
    def _is_spa_active(data: dict) -> bool:
        """Return True if any spa component is actively running."""
        return (
            data.get("heater") == "on"
            or data.get("filter") == "on"
            or data.get("bubble") == "on"
            or data.get("jet") == "on"
        )

    def _enable_rapid_polling(self, expected_changes: dict, raw_command: dict = None) -> None:
        """Enable rapid polling and track expected changes."""
        self._pending_changes.update(expected_changes)
        if raw_command:
            self._pending_raw_command.update(raw_command)
            self._command_retry_count = 0
        self._rapid_poll_until = time.time() + RAPID_POLL_TIMEOUT
        self.update_interval = timedelta(seconds=RAPID_SCAN_INTERVAL)
        _LOGGER.debug("Rapid polling enabled, waiting for changes: %s", expected_changes)

    async def _check_power_cycle(self, data: dict) -> None:
        """Check for power cycle and restore state if enabled.
        
        Uses multiple detection methods:
        1. is_online transition from False to True
        2. Multiple simultaneous parameter changes indicating a reset
        3. Temperature unit reverting to default (typically F/1)
        """
        current_is_online = data.get("is_online", True)
        power_cycle_detected = False
        detection_method = ""
        
        # Method 1: Track is_online transitions
        if self._last_is_online is not None:
            # Detect power off transition (True → False)
            if self._last_is_online and not current_is_online:
                _LOGGER.info("🔌 MSpa power OFF detected (is_online: True → False)")
                # Save current state before power off
                self._saved_state = {
                    "heater": data.get("heater"),
                    "target_temperature": data.get("target_temperature"),
                    "filter": data.get("filter"),
                    "temperature_unit": data.get("temperature_unit"),
                    "ozone": data.get("ozone"),
                    "uvc": data.get("uvc"),
                }
                _LOGGER.info(f"💾 Saved state for restoration: {self._saved_state}")
            
            # Detect power on transition (False → True)
            elif not self._last_is_online and current_is_online:
                power_cycle_detected = True
                detection_method = "is_online transition (False → True)"
                _LOGGER.info(f"⚡ MSpa power ON detected via {detection_method}")
        
        # Method 2: Detect multiple simultaneous changes suggesting a reset
        # This helps catch quick power cycles that we might miss with is_online
        if self._last_snapshot and not power_cycle_detected:
            changes_detected = []
            
            # Check for key parameters reverting to defaults
            if self._last_snapshot.get("temperature_unit") == 0 and data.get("temperature_unit") == 1:
                changes_detected.append("temp_unit_reset_to_F")
            
            if self._last_snapshot.get("heater") == "on" and data.get("heater") == "off":
                changes_detected.append("heater_off")
            
            if self._last_snapshot.get("filter") == "on" and data.get("filter") == "off":
                changes_detected.append("filter_off")
            
            if self._last_snapshot.get("ozone") == "on" and data.get("ozone") == "off":
                changes_detected.append("ozone_off")
            
            if self._last_snapshot.get("uvc") == "on" and data.get("uvc") == "off":
                changes_detected.append("uvc_off")
            
            # If multiple things turned off simultaneously, it's likely a power cycle
            if len(changes_detected) >= 2:
                power_cycle_detected = True
                detection_method = f"multiple simultaneous changes: {', '.join(changes_detected)}"
                _LOGGER.warning(f"⚡ Possible power cycle detected via {detection_method}")
                _LOGGER.info("💡 TIP: If this is a false positive, please report it with the changes detected")
        
        # Store current state as snapshot for next comparison
        self._last_snapshot = {
            "temperature_unit": data.get("temperature_unit"),
            "heater": data.get("heater"),
            "filter": data.get("filter"),
            "ozone": data.get("ozone"),
            "uvc": data.get("uvc"),
            "target_temperature": data.get("target_temperature"),
        }
        
        # Handle power cycle restoration
        if power_cycle_detected:
            self._power_cycle_detected = True
            
            # Check config options
            track_unit = self.config_entry.options.get(CONF_TRACK_TEMPERATURE_UNIT, False)
            restore_enabled = self.config_entry.options.get(CONF_RESTORE_STATE, False)
            
            _LOGGER.info(f"🔧 Config: track_temperature_unit={track_unit}, restore_state={restore_enabled}")
            
            # Handle temperature unit tracking (independent of restore_state)
            if track_unit:
                # Set temperature unit based on HA unit system
                ha_unit = self.hass.config.units.temperature_unit
                desired_unit = 1 if ha_unit == UnitOfTemperature.FAHRENHEIT else 0
                current_unit = data.get("temperature_unit", 0)
                
                if current_unit != desired_unit:
                    unit_name = "Fahrenheit" if desired_unit == 1 else "Celsius"
                    _LOGGER.info(f"🌡️ Setting MSpa temperature unit to {unit_name} to match HA system")
                    try:
                        await self.set_temperature_unit(desired_unit)
                    except Exception as err:
                        _LOGGER.error(f"❌ Failed to set temperature unit: {err}")
            
            # Handle state restoration (independent of track_unit)
            if restore_enabled:
                if self._saved_state:
                    _LOGGER.info("♻️ Starting state restoration after power cycle")
                    await self._restore_saved_state()
                else:
                    _LOGGER.warning("⚠️ No saved state available for restoration (device may have been off during HA restart)")
        
        # Update last is_online state
        self._last_is_online = current_is_online
    
    async def _restore_saved_state(self) -> None:
        """Restore saved state after power cycle."""
        try:
            restored_items = []
            failed_items = []
            
            # Small delay to allow temperature unit to be set first
            await asyncio.sleep(2)
            
            _LOGGER.info(f"♻️ Restoring state from: {self._saved_state}")
            
            # Restore target temperature if saved
            if "target_temperature" in self._saved_state:
                temp = self._saved_state["target_temperature"]
                try:
                    _LOGGER.info(f"🌡️ Restoring target temperature to {temp}°C")
                    await self.api.set_temperature_setting(temp)
                    restored_items.append(f"temperature={temp}°C")
                    await asyncio.sleep(0.5)  # Small delay between commands
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore temperature: {err}")
                    failed_items.append(f"temperature: {err}")
            
            # Restore heater state if saved and was on
            if self._saved_state.get("heater") == "on":
                try:
                    _LOGGER.info("♨️ Restoring heater to ON")
                    await self.set_feature_state("heater", "on")
                    restored_items.append("heater=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore heater: {err}")
                    failed_items.append(f"heater: {err}")
            
            # Restore filter state if saved and was on
            if self._saved_state.get("filter") == "on":
                try:
                    _LOGGER.info("💨 Restoring filter to ON")
                    await self.set_feature_state("filter", "on")
                    restored_items.append("filter=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore filter: {err}")
                    failed_items.append(f"filter: {err}")
            
            # Restore ozone if saved and was on
            if self._saved_state.get("ozone") == "on":
                try:
                    _LOGGER.info("🫧 Restoring ozone to ON")
                    await self.set_feature_state("ozone", "on")
                    restored_items.append("ozone=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore ozone: {err}")
                    failed_items.append(f"ozone: {err}")
            
            # Restore UVC if saved and was on
            if self._saved_state.get("uvc") == "on":
                try:
                    _LOGGER.info("💡 Restoring UVC to ON")
                    await self.set_feature_state("uvc", "on")
                    restored_items.append("uvc=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore UVC: {err}")
                    failed_items.append(f"uvc: {err}")
            
            # Summary
            if restored_items:
                _LOGGER.info(f"✅ State restoration completed: {', '.join(restored_items)}")
            if failed_items:
                _LOGGER.error(f"❌ Failed to restore some items: {', '.join(failed_items)}")
            if not restored_items and not failed_items:
                _LOGGER.info("ℹ️ No items to restore (all were OFF)")
            
        except Exception as err:
            _LOGGER.error(f"❌ State restoration failed with error: {err}", exc_info=True)
    
    async def _enforce_temperature_unit(self, data: dict) -> None:
        """Enforce temperature unit to match HA system if always_enforce_unit is enabled.
        
        This is useful for devices that forget temperature unit setting even without a full power cycle.
        """
        always_enforce = self.config_entry.options.get(CONF_ALWAYS_ENFORCE_UNIT, False)
        
        if not always_enforce:
            return
        
        # Don't enforce immediately after a detected power cycle (already handled there)
        if self._power_cycle_detected:
            self._power_cycle_detected = False
            return
        
        # Get desired unit from HA system
        ha_unit = self.hass.config.units.temperature_unit
        desired_unit = 1 if ha_unit == UnitOfTemperature.FAHRENHEIT else 0
        current_unit = data.get("temperature_unit", 0)
        
        # Only enforce if there's a mismatch
        if current_unit != desired_unit:
            unit_name = "Fahrenheit" if desired_unit == 1 else "Celsius"
            _LOGGER.info(f"🌡️ Enforcing temperature unit: {unit_name} (always_enforce_unit enabled)")
            try:
                await self.set_temperature_unit(desired_unit)
            except Exception as err:
                _LOGGER.error(f"❌ Failed to enforce temperature unit: {err}")
    
    @property
    def last_data(self) -> dict:
        return self._last_data
