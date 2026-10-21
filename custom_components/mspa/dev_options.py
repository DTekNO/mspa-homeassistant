"""Development options, read from a file beside configuration.yaml.

`CONF_OUTDOOR_SENSOR` and `CONF_CROSSING_LOG` are instruments rather than features, so
neither is offered in the options dialog. Until now the only way to set one was to edit
`.storage/core.config_entries` by hand, which means stopping Home Assistant, finding one
entry in a single-line JSON file holding every integration on the system, and getting it
back together again. That is an unreasonable thing to ask in order to switch on a log.

So they can also be set here, in `mspa_dev.yaml` in the configuration directory:

    # Keep the full crossing log, cooling rows and all.
    crossing_log: true
    # The site thermometer, recorded alongside the forecast for later comparison.
    outdoor_sensor: sensor.nes25_outdoor_temperature

The file is optional, applies to every MSpa entry, and is read once per setup — so
editing it takes effect on Reload (Settings → Devices & services → MSpa → ⋮ → Reload),
with no restart and nothing in `.storage` touched. What it holds is reported by the
Ambient learning sensor under `dev_options`, which is how you confirm it was read
without going to the log.

Only the keys in `DEV_OPTION_KEYS` are honoured. A file that could set any option would
be a second, undocumented configuration surface competing with the dialog; this one can
only set the two things the dialog deliberately does not. For a key it does name the
file wins over the config entry, because setting it here is the newer and more
deliberate act — and because an entry option left over from an earlier hand-edit would
otherwise be impossible to turn off from here.
"""
from __future__ import annotations

import logging
import os

from .const import CONF_CROSSING_LOG, CONF_OUTDOOR_SENSOR

_LOGGER = logging.getLogger(__name__)

DEV_OPTIONS_FILE = "mspa_dev.yaml"

# What the file may set. Deliberately short: see the module docstring.
DEV_OPTION_KEYS = frozenset({CONF_CROSSING_LOG, CONF_OUTDOOR_SENSOR})


def read_dev_options(path: str) -> dict:
    """Parse the file, or an empty mapping when there is nothing usable to parse.

    Blocking, so call it from the executor. Never raises: a malformed file leaves the
    integration running on the config entry alone, which is what it did before the file
    existed. The log says which, because a typo here is otherwise silent — the symptom
    is an option that looks set and is not.
    """
    if not os.path.exists(path):
        return {}
    try:
        import yaml                                    # noqa: PLC0415
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except Exception:                                  # noqa: BLE001
        _LOGGER.warning("%s could not be read; ignoring it",
                        os.path.basename(path), exc_info=True)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _LOGGER.warning("%s should be a mapping of option names to values, not %s",
                        os.path.basename(path), type(raw).__name__)
        return {}
    known = {k: v for k, v in raw.items() if k in DEV_OPTION_KEYS}
    for key in raw:
        if key not in DEV_OPTION_KEYS:
            _LOGGER.warning("%s: ignoring %r, which is not a development option",
                            os.path.basename(path), key)
    if known:
        _LOGGER.info("%s: %s", os.path.basename(path),
                     ", ".join(f"{k}={v!r}" for k, v in sorted(known.items())))
    return known
