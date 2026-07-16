"""
Pytest configuration for MSpa integration tests.

Stubs the entire homeassistant package tree and bypasses the integration's
__init__.py (which imports voluptuous, the coordinator, etc.) so that tests
can import individual modules (sensor, entity, const) without a running HA
instance or third-party packages installed.

Order matters — this file must complete all sys.modules manipulation before
any test module performs its top-level imports.
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT  = Path(__file__).parent.parent                      # .../mspa/
_PKG_ROOT   = _REPO_ROOT / "custom_components" / "mspa"


# ════════════════════════════════════════════════════════════════════════════
# 1. Real base classes for HA entity types
#    Python requires that base classes are actual types, not MagicMock instances.
# ════════════════════════════════════════════════════════════════════════════

class _HAEntity:
    def __init__(self, *args, **kwargs): pass

class _CoordinatorEntity:
    def __init__(self, coordinator, *args, **kwargs):
        self.coordinator = coordinator
    @property
    def available(self):
        return True

class _SensorEntity(_HAEntity):        pass
class _SwitchEntity(_HAEntity):        pass
class _NumberEntity(_HAEntity):        pass
class _BinarySensorEntity(_HAEntity):  pass
class _ClimateEntity(_HAEntity):       pass
class _DateTimeEntity(_HAEntity):      pass
class _RestoreEntity(_HAEntity):
    async def async_added_to_hass(self): pass
    async def async_get_last_state(self): return None


# ════════════════════════════════════════════════════════════════════════════
# 2. Stub the homeassistant package tree
# ════════════════════════════════════════════════════════════════════════════

_entity_cat          = MagicMock(); _entity_cat.CONFIG = "config"; _entity_cat.DIAGNOSTIC = "diagnostic"
_sensor_mod          = MagicMock(); _sensor_mod.SensorEntity = _SensorEntity; _sensor_mod.SensorStateClass = MagicMock(); _sensor_mod.SensorDeviceClass = MagicMock()
_switch_mod          = MagicMock(); _switch_mod.SwitchEntity = _SwitchEntity
_number_mod          = MagicMock(); _number_mod.NumberEntity = _NumberEntity
_binary_mod          = MagicMock(); _binary_mod.BinarySensorEntity = _BinarySensorEntity
_climate_mod         = MagicMock(); _climate_mod.ClimateEntity = _ClimateEntity
_datetime_mod        = MagicMock(); _datetime_mod.DateTimeEntity = _DateTimeEntity
_coordinator_mod     = MagicMock(); _coordinator_mod.CoordinatorEntity = _CoordinatorEntity
_restore_mod         = MagicMock(); _restore_mod.RestoreEntity = _RestoreEntity
_entity_mod          = MagicMock(); _entity_mod.EntityCategory = _entity_cat
_dr_mod              = MagicMock(); _dr_mod.CONNECTION_NETWORK_MAC = "mac"
_unit_of_temp        = MagicMock(); _unit_of_temp.CELSIUS = "°C"
_const_mod           = MagicMock()
_const_mod.UnitOfTemperature = _unit_of_temp
_unit_of_power       = MagicMock(); _unit_of_power.WATT = "W"
_unit_of_energy      = MagicMock(); _unit_of_energy.WATT_HOUR = "Wh"; _unit_of_energy.KILO_WATT_HOUR = "kWh"
_const_mod.UnitOfPower       = _unit_of_power
_const_mod.UnitOfEnergy      = _unit_of_energy
_const_mod.EntityCategory    = _entity_cat
_const_mod.Platform          = MagicMock()
_helpers_mod         = MagicMock()
_helpers_mod.update_coordinator = _coordinator_mod
_helpers_mod.restore_state      = _restore_mod
_helpers_mod.entity             = _entity_mod
_helpers_mod.device_registry    = _dr_mod

sys.modules.update({
    "voluptuous":                                   MagicMock(),
    "homeassistant":                                MagicMock(),
    "homeassistant.config_entries":                 MagicMock(),
    "homeassistant.core":                           MagicMock(),
    "homeassistant.components":                     MagicMock(),
    "homeassistant.components.sensor":              _sensor_mod,
    "homeassistant.components.switch":              _switch_mod,
    "homeassistant.components.number":              _number_mod,
    "homeassistant.components.binary_sensor":       _binary_mod,
    "homeassistant.components.climate":             _climate_mod,
    "homeassistant.components.datetime":            _datetime_mod,
    "homeassistant.helpers":                        _helpers_mod,
    "homeassistant.helpers.update_coordinator":     _coordinator_mod,
    "homeassistant.helpers.entity":                 _entity_mod,
    "homeassistant.helpers.restore_state":          _restore_mod,
    "homeassistant.helpers.device_registry":        _dr_mod,
    "homeassistant.helpers.event":                  MagicMock(),
    "homeassistant.helpers.config_validation":      MagicMock(),
    "homeassistant.helpers.entity_registry":        MagicMock(),
    "homeassistant.const":                          _const_mod,
    "homeassistant.util":                           MagicMock(),
    "homeassistant.util.dt":                        MagicMock(),
})


# ════════════════════════════════════════════════════════════════════════════
# 3. Build a real package stub for custom_components.mspa
#    This stops Python from executing __init__.py (which imports the
#    coordinator, voluptuous, etc. that we don't want in unit tests).
# ════════════════════════════════════════════════════════════════════════════

def _make_pkg(name: str, path: Path) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__package__ = name
    mod.__path__    = [str(path)]
    mod.__file__    = str(path / "__init__.py")
    return mod

_cc_pkg   = _make_pkg("custom_components",       _REPO_ROOT / "custom_components")
_mspa_pkg = _make_pkg("custom_components.mspa",  _PKG_ROOT)

sys.modules["custom_components"]      = _cc_pkg
sys.modules["custom_components.mspa"] = _mspa_pkg

# Stub the coordinator — sensor.py does NOT import it directly, but entity.py
# might pull it in transitively.  Pre-stubbing prevents surprises.
sys.modules["custom_components.mspa.coordinator"] = MagicMock()

# Ensure the repo root is on sys.path so absolute imports work.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
