"""Config flow for MSpa Hot Tub integration."""
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
from .const import (
    DOMAIN,
    CONF_REGION,
    DEFAULT_REGION,
    REGIONS,
    COUNTRY_TO_REGION,
    DEFAULT_PUMP_POWER,
    DEFAULT_BUBBLE_POWER,
    DEFAULT_HEATER_POWER_PREHEAT,
    DEFAULT_HEATER_POWER_HEAT,
    CONF_TRACK_TEMPERATURE_UNIT,
    CONF_RESTORE_STATE,
    CONF_ALWAYS_ENFORCE_UNIT,
    CONF_AMBIENT_CORRECTION,
    DEFAULT_AMBIENT_CORRECTION,
    CONF_WEATHER_ENTITY,
    CONF_SCHEDULE_TARGET_TEMP,
    DEFAULT_SCHEDULE_TARGET_TEMP,
)
import hashlib
from .mspa_api import DEMO_EMAIL, _DEMO_DEVICES

_LOGGER = logging.getLogger(__name__)

from typing import Any, Tuple, Optional

def detect_region_from_hass(hass) -> Tuple[str, Optional[str]]:
    """Detect region based on Home Assistant country/timezone with fallback to ROW.
    
    Returns:
        Tuple of (region, country_code) where country_code is None if not detected
    """
    # Try to get country from Home Assistant config
    try:
        country = hass.config.country
        if country:
            region = COUNTRY_TO_REGION.get(country, DEFAULT_REGION)
            _LOGGER.info(f"Auto-detected region '{region}' based on country '{country}'")
            return region, country
    except Exception as e:
        _LOGGER.debug(f"Could not detect country: {e}")
    
    # Safe fallback to ROW (Europe) for maximum compatibility
    _LOGGER.info("Using default region 'ROW' (Europe/Rest of World)")
    return DEFAULT_REGION, None

class MSpaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MSpa Hot Tub."""

    VERSION = 1

    def __init__(self):
        super().__init__()
        self._data: dict = {}
        self._devices: list = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step: collect credentials and discover devices."""
        errors = {}

        # If an MSpa entry already exists, reuse its credentials — we only support one account.
        # Skip the credential form and jump straight to device selection.
        if user_input is None:
            existing = self._async_current_entries()
            if existing:
                first = existing[0]
                self._data = {
                    "account_email": first.data["account_email"],
                    "password": first.data["password"],
                    "region": first.data.get("region", DEFAULT_REGION),
                }
                try:
                    self._devices = await self._fetch_devices(
                        self._data["account_email"],
                        self._data["password"],
                        self._data["region"],
                    )
                    return await self.async_step_device()
                except Exception as err:
                    _LOGGER.warning(
                        "Could not reuse existing MSpa credentials (%s) — falling back to manual entry", err
                    )
                    # Fall through to show the credential form

        # Auto-detect region based on HA country setting for smart default
        detected_region, detected_country = detect_region_from_hass(self.hass)
        
        # Build description for the form
        if detected_country:
            description_placeholders = {
                "detected_info": f"Auto-detected from your Home Assistant country setting ({detected_country}): {REGIONS[detected_region]}"
            }
        else:
            description_placeholders = {
                "detected_info": "No country detected, defaulting to Europe/Rest of World for maximum compatibility"
            }

        if user_input is not None:
            # Strip whitespace from email and password to prevent common user errors
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD].strip()

            if len(user_input[CONF_EMAIL]) != len(email):
                _LOGGER.warning("DIAGNOSTIC: Email had leading/trailing whitespace - removed %d characters",
                               len(user_input[CONF_EMAIL]) - len(email))
            if len(user_input[CONF_PASSWORD]) != len(password):
                _LOGGER.warning("DIAGNOSTIC: Password had leading/trailing whitespace - removed %d characters",
                               len(user_input[CONF_PASSWORD]) - len(password))

            # Generate MD5 hash (skipped for demo mode — password is irrelevant)
            if email == DEMO_EMAIL:
                password_hash = "demo"
            else:
                password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
            region = user_input.get(CONF_REGION, detected_region)

            self._data = {
                "account_email": email,
                "password": password_hash,
                "region": region,
            }

            # Fetch device list to validate credentials and discover available devices
            try:
                self._devices = await self._fetch_devices(email, password_hash, region)
            except RuntimeError as err:
                err_msg = str(err).lower()
                if "authentication failed" in err_msg or "password" in err_msg:
                    _LOGGER.warning("DIAGNOSTIC: Authentication failed: %s", err)
                    errors["base"] = "invalid_auth"
                else:
                    _LOGGER.warning("DIAGNOSTIC: Device discovery failed: %s", err)
                    errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.warning("DIAGNOSTIC: Device discovery failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_device()

        data_schema = vol.Schema({
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_REGION, default=detected_region, description={"suggested_value": detected_region}): vol.In(REGIONS),
        })
        return self.async_show_form(
            step_id="user", 
            data_schema=data_schema, 
            errors=errors,
            description_placeholders=description_placeholders
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None):
        """Handle device selection when multiple devices are available."""
        errors = {}

        # Filter out devices that are already configured
        configured_ids = {
            entry.unique_id
            for entry in self._async_current_entries()
            if entry.unique_id
        }
        available = [d for d in self._devices if d.get("device_id") not in configured_ids]

        if not available:
            return self.async_abort(reason="no_devices_available")

        if len(available) == 1:
            # Only one unconfigured device — select it automatically
            return await self._create_entry_for_device(available[0])

        # Multiple unconfigured devices available
        if user_input is not None:
            device_id = user_input["device_id"]
            device = next((d for d in available if d.get("device_id") == device_id), None)
            if device:
                return await self._create_entry_for_device(device)
            errors["base"] = "unknown"

        device_options = {
            d["device_id"]: d.get("device_alias") or d.get("product_model") or d["device_id"]
            for d in available
            if d.get("device_id")
        }
        data_schema = vol.Schema({
            vol.Required("device_id"): vol.In(device_options),
        })
        return self.async_show_form(
            step_id="device",
            data_schema=data_schema,
            errors=errors,
        )

    async def _create_entry_for_device(self, device: dict) -> dict:
        """Create a config entry for the given device."""
        device_id = device.get("device_id")
        # Prevent adding the same physical device twice
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        alias = device.get("device_alias") or device.get("product_model") or ""
        title = f"MSpa {alias}".strip() if alias else "MSpa Hot Tub"

        return self.async_create_entry(
            title=title,
            data={
                **self._data,
                "device_id": device_id,
                "product_id": device.get("product_id"),
            },
        )

    async def _fetch_devices(self, email: str, password_hash: str, region: str) -> list:
        """Authenticate and return the list of devices for these credentials."""
        if email == DEMO_EMAIL:
            _LOGGER.info("DEMO MODE: returning %d virtual demo devices", len(_DEMO_DEVICES))
            return list(_DEMO_DEVICES)

        from .mspa_api import MSpaApiClient
        temp_api = MSpaApiClient(
            hass=self.hass,
            account_email=email,
            password=password_hash,
            coordinator=None,
            region=region,
        )
        await temp_api.authenticate()
        device_list = await temp_api.get_device_list()
        if not device_list or not device_list.get("list"):
            raise RuntimeError("No devices found for these credentials")
        return device_list["list"]

    # Hook the options flow for per-device settings
    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for per-device power consumption settings."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            # Save options and return
            return self.async_create_entry(title="", data=user_input)

        data_schema = vol.Schema({
            vol.Optional(
                "pump_power",
                default=self.config_entry.options.get("pump_power", DEFAULT_PUMP_POWER),
            ): vol.All(int, vol.Range(min=0)),
            vol.Optional(
                "bubble_power",
                default=self.config_entry.options.get("bubble_power", DEFAULT_BUBBLE_POWER),
            ): vol.All(int, vol.Range(min=0)),
            vol.Optional(
                "heater_power_preheat",
                default=self.config_entry.options.get("heater_power_preheat", DEFAULT_HEATER_POWER_PREHEAT),
            ): vol.All(int, vol.Range(min=0)),
            vol.Optional(
                "heater_power_heat",
                default=self.config_entry.options.get("heater_power_heat", DEFAULT_HEATER_POWER_HEAT),
            ): vol.All(int, vol.Range(min=0)),
            vol.Optional(
                CONF_TRACK_TEMPERATURE_UNIT,
                default=self.config_entry.options.get(CONF_TRACK_TEMPERATURE_UNIT, False),
                description="Set MSpa temperature unit to match Home Assistant system unit on power-up"
            ): bool,
            vol.Optional(
                CONF_ALWAYS_ENFORCE_UNIT,
                default=self.config_entry.options.get(CONF_ALWAYS_ENFORCE_UNIT, False),
                description="Always enforce temperature unit on every update (use if device frequently forgets)"
            ): bool,
            vol.Optional(
                CONF_RESTORE_STATE,
                default=self.config_entry.options.get(CONF_RESTORE_STATE, False),
                description="Restore previous states after power outage (heater, temperature, filter, etc.)"
            ): bool,
            vol.Optional(
                CONF_WEATHER_ENTITY,
                description={"suggested_value": self.config_entry.options.get(CONF_WEATHER_ENTITY)},
            ): EntitySelector(EntitySelectorConfig(domain="weather")),
            vol.Optional(
                CONF_AMBIENT_CORRECTION,
                default=self.config_entry.options.get(
                    CONF_AMBIENT_CORRECTION, DEFAULT_AMBIENT_CORRECTION),
            ): bool,
            vol.Optional(
                CONF_SCHEDULE_TARGET_TEMP,
                default=self.config_entry.options.get(CONF_SCHEDULE_TARGET_TEMP, DEFAULT_SCHEDULE_TARGET_TEMP),
            ): NumberSelector(NumberSelectorConfig(
                min=20, max=40, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.BOX,
            )),
        })

        return self.async_show_form(step_id="init", data_schema=data_schema)