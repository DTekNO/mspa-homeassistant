"""Shims for Home Assistant APIs that changed while this integration was supported.

Its own module for two reasons. Everything here is temporary by construction and should
be deleted as a unit once `hacs.json` can name a high enough minimum — code with an
expiry date is easier to remove when it is not threaded through a setup function. And
the branch that will rot is the deprecated one, which nobody's Home Assistant will
exercise once they have upgraded, so it needs a test more than the live branch does and
a module with no heavy imports is one a test can reach.
"""
from .const import DOMAIN


def device_by_identifier(dev_reg, config_entry_id: str, identifier: str):
    """Look a device up by identifier, on old and new Home Assistant alike.

    `async_get_device(identifiers=...)` is deprecated from 2026.8 and stops working in
    2027.8: identifiers are no longer unique across config entries, so the lookup has to
    say which entry it means. The replacement, `async_get_device_by_identifier`, arrived
    in 2026.8.0 (core dff7210c4d9, 20.07.2026) — but `hacs.json` still declares a minimum
    of 2026.3.0, so calling it unconditionally would break every install between the two,
    for the sake of a warning that has a year left to run.

    Asking the registry what it supports costs one `hasattr` per setup and keeps both
    working until the minimum can be raised, at which point this file collapses to
    nothing. Reported as issue #24.
    """
    if hasattr(dev_reg, "async_get_device_by_identifier"):
        return dev_reg.async_get_device_by_identifier(
            (DOMAIN, identifier), config_entry_id)
    return dev_reg.async_get_device(identifiers={(DOMAIN, identifier)})
