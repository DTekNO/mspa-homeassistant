from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.datetime import DateTimeEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
import logging

_LOGGER = logging.getLogger(__name__)

class MSpaBaseEntity:
    _attr_has_entity_name = True

    def __init__(self, coordinator):
        _LOGGER.debug("Initializing %s", self.__class__.__name__)
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._attr_name = f"mspa {self.name}".strip()
        _LOGGER.debug("internal name set to: %s", self._attr_name)

    @property
    def device_info(self):
        name = f"MSpa {getattr(self.coordinator, 'series', 'unknown')} {getattr(self.coordinator, 'device_alias', getattr(self.coordinator, 'model', 'unknown model'))}"
        wifi = getattr(self.coordinator, "wifi_version", None) or ""
        mcu = getattr(self.coordinator, "mcu_version", None) or ""
        if mcu.startswith("mcu-"):
            mcu = mcu[4:]
        fw_version = f"{wifi}-{mcu}" if wifi and mcu else wifi or mcu or "unknown"
        return {
            "identifiers": {(DOMAIN, getattr(self.coordinator, "device_id", "mspa_hottub"))},
            "connections": {(dr.CONNECTION_NETWORK_MAC, getattr(self.coordinator, "mac_address"))} if getattr(self.coordinator, "mac_address", None) else set(),
            "manufacturer": "MSpa",
            "model": getattr(self.coordinator, "model", None),
            "name": name,
            "sw_version": fw_version,
            "serial_number": getattr(self.coordinator, "serial_number", None) or None,
        }

    # No entity_picture here. Every entity type inherits from this class — sensors,
    # switches, climate, numbers, buttons — and Home Assistant shows a picture in
    # preference to an icon, so supplying one here replaced the icon on every row of the
    # device panel with the same photograph of the spa, leaving nothing to tell the rows
    # apart.
    #
    # Two entities declare their own, because `entity_picture` is the key the picture
    # cards read and there has to be something they can be pointed at: climate, and the
    # water temperature sensor (a picture card with the reading in the footer, as the
    # MSpa Link app shows it). Those two rows trade their icon for the photo; the rest
    # keep theirs.

    @property
    def available(self):
        """Return True if entity is available."""
        # Check if coordinator has data and if device is online
        if not self.coordinator.last_update_success:
            return False
        
        # Check if device is online based on is_online flag or ConnectType
        is_online = self.coordinator._last_data.get("is_online", True)
        connect_type = self.coordinator._last_data.get("ConnectType", "")
        
        # Device is unavailable if explicitly offline or if ConnectType is "offline"
        if is_online is False or connect_type == "offline":
            return False
        
        return True


class MSpaSensorEntity(MSpaBaseEntity, CoordinatorEntity, SensorEntity):
    pass

class MSpaClimateEntity(MSpaBaseEntity, CoordinatorEntity, ClimateEntity):
    pass

class MSpaBinarySensorEntity(MSpaBaseEntity, CoordinatorEntity, BinarySensorEntity):
    pass

class MSpaSwitchEntity(MSpaBaseEntity, CoordinatorEntity, SwitchEntity):
    pass

class MSpaNumberEntity(MSpaBaseEntity, CoordinatorEntity, NumberEntity):
    pass

class MSpaDateTimeEntity(MSpaBaseEntity, CoordinatorEntity, DateTimeEntity):
    pass

class MSpaButtonEntity(MSpaBaseEntity, CoordinatorEntity, ButtonEntity):
    pass
