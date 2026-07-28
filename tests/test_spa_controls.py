"""Tests for MSpa spa control behaviour.

Covers the tangible integration behaviours rather than internal math:
  _is_spa_active           — which components keep the spa "active" for polling
  _effective_heat_rate     — rate fallback and clamping
  _calculate_total_power   — power sensor logic per heat_state
  filter→heater coupling   — turning filter off also expects heater off

Run with: python -m pytest tests/test_spa_controls.py -v
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.sensor import (
    _effective_heat_rate,
    _effective_cool_rate,
    _calculate_total_power,
)
from custom_components.mspa.const import (
    DEFAULT_PUMP_POWER,
    DEFAULT_BUBBLE_POWER,
    DEFAULT_HEATER_POWER_PREHEAT,
    DEFAULT_HEATER_POWER_HEAT,
)


# ── Minimal mock helpers ──────────────────────────────────────────────────────

def _make_coord(**last_data_overrides):
    """Minimal mock coordinator for sensor helper tests."""
    c = MagicMock()
    c.computed_heat_rate = None
    c.computed_cool_rate = None
    c._last_data = {
        "device_heat_perhour": 0,
        "heater": "off",
        "filter": "off",
        "bubble": "off",
        "jet": "off",
        "heat_state": 0,
        **last_data_overrides,
    }
    return c


def _make_config_entry(options=None):
    """Minimal config entry stub."""
    e = MagicMock()
    e.options = options or {}
    return e


def _trigger_coord(**overrides) -> MSpaUpdateCoordinator:
    """Coordinator instance via object.__new__ for async method tests."""
    c = object.__new__(MSpaUpdateCoordinator)
    c._pending_changes = {}
    c._pending_raw_command = {}
    c._command_retry_count = 0
    c._rapid_poll_until = None
    c.update_interval = MagicMock()
    c._last_data = {
        "heater": "off",
        "filter": "off",
        "bubble": "off",
        "bubble_level": 1,
    }
    c.api = MagicMock()
    c.api.set_heater_state = AsyncMock()
    c.api.set_filter_state = AsyncMock()
    c.api.set_bubble_state = AsyncMock()
    c.api.set_jet_state = AsyncMock()
    c.async_request_refresh = AsyncMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════════
# _is_spa_active — polling tier input
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsSpaActive:
    """Static method: returns True when at least one component is on."""

    def test_heater_on_is_active(self):
        assert MSpaUpdateCoordinator._is_spa_active({"heater": "on"}) is True

    def test_filter_on_is_active(self):
        assert MSpaUpdateCoordinator._is_spa_active({"filter": "on"}) is True

    def test_bubble_on_is_active(self):
        assert MSpaUpdateCoordinator._is_spa_active({"bubble": "on"}) is True

    def test_jet_on_is_active(self):
        assert MSpaUpdateCoordinator._is_spa_active({"jet": "on"}) is True

    def test_all_off_is_not_active(self):
        data = {"heater": "off", "filter": "off", "bubble": "off", "jet": "off"}
        assert MSpaUpdateCoordinator._is_spa_active(data) is False

    def test_empty_dict_is_not_active(self):
        assert MSpaUpdateCoordinator._is_spa_active({}) is False


# ═══════════════════════════════════════════════════════════════════════════════
# _effective_heat_rate — rate selection and clamping
# ═══════════════════════════════════════════════════════════════════════════════

class TestEffectiveHeatRate:

    def test_uses_computed_rate_when_available(self):
        c = _make_coord()
        c.computed_heat_rate = 2.0
        assert _effective_heat_rate(c) == pytest.approx(2.0)

    def test_falls_back_to_device_rate(self):
        c = _make_coord(device_heat_perhour=15)  # 15/10 = 1.5°C/h
        c.computed_heat_rate = None
        assert _effective_heat_rate(c) == pytest.approx(1.5)

    def test_device_rate_clamped_to_max(self):
        c = _make_coord(device_heat_perhour=25)  # 2.5 → clamped to 2.0
        c.computed_heat_rate = None
        assert _effective_heat_rate(c) == pytest.approx(2.0)

    def test_device_rate_clamped_to_min(self):
        c = _make_coord(device_heat_perhour=1)  # 0.1 → clamped to 0.5
        c.computed_heat_rate = None
        assert _effective_heat_rate(c) == pytest.approx(0.5)

    def test_zero_device_rate_returns_none(self):
        c = _make_coord(device_heat_perhour=0)
        c.computed_heat_rate = None
        assert _effective_heat_rate(c) is None


# ═══════════════════════════════════════════════════════════════════════════════
# _calculate_total_power — power sensor logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestTotalPower:

    def test_all_off_is_zero(self):
        c = _make_coord()
        entry = _make_config_entry()
        assert _calculate_total_power(c, entry) == 0

    def test_filter_on_adds_pump_power(self):
        c = _make_coord(filter="on")
        entry = _make_config_entry()
        assert _calculate_total_power(c, entry) == DEFAULT_PUMP_POWER

    def test_bubble_on_adds_bubble_power(self):
        c = _make_coord(bubble="on")
        entry = _make_config_entry()
        assert _calculate_total_power(c, entry) == DEFAULT_BUBBLE_POWER

    def test_heater_preheat_state(self):
        c = _make_coord(heater="on", heat_state=2)
        entry = _make_config_entry()
        assert _calculate_total_power(c, entry) == DEFAULT_HEATER_POWER_PREHEAT

    def test_heater_full_heat_state(self):
        c = _make_coord(heater="on", heat_state=3)
        entry = _make_config_entry()
        assert _calculate_total_power(c, entry) == DEFAULT_HEATER_POWER_HEAT

    def test_heater_off_contributes_zero_regardless_of_state(self):
        c = _make_coord(heater="off", heat_state=3)
        entry = _make_config_entry()
        assert _calculate_total_power(c, entry) == 0

    def test_custom_power_options_respected(self):
        c = _make_coord(filter="on", heater="on", heat_state=3)
        entry = _make_config_entry(options={"pump_power": 100, "heater_power_heat": 3000})
        assert _calculate_total_power(c, entry) == 3100


# ═══════════════════════════════════════════════════════════════════════════════
# Filter→heater coupling — turning filter off registers heater=off as expected
# ═══════════════════════════════════════════════════════════════════════════════

class TestFilterHeaterCoupling:

    def test_filter_off_registers_heater_off_in_pending_changes(self):
        """set_feature_state('filter', 'off') must add heater='off' to _pending_changes.

        The API enforces filter-off → heater-off at the hardware level.  HA must
        track this implicit change so the retry payload doesn't accidentally
        resend a stale heater-on command from an earlier user action.
        """
        c = _trigger_coord()
        _run(c.set_feature_state("filter", "off"))
        assert c._pending_changes.get("heater") == "off"

    def test_heater_on_alone_does_not_register_filter_change(self):
        """Turning heater on independently must not touch filter expectations."""
        c = _trigger_coord()
        _run(c.set_feature_state("heater", "on"))
        assert "filter" not in c._pending_changes


# ═══════════════════════════════════════════════════════════════════════════════
# Bubble level→on coupling
# ═══════════════════════════════════════════════════════════════════════════════

class TestBubbleLevelOnCoupling:

    def test_bubble_on_with_zero_level_uses_level_1(self):
        """Turn-on with bubble_level=0 in last_data must send level 1, not 0.

        The device ignores bubble_state=1 when level=0; the fix clamps to ≥1
        so the switch always works regardless of what the device last reported.
        Also updates _last_data immediately so the slider reflects the chosen level.
        """
        c = _trigger_coord()
        c._last_data["bubble_level"] = 0
        _run(c.set_feature_state("bubble", "on"))
        args = c.api.set_bubble_state.call_args
        state, level = args[0]
        assert state == 1
        assert level >= 1
        assert c._last_data["bubble_level"] == level

    def test_bubble_on_with_missing_level_uses_level_1(self):
        """Turn-on with no bubble_level key in last_data defaults to level 1."""
        c = _trigger_coord()
        del c._last_data["bubble_level"]
        _run(c.set_feature_state("bubble", "on"))
        args = c.api.set_bubble_state.call_args
        _, level = args[0]
        assert level == 1

    def test_bubble_on_preserves_last_known_level(self):
        """Turn-on when bubble_level=2 was last known must use level 2."""
        c = _trigger_coord()
        c._last_data["bubble_level"] = 2
        _run(c.set_feature_state("bubble", "on"))
        args = c.api.set_bubble_state.call_args
        _, level = args[0]
        assert level == 2

    def test_set_bubble_level_sends_bubble_state_on(self):
        """set_bubble_level must send bubble_state=1 so the device activates.

        The device activates bubbles when it receives a level command; HA should
        be explicit rather than relying on that side-effect.
        """
        c = _trigger_coord()
        svc = type("ServiceCall", (), {"data": {"level": 3}})()
        _run(c.set_bubble_level(svc))
        c.api.set_bubble_state.assert_called_once_with(1, 3)

    def test_set_bubble_level_registers_bubble_on_in_pending(self):
        """set_bubble_level must register bubble='on' in _pending_changes.

        Without this, the bubble switch stays 'off' in HA even after the device
        activates — the user would see the switch snap back to off on next poll.
        """
        c = _trigger_coord()
        svc = type("ServiceCall", (), {"data": {"level": 2}})()
        _run(c.set_bubble_level(svc))
        assert c._pending_changes.get("bubble") == "on"
        assert c._pending_changes.get("bubble_level") == 2

    def test_set_bubble_level_updates_last_data_immediately(self):
        """set_bubble_level must write the new level into _last_data before the API
        call so the slider reflects it on the next render cycle."""
        c = _trigger_coord()
        c._last_data["bubble_level"] = 1
        svc = type("ServiceCall", (), {"data": {"level": 3}})()
        _run(c.set_bubble_level(svc))
        assert c._last_data["bubble_level"] == 3
