import logging

from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, DEFAULT_SCHEDULE_TARGET_TEMP
from .entity import MSpaNumberEntity, MSpaBaseEntity
from .coordinator import MSpaUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: MSpaUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        MspaBubbleLevelNumber(coordinator),
        MSpaScheduleTargetTemp(coordinator),
    ])

class MspaBubbleLevelNumber(MSpaNumberEntity):
    """Representation of the MSpa bubble level number entity."""

    name = "Bubble Level"
    icon = "mdi:chart-bubble"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_bubble_level_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_native_min_value = 1
        self._attr_native_max_value = 3
        self._attr_native_step = 1

    @property
    def native_value(self):
        return self.coordinator._last_data.get("bubble_level", 1)

    async def async_set_native_value(self, value: int):
        value = max(self._attr_native_min_value, min(self._attr_native_max_value, int(value)))
        _LOGGER.debug("Setting bubble level to %d", value)
        await self.coordinator.set_bubble_level(type("ServiceCall", (), {"data": {"level": value}})())
        await self.coordinator.async_request_refresh()


class MSpaScheduleTargetTemp(MSpaNumberEntity, RestoreEntity):
    """Target temperature the spa should reach by the scheduled ready time.

    Appears in the device panel under Configuration.  Changing this immediately
    updates the Heat Schedule sensor's computed start time and the autonomous
    heating trigger in the coordinator.  Value persists across HA restarts.
    """

    name = "Schedule target temperature"
    _attr_icon = "mdi:thermometer-auto"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 20.0
    _attr_native_max_value = 40.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "°C"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_schedule_target_temp_{getattr(coordinator, 'device_id', 'unknown')}"

    @property
    def native_value(self) -> float:
        return self.coordinator.schedule_target_temp

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.schedule_target_temp = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            try:
                self.coordinator.schedule_target_temp = float(last_state.state)
            except (ValueError, TypeError):
                pass
