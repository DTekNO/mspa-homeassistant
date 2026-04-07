"""DataUpdateCoordinator for MSpa integration."""
import logging
from datetime import timedelta
from .mspa_api import MSpaApiClient

from typing import Any, Dict
import asyncio

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
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

from homeassistant.const import ATTR_STATE, ATTR_TEMPERATURE


_LOGGER = logging.getLogger(__name__)

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
        self._pending_changes = {}  # Track expected changes
        self._last_heat_state = None  # Track heat state changes
        self._last_is_online = None  # Track power on/off transitions
        self._saved_state = {}  # Store state before power off for restoration
        self._last_snapshot = {}  # Store last known state for change detection
        self._power_cycle_detected = False  # Flag to track if we just detected a power cycle

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
            
            # Bubble state requires level parameter
            if feature == "bubble":
                await api_method(numerical_state, self._last_data.get("bubble_level", 1))
            else:
                await api_method(numerical_state)
            
            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling({feature: state})
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
            self._enable_rapid_polling({"target_temperature": temperature})
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
            await self.api.set_bubble_state(numerical_state, self._last_data.get("bubble_level", 1))
            
            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling({"bubble": bubble_state})
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
            self._enable_rapid_polling({"bubble_level": bubble_level})
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
            self._enable_rapid_polling({"temperature_unit": unit})
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
        from datetime import datetime
        import time
        
        current_time = time.time()
        should_rapid_poll = False
        
        # Check if any pending changes have been confirmed
        if self._pending_changes:
            confirmed = []
            for key, expected_value in list(self._pending_changes.items()):
                if data.get(key) == expected_value:
                    _LOGGER.debug(f"Pending change confirmed: {key} = {expected_value}")
                    confirmed.append(key)
            
            # Remove confirmed changes
            for key in confirmed:
                del self._pending_changes[key]
            
            # Continue rapid polling if there are still pending changes
            if self._pending_changes:
                should_rapid_poll = True
                _LOGGER.debug(f"Still waiting for changes: {self._pending_changes}")
        
        # Check if we're in preheat mode (heat_state == 2)
        current_heat_state = data.get("heat_state")
        if current_heat_state == 2 and data.get("heater") == "on":
            _LOGGER.debug("Preheat mode detected, enabling rapid polling")
            should_rapid_poll = True
        
        # Track heat state transitions
        if self._last_heat_state != current_heat_state:
            _LOGGER.debug(f"Heat state changed: {self._last_heat_state} -> {current_heat_state}")
            self._last_heat_state = current_heat_state
        
        # Check if rapid poll timeout has expired
        if self._rapid_poll_until and current_time > self._rapid_poll_until:
            _LOGGER.debug("Rapid poll timeout expired, returning to normal polling")
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
    
    def _enable_rapid_polling(self, expected_changes: dict) -> None:
        """Enable rapid polling and track expected changes."""
        import time
        
        self._pending_changes.update(expected_changes)
        self._rapid_poll_until = time.time() + RAPID_POLL_TIMEOUT
        self.update_interval = timedelta(seconds=RAPID_SCAN_INTERVAL)
        _LOGGER.debug(f"Rapid polling enabled, waiting for changes: {expected_changes}")
    
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
