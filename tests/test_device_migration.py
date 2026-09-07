"""The device-identifier lookup, across the Home Assistant versions it has to work on.

`async_get_device(identifiers=...)` is deprecated from 2026.8 and stops working in
2027.8 (issue #24). Its replacement landed in 2026.8.0, while `hacs.json` still declares
a minimum of 2026.3.0 — so both branches are live, and the deprecated one is the branch
nobody's Home Assistant will exercise once they have upgraded. That is exactly the kind
that rots unnoticed, which is why it is tested rather than merely written.
"""
from custom_components.mspa.const import DOMAIN
from custom_components.mspa.device_compat import device_by_identifier


class _NewRegistry:
    """Home Assistant 2026.8 and later."""

    def __init__(self, found=None):
        self.found = found
        self.calls = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.calls.append((identifier, config_entry_id))
        return self.found

    def async_get_device(self, identifiers=None, connections=None):
        raise AssertionError("the deprecated call must not be reached on new HA")


class _OldRegistry:
    """Home Assistant 2026.3 to 2026.7 — the replacement does not exist yet."""

    def __init__(self, found=None):
        self.found = found
        self.calls = []

    def async_get_device(self, identifiers=None, connections=None):
        self.calls.append(identifiers)
        return self.found


class TestDeviceByIdentifier:

    def test_new_home_assistant_uses_the_replacement_with_the_entry_id(self):
        sentinel = object()
        reg = _NewRegistry(found=sentinel)
        assert device_by_identifier(reg, "entry123", "mspa_hottub") is sentinel
        assert reg.calls == [((DOMAIN, "mspa_hottub"), "entry123")], \
            "the entry id is the whole point of the new API — it must be passed"

    def test_old_home_assistant_falls_back_to_the_deprecated_call(self):
        sentinel = object()
        reg = _OldRegistry(found=sentinel)
        assert device_by_identifier(reg, "entry123", "mspa_hottub") is sentinel
        assert reg.calls == [{(DOMAIN, "mspa_hottub")}]

    def test_a_missing_device_is_none_on_both(self):
        for reg in (_NewRegistry(found=None), _OldRegistry(found=None)):
            assert device_by_identifier(reg, "e", "mspa_hottub") is None

    def test_the_real_identifier_is_looked_up_the_same_way(self):
        reg = _NewRegistry()
        device_by_identifier(reg, "e", "ede01c26")
        assert reg.calls == [((DOMAIN, "ede01c26"), "e")]

    def test_detection_is_on_the_registry_not_on_a_version_string(self):
        """A version check would need updating; asking the object never goes stale."""
        reg = _OldRegistry()
        assert not hasattr(reg, "async_get_device_by_identifier")
        device_by_identifier(reg, "e", "x")
        assert reg.calls, "must have used the only method available"
