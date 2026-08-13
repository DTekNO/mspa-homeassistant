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

    def _describe_caller(self) -> str:
        """Best-effort identification of whatever asked for this change.

        Home Assistant sets the entity's context immediately before invoking a
        service, so `user_id` names a person acting through the UI and `parent_id`
        links to the automation or script that caused it.  Automations stamp their
        own state with the same context when they run, so scanning states for a
        matching id usually recovers a friendly name.

        Best effort by design: a direct websocket or REST call carries neither, and
        the parent's state may already have moved on.  Reporting the raw ids anyway
        means they can still be matched against a trace by hand.

        Added 2026-08-13, after a `filter: off` arrived 2.5 minutes before a
        scheduled heat start with nothing in the log to say where it came from.
        """
        ctx = getattr(self, "_context", None)
        if ctx is None:
            return "no context (internal call)"
        if getattr(ctx, "user_id", None):
            return f"user_id={ctx.user_id}"
        parent = getattr(ctx, "parent_id", None)
        if parent:
            try:
                for state in self.hass.states.async_all():
                    if state.context.id == parent:
                        return f"{state.entity_id} (parent_id={parent})"
            except Exception:                     # never let tracing break a command
                pass
            return f"parent_id={parent} (no matching entity)"
        return f"context_id={getattr(ctx, 'id', None)}, no user or parent"

    async def async_turn_on(self, **kwargs):
        _LOGGER.info("Switch: %s → on, requested by %s",
                     self.feature, self._describe_caller())
        await self.coordinator.set_feature_state(feature=self.feature, state="on")

    async def async_turn_off(self, **kwargs):
        # Emphasised for the pump: turning the filter off makes the API layer send a
        # second command turning the heater off too, so one stray call can end a
        # heating session rather than merely stopping circulation.
        level = _LOGGER.warning if self.feature == "filter" else _LOGGER.info
        level("Switch: %s → off, requested by %s%s",
              self.feature, self._describe_caller(),
              " — this also forces the heater off" if self.feature == "filter" else "")
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