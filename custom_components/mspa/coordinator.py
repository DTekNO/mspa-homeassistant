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

from .const import (
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    RAPID_SCAN_INTERVAL,
    RAPID_POLL_TIMEOUT,
    CONF_TRACK_TEMPERATURE_UNIT,
    CONF_RESTORE_STATE,
    CONF_ALWAYS_ENFORCE_UNIT,
)

import time
from homeassistant.const import ATTR_STATE, ATTR_TEMPERATURE


_LOGGER = logging.getLogger(__name__)

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
        self._pending_changes = {}  # Track expected changes (transformed keys)
        self._pending_raw_command = {}  # Raw API payload for retrying unconfirmed commands
        self._command_retry_count = 0  # Number of retries attempted for current command
        self._last_heat_state = None  # Track heat state changes
        self._last_is_online = None  # Track power on/off transitions
        self._saved_state = {}  # Store state before power off for restoration
        self._last_snapshot = {}  # Store last known state for change detection
        self._power_cycle_detected = False  # Flag to track if we just detected a power cycle

        # Computed heating rate tracking (°C / hour, exponential moving average).
        # Updated on every poll where the heater is in full-heat mode (heat_state == 3)
        # and at least _MIN_RATE_SAMPLE_HOURS have elapsed since the last valid sample.
        # Outliers (caused by adding hot/cold water, sensor noise etc.) are rejected.
        self.computed_heat_rate: float | None = None  # °C/h, None until enough data
        self._rate_last_temp: float | None = None     # °C at last accepted sample
        self._rate_last_time: float | None = None     # monotonic seconds at last sample

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

        # Prediction accuracy tracker.  Records the initial estimate at the
        # start of a big heating session and compares it to the actual elapsed
        # time when the target is reached.  Results are logged at INFO level
        # (grep for "PREDICTION_RESULT") and the last 10 are persisted in the
        # rates store under the key "prediction_history".
        self._prediction: dict | None = None   # active prediction (see _start_prediction)
        self._prediction_history: list[dict] = []  # last 10 completed predictions

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
            if (new_temp is not None and new_target is not None
                    and new_target > new_temp
                    and (new_target - new_temp) > 2.0
                    and self._prediction is None):
                # Lazy import to reuse the sensor's segmented calculation.
                from .sensor import _segmented_heating_minutes
                est_minutes = _segmented_heating_minutes(new_temp, new_target, self)
                if est_minutes is not None:
                    self._prediction = {
                        "start_time": datetime.now(timezone.utc).isoformat(),
                        "start_temp": new_temp,
                        "target_temp": new_target,
                        "estimated_minutes": round(est_minutes, 1),
                        "session_scalar": self._session_scalar,
                    }
                    _LOGGER.info(
                        "PREDICTION_START: %.1f°C → %.1f°C, estimated %.0f min (scalar=%.3f)",
                        new_temp, new_target, est_minutes, self._session_scalar,
                    )

            # Finish tracking when near_target is newly reached.
            if (self.near_target and self._prediction is not None):
                start_iso = self._prediction["start_time"]
                start_dt = datetime.fromisoformat(start_iso)
                actual_minutes = (datetime.now(timezone.utc) - start_dt).total_seconds() / 60
                est = self._prediction["estimated_minutes"]
                error_minutes = actual_minutes - est
                error_pct = (error_minutes / actual_minutes * 100) if actual_minutes > 0 else 0
                result = {
                    **self._prediction,
                    "actual_minutes": round(actual_minutes, 1),
                    "error_minutes": round(error_minutes, 1),
                    "error_percent": round(error_pct, 1),
                    "end_time": datetime.now(timezone.utc).isoformat(),
                }
                self._prediction_history.append(result)
                self._prediction_history = self._prediction_history[-10:]  # keep last 10
                self._prediction = None
                _LOGGER.info(
                    "PREDICTION_RESULT: %.1f°C → %.1f°C | estimated %.0f min, actual %.0f min | error %+.0f min (%+.1f%%)",
                    result["start_temp"], result["target_temp"],
                    est, actual_minutes, error_minutes, error_pct,
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
                    self.near_target = True
                elif delta >= _NEAR_TARGET_ACTIVATE:
                    self.near_target = False
            # else: no temp data — leave flag unchanged

            # Load persisted rates on the very first poll after startup/reload.
            if not self._rates_loaded:
                self._rates_loaded = True
                stored = await self._rates_store.async_load()
                if stored:
                    self.computed_heat_rate = stored.get("heat_rate")
                    self.computed_cool_rate = stored.get("cool_rate")
                    stored_buckets = stored.get("heat_rate_buckets")
                    self._prediction_history = stored.get("prediction_history", [])
                    if isinstance(stored_buckets, list) and len(stored_buckets) == 3:
                        save_ts = stored.get("bucket_save_ts")
                        if save_ts is not None:
                            days_elapsed = (time.time() - save_ts) / 86400
                            # Decay each bucket toward the global flat EMA over time so
                            # stale seasonal rates don't anchor predictions indefinitely.
                            # 0.98/day: ~75% weight at 14 days, ~55% at 30 days, floor 0.4.
                            decay = max(0.4, 0.98 ** days_elapsed)
                            global_rate = self.computed_heat_rate
                            self.heat_rate_buckets = [
                                (float(b) * decay + global_rate * (1 - decay))
                                if b is not None and global_rate is not None
                                else (float(b) * decay if b is not None else None)
                                for b in stored_buckets
                            ]
                            _LOGGER.debug(
                                "Loaded buckets: %.1f days old, decay=%.3f → %s",
                                days_elapsed, decay, self.heat_rate_buckets,
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

            # --- Observed heating rate tracking ---
            # Only sample when in full-heat mode to avoid preheat / cooling skew.
            # Bounds are intentionally generous: even a tiny spa at max power can't
            # exceed ~5 °C/h, and below 0.05 °C/h the signal is just sensor noise.
            _MIN_RATE_SAMPLE_HOURS = 3 / 60   # 3 minutes minimum between samples
            _MIN_HEAT_RATE = 0.05             # °C/h  — below this is noise / flat
            _MAX_HEAT_RATE = 3.0              # °C/h  — above this is an outlier (no real spa heats faster than ~2.5°C/h)
            _EMA_ALPHA = 0.25                 # smoothing factor (lower = slower to adapt)

            curr_temp = transformed_data.get("water_temperature")
            heat_state = transformed_data.get("heat_state")
            now_mono = time.monotonic()

            # Detect new heating session.  Only reset the session scalar when
            # the heater newly engages AND delta-to-target is large (> 2°C).
            # This avoids false resets from thermostat cycling near setpoint.
            heater_now_active = (heat_state == 3)
            if heater_now_active and not self._heat_was_active:
                delta_to_target = abs((new_target or 0) - (curr_temp or 0))
                if delta_to_target > 2.0:
                    self._session_scalar = 1.0
                    self._session_scalar_bucket = None
                    self._session_fresh_buckets = set()
                    _LOGGER.debug(
                        "MSpa: new heating session (delta %.1f°C) — session scalar reset",
                        delta_to_target,
                    )
            self._heat_was_active = heater_now_active

            if heat_state == 3 and curr_temp is not None:
                if self._rate_last_temp is None:
                    # First poll in heat mode — set anchor, no sample yet
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
                elif curr_temp != self._rate_last_temp:
                    # Temperature has changed — elapsed time since anchor is the true
                    # duration of the previous 0.5 °C step, giving a real rate.
                    elapsed_hours = (now_mono - self._rate_last_time) / 3600
                    if elapsed_hours >= _MIN_RATE_SAMPLE_HOURS:
                        delta = curr_temp - self._rate_last_temp
                        rate = delta / elapsed_hours  # guarded: elapsed_hours > 0
                        if _MIN_HEAT_RATE <= rate <= _MAX_HEAT_RATE:
                            if self.computed_heat_rate is None:
                                self.computed_heat_rate = rate
                            else:
                                self.computed_heat_rate = (
                                    _EMA_ALPHA * rate
                                    + (1 - _EMA_ALPHA) * self.computed_heat_rate
                                )
                            # Also update the temperature bucket for this observation.
                            # _rate_last_temp is the *start* temperature of the step.
                            _bi = 0 if self._rate_last_temp < 30.0 else 1 if self._rate_last_temp < 37.0 else 2
                            _bp = self.heat_rate_buckets[_bi]
                            self.heat_rate_buckets[_bi] = (
                                _EMA_ALPHA * rate + (1 - _EMA_ALPHA) * _bp
                            ) if _bp is not None else rate
                            self._session_fresh_buckets.add(_bi)
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
                            _LOGGER.debug(
                                "Heat rate sample %.3f °C/h (EMA → %.3f °C/h, bucket[%d] → %.3f °C/h)",
                                rate, self.computed_heat_rate,
                                _bi, self.heat_rate_buckets[_bi],
                            )
                        else:
                            _LOGGER.debug(
                                "Heat rate sample %.3f °C/h rejected (out of bounds)", rate
                            )
                    # Always advance anchor to new temperature (even if rate rejected).
                    # This prevents a rejected outlier span from being re-used.
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
                # else: temperature unchanged — let elapsed time accumulate, don’t touch anchor
            else:
                # Heater off/preheat — reset anchor so next on-cycle starts fresh
                self._rate_last_temp = None
                self._rate_last_time = None

            # --- Observed cooling rate tracking ---
            # Sample passively when heater is not active and temperature is dropping.
            _MIN_COOL_RATE = 0.01  # °C/h — below this is sensor drift
            _MAX_COOL_RATE = 3.0   # °C/h — above this is unusual for an insulated spa

            if heat_state not in (2, 3) and curr_temp is not None:
                if self._cool_last_temp is None:
                    # First poll in cooling mode — set anchor, no sample yet
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
                                _LOGGER.debug(
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
                # Actively heating — reset cooling anchor so next off-cycle starts fresh
                self._cool_last_temp = None
                self._cool_last_time = None
            # --- end rate tracking ---

            # Persist updated rates so they survive reloads and HA restarts.
            await self._rates_store.async_save({
                "heat_rate": self.computed_heat_rate,
                "cool_rate": self.computed_cool_rate,
                "heat_rate_buckets": self.heat_rate_buckets,
                "bucket_save_ts": time.time(),
                "prediction_history": self._prediction_history,
            })

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


    # Map of features to their respective API methods
    FEATURE_API_MAP = {
        "heater": "set_heater_state",
        "filter": "set_filter_state",
        "bubble": "set_bubble_state",
        "jet": "set_jet_state",
        "ozone": "set_ozone_state",
        "uvc": "set_uvc_state",
    }

    async def set_feature_state(self, feature: str, state: str) -> None:
        """Set a feature state using the API map."""
        _LOGGER.debug(f"Setting MSpa feature {feature} to {state}")
        try:
            if state.lower() not in ["on", "off"]:
                raise ValueError("State must be 'on' or 'off'")
            numerical_state = 1 if state.lower() == "on" else 0
            api_method = getattr(self.api, self.FEATURE_API_MAP[feature])

            # Build raw command for potential retry
            if feature == "bubble":
                bubble_level = self._last_data.get("bubble_level", 1)
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
            bubble_level = self._last_data.get("bubble_level", 1)
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
            await self.api.set_bubble_level(bubble_level)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"bubble_level": bubble_level},
                {"bubble_level": bubble_level},
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
        """Check if we should enable or disable rapid polling."""
        import time

        current_time = time.time()
        should_rapid_poll = False

        # Check if any pending changes have been confirmed
        if self._pending_changes:
            confirmed = []
            for key, expected_value in list(self._pending_changes.items()):
                if data.get(key) == expected_value:
                    _LOGGER.debug("Pending change confirmed: %s = %s", key, expected_value)
                    confirmed.append(key)

            # Remove confirmed changes and prune their raw-command counterparts so
            # that the retry payload never re-sends already-acknowledged fields.
            for key in confirmed:
                del self._pending_changes[key]
                raw_key = _PENDING_TO_RAW_KEY.get(key)
                if raw_key:
                    self._pending_raw_command.pop(raw_key, None)

            if confirmed and not self._pending_changes:
                # All pending changes confirmed — reset retry state
                self._command_retry_count = 0

            # Continue rapid polling if there are still pending changes
            if self._pending_changes:
                should_rapid_poll = True
                _LOGGER.debug("Still waiting for changes: %s", self._pending_changes)

        # Track heat state transitions for logging only.
        # Preheat does not trigger rapid polling — a multi-minute preheat cycle does not
        # need sub-second resolution and the old behaviour caused repeated 15-call/15s
        # bursts every ~75s during preheat.
        current_heat_state = data.get("heat_state")
        if self._last_heat_state != current_heat_state:
            _LOGGER.debug("Heat state changed: %s -> %s", self._last_heat_state, current_heat_state)
            self._last_heat_state = current_heat_state

        # Check if rapid poll timeout has expired
        if self._rapid_poll_until and current_time > self._rapid_poll_until:
            if self._pending_changes:
                if self._command_retry_count < 1 and self._pending_raw_command:
                    # One retry: resend the original command and re-open the window
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
                    self._rapid_poll_until = current_time + RAPID_POLL_TIMEOUT
                    should_rapid_poll = True
                else:
                    # Retries exhausted — give up and return to normal polling
                    _LOGGER.warning(
                        "Spa did not confirm changes after retry: %s — giving up",
                        self._pending_changes,
                    )
                    self._pending_changes.clear()
                    self._pending_raw_command.clear()
                    self._command_retry_count = 0
                    self._rapid_poll_until = None
                    should_rapid_poll = False
            else:
                self._rapid_poll_until = None
                should_rapid_poll = False

        # Adjust polling interval
        if should_rapid_poll and not self._rapid_poll_until:
            # Start rapid polling
            self._rapid_poll_until = current_time + RAPID_POLL_TIMEOUT
            self.update_interval = timedelta(seconds=RAPID_SCAN_INTERVAL)
            _LOGGER.info("Enabled rapid polling (1s interval) for up to 15 seconds")
        elif not should_rapid_poll and self.update_interval.total_seconds() < DEFAULT_SCAN_INTERVAL:
            # Return to normal polling
            self._rapid_poll_until = None
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
            _LOGGER.info("Returned to normal polling (60s interval)")

    def _enable_rapid_polling(self, expected_changes: dict, raw_command: dict = None) -> None:
        """Enable rapid polling and track expected changes."""
        import time

        self._pending_changes.update(expected_changes)
        if raw_command:
            self._pending_raw_command.update(raw_command)
            self._command_retry_count = 0  # Reset retry count for new command
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
