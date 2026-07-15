"""Datetime platform for MSpa integration — scheduled ready time."""
import logging
from datetime import datetime

from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import MSpaDateTimeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MSpaScheduledReadyAt(coordinator)])


class MSpaScheduledReadyAt(MSpaDateTimeEntity, RestoreEntity):
    """Scheduled ready-at time for the spa.

    Appears in the device panel under Controls.  Set it to when you want the
    spa to be ready; the Heat Schedule sensor uses it to work out when
    conditioning must start.  Value persists across HA restarts.
    """

    name = "Scheduled for"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"mspa_scheduled_ready_at_{getattr(coordinator, 'device_id', 'unknown')}"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.scheduled_ready_at

    async def async_set_value(self, value: datetime) -> None:
        self.coordinator.scheduled_ready_at = value
        self.coordinator.ready_latched = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            restored = dt_util.parse_datetime(last_state.state)
            if restored is not None:
                self.coordinator.scheduled_ready_at = restored
