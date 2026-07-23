from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import PRECISION_HALVES
from .const import DOMAIN, TEMP_UNIT, MAX_TEMP, MIN_TEMP
from .entity import MSpaClimateEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MSpaClimate(coordinator)])


class MSpaClimate(MSpaClimateEntity):
    """Representation of the MSpa climate control entity."""
    name = "Heater Control"

    _attr_precision = PRECISION_HALVES
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_temperature_unit = TEMP_UNIT

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_climate_{getattr(coordinator, 'device_id', 'unknown')}"
        self._attr_icon = "mdi:hot-tub"
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
        self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
        )

    @property
    def current_temperature(self):
        return self.coordinator.last_data.get("water_temperature")

    @property
    def target_temperature(self):
        return self.coordinator.last_data.get("target_temperature")

    async def async_set_temperature(self, **kwargs):
        temperature = kwargs.get("temperature")
        if temperature is not None:
            await self.coordinator.set_temperature(type("ServiceCall", (), {"data": {"temperature": temperature}})())

    @property
    def hvac_mode(self):
        return HVACMode.HEAT if self.coordinator.last_data.get("heater") == "on" else HVACMode.OFF

    @property
    def hvac_action(self):
        heat_state = self.coordinator.last_data.get("heat_state")
        if heat_state == 2:
            return HVACAction.PREHEATING  # You may need to define this if not present
        if heat_state == 3:
            return HVACAction.HEATING
        if heat_state == 4:
            return HVACAction.IDLE
        return HVACAction.OFF

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.HEAT:
            await self.coordinator.set_feature_state("heater", "on")
        elif hvac_mode == HVACMode.OFF:
            await self.coordinator.set_feature_state("heater", "off")
