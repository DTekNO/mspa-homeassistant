"""Button platform for MSpa integration — cancel the heat schedule."""
import logging
import time

from homeassistant.const import EntityCategory

from .const import DOMAIN
from .entity import MSpaButtonEntity

_LOGGER = logging.getLogger(__name__)

# Presses are ignored for this long after the entity appears.  A cancel is
# destructive and silent — it clears a plan the user may still want — so it must not
# be possible for a restart or a reload to trigger one.  The window covers an
# automation that fires on `homeassistant.start`, and it means correctness does not
# depend on DATETIME preceding BUTTON in PLATFORMS, which is what currently
# guarantees the schedule is restored before this entity exists.
_PRESS_GRACE_SECONDS = 20


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

    async def async_get_last_state(self):
        """Never restore a previous last-press timestamp.

        A ButtonEntity's state *is* the timestamp of its last press, and
        `ButtonEntity.async_internal_added_to_hass` restores it across restarts so the
        UI can show "last pressed 3 days ago".  For a cancel button that is worthless,
        and it actively misleads: the entity is `unavailable` while no schedule exists,
        so the moment one is applied it flips to that restored timestamp — a state
        change the logbook narrates as **"Pressed"**.

        Observed 2026-08-10: the calendar automation set Scheduled for at 11:28:51 and
        the feed showed "Cancel Heat Schedule → Pressed" at 11:28:53. Nothing had been
        pressed and nothing was cancelled, but it was indistinguishable from the button
        having destroyed the schedule the automation had just set.

        `state` and `_async_press_action` are both `@final`, so the restore cannot be
        intercepted there. Returning None here is the supported way to decline it —
        ButtonEntity's restore is guarded by `if state is not None`. The state then
        stays `unknown` until a genuine press, so becoming available reveals nothing.

        Verified against homeassistant/components/button at core 72ca26b (2026-08-10).
        If a future core stops routing the restore through `async_get_last_state`, the
        phantom press returns — the symptom to look for is the logbook reporting a
        press at the moment a schedule is applied.
        """
        return None

    @property
    def available(self) -> bool:
        """Offered only when there is something to cancel.

        Safe now that no timestamp is restored: becoming available exposes `unknown`
        rather than a stale press time. A press after a genuine one in the same
        session can still re-surface that timestamp on the next availability change,
        but it is then a true record rather than a fabricated one.
        """
        return (
            super().available
            and getattr(self.coordinator, "scheduled_ready_at", None) is not None
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._added_at = time.monotonic()

    async def async_press(self) -> None:
        # Every outcome is logged.  The activity feed only records that the button
        # was "Pressed", which cannot answer whether a schedule was actually
        # cleared — the question that matters when a plan goes missing.
        added_at = getattr(self, "_added_at", None)
        if added_at is not None and time.monotonic() - added_at < _PRESS_GRACE_SECONDS:
            _LOGGER.warning(
                "Cancel Heat Schedule pressed %.0f s after startup — ignored. "
                "Cancelling is destructive, so presses within %d s of a restart or "
                "reload are refused in case a schedule is still being restored.",
                time.monotonic() - added_at, _PRESS_GRACE_SECONDS,
            )
            return

        scheduled = getattr(self.coordinator, "scheduled_ready_at", None)
        if scheduled is None:
            _LOGGER.info("Cancel Heat Schedule pressed — nothing scheduled, no change")
            return

        _LOGGER.info("Cancel Heat Schedule pressed — clearing the schedule set for %s",
                     scheduled)
        self.coordinator.clear_schedule("cancelled by user")
        # The schedule is persisted by the Scheduled for entity's own retained
        # state, not by the rates store, so pushing listeners is what makes the
        # cancel stick: that entity re-renders as unknown, and its restore path
        # only reinstates a *future* value.
        self.coordinator.async_update_listeners()
