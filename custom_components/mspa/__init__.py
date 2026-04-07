import logging

import voluptuous as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import MSpaUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# This integration only supports config entries
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.CLIMATE,
    Platform.NUMBER
]

SERVICES = [
    "set_temperature",
    "set_heater",
    "set_filter",
    "set_bubble",
    "set_jet",
    "set_bubble_level"
]

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the MSpa integration from configuration.yaml (if used)."""
    return True


def _register_services(hass, coordinator):
    for service in SERVICES:
        hass.services.async_register(DOMAIN, service, getattr(coordinator, service))
        _LOGGER.debug('MSpa registered "%s" service', service)


def _unregister_services(hass):
    for service in SERVICES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
            _LOGGER.debug('MSpa unregistered "%s" service', service)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up MSpa from a config entry."""
    coordinator = MSpaUpdateCoordinator(hass, entry)
    await coordinator.api.async_init()

    # Migrate old hardcoded device identifier to real device_id
    real_device_id = coordinator.device_id
    if real_device_id and real_device_id != "mspa_hottub":
        dev_reg = dr.async_get(hass)
        old_device = dev_reg.async_get_device(identifiers={(DOMAIN, "mspa_hottub")})
        if old_device is not None:
            # Remove the empty new-identifier device if it was already created
            new_device = dev_reg.async_get_device(identifiers={(DOMAIN, real_device_id)})
            if new_device is not None and new_device.id != old_device.id:
                _LOGGER.info(
                    "Removing empty duplicate device '%s' before migration",
                    new_device.id,
                )
                dev_reg.async_remove_device(new_device.id)

            _LOGGER.info(
                "Migrating device identifier from 'mspa_hottub' to '%s'",
                real_device_id,
            )
            dev_reg.async_update_device(
                old_device.id,
                new_identifiers={(DOMAIN, real_device_id)},
            )

    # Migrate old entity unique_ids that lacked device_id suffix
    if real_device_id:
        ent_reg = er.async_get(hass)
        _OLD_UNIQUE_ID_PREFIXES = [
            # Diagnostic sensors had unique_id "mspa_{key}" without device_id
            "mspa_otastatus", "mspa_wifivertion", "mspa_mcuversion",
            "mspa_ConnectType", "mspa_temperature_unit", "mspa_auto_inflate",
            "mspa_filter_current", "mspa_safety_lock", "mspa_heat_time_switch",
            "mspa_heat_state", "mspa_multimcuotainfo", "mspa_heat_time",
            "mspa_filter_life", "mspa_trdversion", "mspa_is_online",
            "mspa_warning", "mspa_device_heat_perhour",
        ]
        # Migrate filter_status which had a missing underscore before device_id.
        _OLD_UNIQUE_ID_RENAMES = [
            (f"mspa_filter_status{real_device_id}", f"mspa_filter_status_{real_device_id}"),
        ]
        for old_uid in _OLD_UNIQUE_ID_PREFIXES:
            new_uid = f"{old_uid}_{real_device_id}"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, old_uid)
            if entity_id is not None:
                # Only migrate if the new unique_id isn't already taken
                if ent_reg.async_get_entity_id("sensor", DOMAIN, new_uid) is None:
                    _LOGGER.info("Migrating entity unique_id '%s' → '%s'", old_uid, new_uid)
                    ent_reg.async_update_entity(entity_id, new_unique_id=new_uid)
                else:
                    # New unique_id already exists — remove the old duplicate
                    _LOGGER.info("Removing old duplicate entity '%s' (new uid already exists)", entity_id)
                    ent_reg.async_remove(entity_id)

        # Rename entities with wrong unique_id format (e.g. missing underscore)
        for old_uid, new_uid in _OLD_UNIQUE_ID_RENAMES:
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, old_uid)
            if entity_id is not None:
                if ent_reg.async_get_entity_id("sensor", DOMAIN, new_uid) is None:
                    _LOGGER.info("Migrating entity unique_id '%s' → '%s'", old_uid, new_uid)
                    ent_reg.async_update_entity(entity_id, new_unique_id=new_uid)
                else:
                    _LOGGER.info("Removing old entity '%s' (corrected uid already exists)", entity_id)
                    ent_reg.async_remove(entity_id)

    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    _LOGGER.debug("MSpa integration %s setup %s %s", DOMAIN, entry.title, entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Ensure options changes cause an immediate refresh
    entry.add_update_listener(async_options_updated)

    _register_services(hass, coordinator)
    _LOGGER.info("MSpa integration %s setup complete", DOMAIN)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    _LOGGER.debug("Unloading MSpa integration %s for entry %s", DOMAIN, entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        remaining = hass.data.get(DOMAIN, {})
        if not remaining:
            # Last entry unloaded — clean up services and cached auth
            _unregister_services(hass)
            hass.data.pop("mspa_auth", None)
            _LOGGER.debug("MSpa integration %s fully unloaded — services and auth cache cleared", DOMAIN)
        else:
            # Re-register services pointing at a still-active coordinator
            remaining_coordinator = next(iter(remaining.values()))
            _register_services(hass, remaining_coordinator)
            _LOGGER.debug(
                "MSpa entry %s unloaded; services re-registered on remaining entry",
                entry.entry_id,
            )
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry
) -> bool:
    """Prevent removing a device independently of its config entry.

    Each MSpa config entry represents exactly one spa device.  Deleting the
    device from the device page would leave the coordinator running with no
    owner.  Returning False tells HA to block the deletion and direct the
    user to delete the integration entry instead.
    """
    return False


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update by refreshing coordinator data."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_request_refresh()
