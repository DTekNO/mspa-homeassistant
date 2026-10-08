"""Binary sensor platform for MSpa — is the spa still answering?

Every other entity this integration provides goes `unavailable` the moment the spa
stops answering, which is precisely when something needs to say so. An automation
cannot trigger on the absence of a state, and `unavailable` is indistinguishable
from a restart, a reload, or Home Assistant itself starting up.

So this entity never goes unavailable. It reports connectivity as a state in its
own right, and it waits out the short outages that are not worth waking anyone for.
"""
import logging
from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_OFFLINE_AFTER_MINUTES,
    DEFAULT_OFFLINE_AFTER_MINUTES,
)
from .entity import MSpaBinarySensorEntity

_LOGGER = logging.getLogger(__name__)


def connectivity(last_seen_utc, now_utc, grace_seconds):
    """Resolve a last-seen time into (is_on, offline_seconds).

    `is_on` follows the CONNECTIVITY device class: True is connected. None means
    the spa has not been seen even once, which is not the same as being offline —
    a fresh install with bad credentials has never seen it either, and saying
    "disconnected" there would be a guess.

    The grace window is the whole reason this is a function rather than a
    comparison. Measured on this installation: the daily Starlink outage drops
    every entity for about 30 seconds, and on 07.10.2026 the vendor cloud dropped
    23 single polls in ten hours while the spa itself stayed on the wifi
    throughout. Neither is an outage anyone wants waking them, and both are
    minutes short of the real one that followed.
    """
    if last_seen_utc is None:
        return None, None
    offline = (now_utc - last_seen_utc).total_seconds()
    if offline < 0:
        offline = 0.0
    return offline <= grace_seconds, offline


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MSpaConnectivity(coordinator)])


class MSpaConnectivity(MSpaBinarySensorEntity):
    """Whether the spa has answered recently enough to call it connected.

    Pair it with a `for:` of your own on top of the grace window if you want to be
    even more patient. The attributes carry the raw material either way: when it
    was last seen, and how long it has been gone.
    """

    name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_device_info = self.device_info
        self._attr_unique_id = (
            f"mspa_connectivity_{getattr(coordinator, 'device_id', 'unknown')}"
        )

    @property
    def available(self) -> bool:
        """Always. An entity that reports outages cannot opt out of existing during one.

        This deliberately does not call `super().available`, which is False whenever
        the coordinator's last update failed — the exact condition this entity is
        here to describe.
        """
        return True

    @property
    def _grace_seconds(self) -> float:
        entry = getattr(self.coordinator, "config_entry", None)
        options = getattr(entry, "options", None) or {}
        try:
            minutes = float(options.get(
                CONF_OFFLINE_AFTER_MINUTES, DEFAULT_OFFLINE_AFTER_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_OFFLINE_AFTER_MINUTES
        if minutes <= 0:
            minutes = DEFAULT_OFFLINE_AFTER_MINUTES
        return minutes * 60.0

    @property
    def is_on(self):
        state, _ = connectivity(
            getattr(self.coordinator, "last_seen_utc", None),
            dt_util.utcnow(),
            self._grace_seconds,
        )
        return state

    @property
    def extra_state_attributes(self):
        last_seen = getattr(self.coordinator, "last_seen_utc", None)
        _, offline = connectivity(last_seen, dt_util.utcnow(), self._grace_seconds)
        return {
            "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime) else None,
            "offline_minutes": round(offline / 60.0, 1) if offline is not None else None,
            "grace_minutes": round(self._grace_seconds / 60.0, 1),
        }
