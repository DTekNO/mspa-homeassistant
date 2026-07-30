"""Datetime platform for MSpa integration — scheduled ready time."""
import logging
from datetime import datetime

from homeassistant.core import callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import MSpaDateTimeEntity

_LOGGER = logging.getLogger(__name__)

# Debounce window for async_set_value.  The HA datetime picker fires one call
# per field (date, then time), so the first call can produce a transient
# past datetime before the user finishes entering the time.  Holding off for
# this many seconds lets both field changes arrive before we commit anything
# to the coordinator — preventing a spurious auto-clear on the next poll.
_SET_DEBOUNCE_SECONDS = 5


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    """True when both values refer to the same moment.

    Tolerates a naive/aware mix rather than raising, since the value can arrive
    from the HA datetime picker, a service call template, or restored state.
    """
    if a is None or b is None:
        return False
    if (a.tzinfo is None) != (b.tzinfo is None):
        return a.replace(tzinfo=None) == b.replace(tzinfo=None)
    return a == b


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
        self._debounce_cancel = None   # cancel handle from async_call_later
        self._pending_value: datetime | None = None  # value waiting to be committed

    def _current_target(self) -> datetime | None:
        """The time this entity is currently presenting.

        While a debounce is in flight that is the user's latest pick, not the
        stale coordinator value — giving immediate UI feedback even though the
        coordinator has not been updated yet.
        """
        if self._debounce_cancel is not None and self._pending_value is not None:
            return self._pending_value
        return self.coordinator.scheduled_ready_at

    @property
    def native_value(self) -> datetime | None:
        return self._current_target()

    async def async_set_value(self, value: datetime) -> None:
        # Re-asserting the time that is already set must be a no-op.  Committing
        # clears _schedule_triggered and ready_latched, so an automation that
        # re-syncs the schedule on a timer (e.g. from a calendar every 15 min)
        # would otherwise re-arm the scheduler mid-heat-up: the trigger re-fires,
        # the setpoint command is resent, and the Heat Schedule sensor drops out
        # of "Heating" back to a start-time state on every run.
        if _same_instant(self._current_target(), value):
            _LOGGER.debug(
                "Heat schedule: ready time re-asserted unchanged (%s) — ignoring",
                value.strftime("%Y-%m-%d %H:%M"),
            )
            return

        # Cancel any previously scheduled commit (restart the debounce window).
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._debounce_cancel = None

        self._pending_value = value
        # Show the pick immediately in the UI while we wait for the second field.
        self.async_write_ha_state()

        self._debounce_cancel = async_call_later(
            self.hass, _SET_DEBOUNCE_SECONDS, self._commit_pending_value
        )

    @callback
    def _commit_pending_value(self, _now: datetime) -> None:
        """Apply the debounced value to the coordinator."""
        self._debounce_cancel = None
        value = self._pending_value
        if value is None:
            return

        old = self.coordinator.scheduled_ready_at
        sched_temp = getattr(self.coordinator, "schedule_target_temp", None)
        data = getattr(self.coordinator, "_last_data", {}) or {}
        try:
            water = float(data.get("water_temperature"))
            water_str = f"{water:.1f}"
        except (TypeError, ValueError):
            water_str = "?"
        old_str = old.strftime("%Y-%m-%d %H:%M") if old is not None else "none"
        _LOGGER.info(
            "Heat schedule: ready time set → %s  [was=%s  sched=%.1f°C  water=%s°C]",
            value.strftime("%Y-%m-%d %H:%M"),
            old_str,
            sched_temp or 0.0,
            water_str,
        )
        self.coordinator.scheduled_ready_at = value
        self.coordinator.ready_latched = False
        self.coordinator._schedule_triggered = False
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()  # push new state to sensors immediately

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            restored = dt_util.parse_datetime(last_state.state)
            if restored is not None and restored > dt_util.now():
                self.coordinator.scheduled_ready_at = restored
                sched_temp = getattr(self.coordinator, "schedule_target_temp", None)
                _LOGGER.info(
                    "Heat schedule: restored from prior state → %s  [sched=%.1f°C]",
                    restored.strftime("%Y-%m-%d %H:%M"),
                    sched_temp or 0.0,
                )
