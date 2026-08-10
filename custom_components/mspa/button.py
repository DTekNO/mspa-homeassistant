"""Button platform for MSpa integration — cancel the heat schedule."""
import logging

from homeassistant.const import EntityCategory

from .const import DOMAIN
from .entity import MSpaButtonEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MSpaCancelHeatSchedule(coordinator)])


class MSpaCancelHeatSchedule(MSpaButtonEntity):
    """Clear a pending heat schedule.

    Home Assistant offers no gesture for clearing a datetime entity — the picker
    always yields a value and `datetime.set_value` requires one — so setting
    **Scheduled for** was a one-way door: the time could be changed but never
    withdrawn.  Editing the date to a past day was the only route, and that fired
    the trigger on the way out and switched the heater on.

    A button is the right shape for this rather than a switch: cancelling is an
    action, not a state.  A switch would need an answer to "what does *off* mean
    when no schedule exists?" and could be left in a position that contradicts the
    datetime entity.

    Pressing it clears the schedule and everything derived from it, and
    deliberately leaves the heater alone — see coordinator.clear_schedule.
    """

    name = "Cancel Heat Schedule"
    icon = "mdi:calendar-remove"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_device_info = self.device_info
        self._attr_unique_id = (
            f"mspa_cancel_heat_schedule_{getattr(coordinator, 'device_id', 'unknown')}"
        )

    @property
    def available(self) -> bool:
        """Only offered when there is something to cancel.

        Pressing it with no schedule set would be a no-op, and a control that does
        nothing is worse than one that is visibly not applicable.
        """
        return (
            super().available
            and getattr(self.coordinator, "scheduled_ready_at", None) is not None
        )

    async def async_press(self) -> None:
        self.coordinator.clear_schedule("cancelled by user")
        # The schedule is persisted by the Scheduled for entity's own retained
        # state, not by the rates store, so pushing listeners is what makes the
        # cancel stick: that entity re-renders as unknown, and its restore path
        # only reinstates a *future* value.
        self.coordinator.async_update_listeners()
