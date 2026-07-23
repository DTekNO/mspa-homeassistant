from .const import DOMAIN
from .entity import MSpaSwitchEntity
from .coordinator import MSpaUpdateCoordinator
import logging

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the MSpa switch entities."""
    coordinator: MSpaUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        MSpaHeaterSwitch(coordinator),
        MSpaFilterSwitch(coordinator),
        MSpaBubbleSwitch(coordinator),
        MSpaJetSwitch(coordinator),
        MSpaOzoneSwitch(coordinator),
        MSpaUVCSwitch(coordinator)
    ]
    async_add_entities(entities, update_before_add=True)

class MSpaFeatureSwitch(MSpaSwitchEntity):
    feature = None
    icon = None
    name = None

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_icon = self.icon
        self._attr_name = f"mspa {self.name}".strip()
        self._attr_unique_id = f"mspa_{self.feature}_{getattr(coordinator, 'device_id', 'unknown')}"
        self.coordinator = coordinator
        _LOGGER.debug("MSpaFeatureSwitch initialized for feature: %s, name: %s", self.feature, self.name)

    @property
    def is_on(self):
        state = self.coordinator.last_data.get(self.feature)
        _LOGGER.debug("%s is %s", self.feature, state)
        return state == "on"

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("Turning on %s", self.feature)
        await self.coordinator.set_feature_state(feature=self.feature, state="on")

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("Turning off %s", self.feature)
        await self.coordinator.set_feature_state(feature=self.feature, state="off")

class MSpaHeaterSwitch(MSpaFeatureSwitch):
    feature = "heater"
    icon = "mdi:hot-tub"
    name = "Heater"

class MSpaFilterSwitch(MSpaFeatureSwitch):
    feature = "filter"
    icon = "mdi:air-filter"
    name = "Filter"

class MSpaJetSwitch(MSpaFeatureSwitch):
    feature = "jet"
    icon = "mdi:turbine"
    name = "Jet"

class MSpaOzoneSwitch(MSpaFeatureSwitch):
    feature = "ozone"
    icon = "mdi:weather-hazy"
    name = "Ozone"
    def __init__(self, coordinator):
        super().__init__(coordinator)
        coordinator.has_ozone_switch = True

class MSpaUVCSwitch(MSpaFeatureSwitch):
    feature = "uvc"
    icon = "mdi:weather-sunny-alert"
    name = "UVC"
    def __init__(self, coordinator):
        super().__init__(coordinator)
        coordinator.has_uvc_switch = True

class MSpaBubbleSwitch(MSpaFeatureSwitch):
    # Bubble switch for MSpa
    feature = "bubble"
    icon = "mdi:chart-bubble"
    name = "Bubble"