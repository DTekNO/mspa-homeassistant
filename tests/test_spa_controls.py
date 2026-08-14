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
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.mspa import coordinator as coordinator_mod
from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from homeassistant.exceptions import HomeAssistantError
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
    # A minimal spa: commands mutate raw state, get_hot_tub_status reports it.  The
    # start sequence confirms each half by reading back, so a mock that only
    # acknowledges commands would leave it polling until it timed out — and would prove
    # nothing about the ordering, which is the thing under test.
    c.calls = []
    c.spa = {"heater_state": 0, "filter_state": 0}

    def _cmd(name, key):
        def _fn(value, *_a):
            c.calls.append(name)
            c.spa[key] = int(value)
            return {"message": "SUCCESS"}
        return _fn

    c.api.set_heater_state = AsyncMock(side_effect=_cmd("heater", "heater_state"))
    c.api.set_filter_state = AsyncMock(side_effect=_cmd("filter", "filter_state"))
    c.api.get_hot_tub_status = AsyncMock(side_effect=lambda *a, **k: dict(c.spa))
    c.api.set_bubble_state = AsyncMock()
    c.api.set_jet_state = AsyncMock()
    c.async_request_refresh = AsyncMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


@pytest.fixture(autouse=True)
def _no_pump_settle(monkeypatch):
    """Skip the pump settle delay in every test — 1.5 s per heater-on otherwise."""
    monkeypatch.setattr(coordinator_mod, "_PUMP_SETTLE_SECONDS", 0)


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

    def test_heater_on_registers_the_pump_it_started(self):
        """Heater-on brings the pump with it, so the filter change is real and must be
        registered as an *expectation* — it is not invented state.

        But it must stay out of the retry *payload*. `_pending_raw_command` is resent
        whole as a single device command when the spa does not confirm, so including
        the pump would retry `{heater_state: 1, filter_state: 1}` together — the one
        combination the soft start exists to avoid, and untested on this hardware. The
        soft start is about command acceptance and known-good sequencing, not about
        avoiding F1 (which the pump start itself is what risks).

        A bare heater-on is the correct retry: the pump was commanded and acknowledged
        with SUCCESS before the heater was touched.
        """
        c = _trigger_coord()                       # fixture starts with filter off
        _run(c.set_feature_state("heater", "on"))
        assert c._pending_changes.get("filter") == "on"
        assert "filter_state" not in c._pending_raw_command, (
            "the pump must not ride along in the heater's retry payload"
        )
        assert c._pending_raw_command.get("heater_state") == 1

    def test_heater_on_leaves_filter_alone_when_already_running(self):
        """No pump command is sent, so nothing about the filter is claimed."""
        c = _trigger_coord()
        c._last_data["filter"] = "on"
        _run(c.set_feature_state("heater", "on"))
        assert "filter" not in c._pending_changes
        c.api.set_filter_state.assert_not_called()


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


# ═══════════════════════════════════════════════════════════════════════════════
# PUMP-BEFORE-HEATER SOFT START
# ═══════════════════════════════════════════════════════════════════════════════

class TestPumpBeforeHeater:
    """The spa refuses to heat without flow, and the MSpa Link app never issues a
    bare heater-on — it starts the pump first.  Every heater path in this
    integration (climate hvac_mode, the heater switch, the set_heater service and
    the scheduler) funnels through set_feature_state, so the ordering is enforced
    there once rather than at four call sites.
    """

    def test_pump_is_commanded_before_the_heater(self):
        c = _trigger_coord()
        _run(c.set_feature_state("heater", "on"))
        assert c.calls == ["filter", "heater"], f"wrong order: {c.calls}"

    def test_pump_is_skipped_when_already_running(self):
        c = _trigger_coord()
        c._last_data["filter"] = "on"
        _run(c.set_feature_state("heater", "on"))
        assert c.calls == ["heater"]

    def test_heater_is_not_commanded_if_the_pump_refuses(self):
        """The one thing that must not happen: heating a spa with no flow."""
        c = _trigger_coord()
        c.api.set_filter_state = AsyncMock(return_value={"message": "ERROR"})
        with pytest.raises(Exception):
            _run(c.set_feature_state("heater", "on"))
        c.api.set_heater_state.assert_not_called()

    def test_a_missing_payload_is_treated_as_failure(self):
        c = _trigger_coord()
        c.api.set_filter_state = AsyncMock(return_value=None)
        with pytest.raises(Exception):
            _run(c.set_feature_state("heater", "on"))
        c.api.set_heater_state.assert_not_called()

    def test_turning_the_heater_off_never_touches_the_pump(self):
        c = _trigger_coord()
        c._last_data["heater"] = "on"
        _run(c.set_feature_state("heater", "off"))
        assert c.calls == ["heater"]

    def test_other_features_are_unaffected(self):
        c = _trigger_coord()
        _run(c.set_feature_state("jet", "on"))
        c.api.set_filter_state.assert_not_called()


class TestStartSequenceIsConfirmed:
    """pump on -> confirmed -> heater on -> confirmed.

    The soft start is not about avoiding an F1 fault — F1 comes from starting the pump
    into a physical obstruction, and this starts the pump. It is about command
    acceptance: a heater command issued with the pump off can simply be refused, and
    asserting heater_state and filter_state in one payload is untested on this hardware.
    So the two are separate commands in a known order, each read back before proceeding.
    """

    @pytest.fixture(autouse=True)
    def _fast_confirm(self, monkeypatch):
        """Collapse the confirmation budget so timeout paths do not really wait."""
        monkeypatch.setattr(coordinator_mod, "RAPID_SCAN_INTERVAL", 0)
        monkeypatch.setattr(coordinator_mod, "RAPID_POLL_TIMEOUT", 0)

    def test_full_sequence_order(self):
        c = _trigger_coord()
        _run(c.set_feature_state("heater", "on"))
        assert c.calls == ["filter", "heater"], "pump must be commanded first"
        assert c.spa == {"heater_state": 1, "filter_state": 1}

    def test_heater_is_not_commanded_until_the_pump_reads_on(self):
        """A pump that acknowledges but never runs must stop the sequence."""
        c = _trigger_coord()
        c.api.set_filter_state = AsyncMock(return_value={"message": "SUCCESS"})  # no state change
        with pytest.raises(HomeAssistantError) as err:
            _run(c.set_feature_state("heater", "on"))
        assert "circulation pump did not start" in str(err.value)
        c.api.set_heater_state.assert_not_called()

    def test_unconfirmed_heater_is_reported_rather_than_assumed(self):
        """Otherwise Ready at counts down to a session that never began."""
        c = _trigger_coord()
        c._last_data["filter"] = "on"
        c.spa["filter_state"] = 1
        c.api.set_heater_state = AsyncMock(return_value={"message": "SUCCESS"})  # no state change
        with pytest.raises(HomeAssistantError) as err:
            _run(c.set_feature_state("heater", "on"))
        assert "has not reported the heater running" in str(err.value)

    def test_confirmation_ignores_a_dropped_status_read(self):
        """A failed poll is not a verdict — only the deadline is."""
        c = _trigger_coord()
        c.spa["filter_state"] = 1
        c.api.get_hot_tub_status = AsyncMock(side_effect=RuntimeError("network"))
        assert _run(c._confirm_feature("filter", "on")) is False

    def test_confirmation_succeeds_when_already_in_the_wanted_state(self):
        c = _trigger_coord()
        c.spa["filter_state"] = 1
        assert _run(c._confirm_feature("filter", "on")) is True
        assert c._last_data["filter"] == "on"

    def test_turning_the_heater_off_is_not_confirmation_gated(self):
        """Only the start sequence has an ordering requirement."""
        c = _trigger_coord()
        c.spa["heater_state"] = 1
        c._last_data["heater"] = "on"
        _run(c.set_feature_state("heater", "off"))
        assert c.spa["heater_state"] == 0


class TestSwitchCallerTracing:
    """A consequential command must record where it came from.

    Added after a `filter: off` arrived 2.5 min before a scheduled heat start on
    2026-08-12 with nothing in the log to attribute it: the command itself was logged
    at DEBUG, so the only evidence was the retry warning 15 s later.
    """

    def _switch(self, feature="filter", context=None, states=()):
        from custom_components.mspa.switch import MSpaFeatureSwitch
        sw = MSpaFeatureSwitch.__new__(MSpaFeatureSwitch)
        sw.feature = feature
        sw._context = context
        sw.hass = MagicMock()
        sw.hass.states.async_all = MagicMock(return_value=list(states))
        return sw

    def test_reports_the_user_for_a_ui_action(self):
        sw = self._switch(context=MagicMock(user_id="abc123", parent_id=None))
        assert "abc123" in sw._describe_caller()

    def test_resolves_a_parent_context_to_an_entity(self):
        st = MagicMock(entity_id="automation.spa_off")
        st.context.id = "p1"
        sw = self._switch(context=MagicMock(user_id=None, parent_id="p1"), states=[st])
        assert "automation.spa_off" in sw._describe_caller()

    def test_reports_the_raw_parent_when_nothing_matches(self):
        """Still useful: the id can be matched against a trace by hand."""
        sw = self._switch(context=MagicMock(user_id=None, parent_id="p9"), states=[])
        assert "p9" in sw._describe_caller()

    def test_no_context_is_reported_as_internal(self):
        assert "internal" in self._switch(context=None)._describe_caller()

    def test_a_broken_state_machine_does_not_break_the_command(self):
        """Tracing is diagnostics; it must never be the reason a command fails."""
        sw = self._switch(context=MagicMock(user_id=None, parent_id="p1"))
        sw.hass.states.async_all = MagicMock(side_effect=RuntimeError("boom"))
        assert "p1" in sw._describe_caller()


class TestExternalChangeVisibility:
    """Every control key that moves must be reported, commanded or not.

    Both blind spots here made the 2026-08-12 filter-off untraceable: detection was
    skipped entirely while a command was pending — exactly when a cascade happens —
    and it stopped after the first changed key, which is precisely a cascade's
    signature (heater and filter moving together).
    """

    def _run_detection(self, snapshot, data, pending=None):
        """Drive the Phase-4 detection block and collect what it logged."""
        c = _trigger_coord()
        c._last_snapshot = snapshot
        c._pending_changes = pending or {}
        c._external_change_until = None
        c._last_state_change_time = 0
        c._last_heat_state = None
        c._last_data = dict(data)
        seen = []
        with patch.object(coordinator_mod._LOGGER, "info",
                          side_effect=lambda msg, *a: seen.append(msg % a if a else msg)):
            _run(c._check_adaptive_polling(data))
        return [m for m in seen if "External change" in m]

    def test_both_keys_of_a_cascade_are_reported(self):
        msgs = self._run_detection(
            {"heater": "on", "filter": "on"}, {"heater": "off", "filter": "off"})
        assert any("heater" in m for m in msgs), msgs
        assert any("filter" in m for m in msgs), f"cascade's second key lost: {msgs}"

    def test_changes_are_reported_even_while_a_command_is_pending(self):
        """A cascade arrives inside the confirmation window, not outside it."""
        msgs = self._run_detection(
            {"heater": "on", "filter": "on"}, {"heater": "off", "filter": "off"},
            pending={"bubble": "on"})
        assert len(msgs) == 2, f"suppressed by an unrelated pending change: {msgs}"

    def test_a_change_we_commanded_is_not_called_external(self):
        msgs = self._run_detection(
            {"filter": "on"}, {"filter": "off"}, pending={"filter": "off"})
        assert msgs == [], f"our own command reported as external: {msgs}"


class TestConfirmationWaitIsSatisfiable:
    """An armed confirmation wait must be capable of being satisfied.

    Reviewed 2026-08-13 after asking whether the `{'filter': 'off'}` wait seen on
    2026-08-12 could have been spurious — armed for a command never actually sent.
    It could not: the API call is awaited *before* _enable_rapid_polling arms the
    wait, and the only other writer of a `filter` expectation sets it to "on".
    But two ways were found for the wait to be armed and then never satisfiable.
    """

    def test_expectation_is_normalised_to_match_the_polled_value(self):
        """`state: "OFF"` via mspa.set_filter sent the command but waited forever.

        The poll reports lowercase, and the expectation kept the caller's casing.
        """
        c = _trigger_coord()
        c.spa["filter_state"] = 1
        c._last_data["filter"] = "on"
        _run(c.set_feature_state("filter", "OFF"))
        assert c._pending_changes.get("filter") == "off", (
            f"unsatisfiable expectation: {c._pending_changes}")

    def test_uppercase_still_sends_the_right_command(self):
        c = _trigger_coord()
        c._last_data["filter"] = "on"
        _run(c.set_feature_state("filter", "OFF"))
        assert c.spa["filter_state"] == 0

    def test_a_refused_command_is_reported_before_the_wait(self):
        """Otherwise it surfaces 15 s later as 'did not confirm', which reads as slow
        rather than as refused."""
        c = _trigger_coord()
        c.api.set_filter_state = AsyncMock(return_value={"message": "DENIED"})
        seen = []
        with patch.object(coordinator_mod._LOGGER, "warning",
                          side_effect=lambda m, *a: seen.append(m % a if a else m)):
            _run(c.set_feature_state("filter", "off"))
        assert any("did not accept" in m for m in seen), seen

    def test_an_accepted_command_is_not_reported_as_refused(self):
        c = _trigger_coord()
        seen = []
        with patch.object(coordinator_mod._LOGGER, "warning",
                          side_effect=lambda m, *a: seen.append(m % a if a else m)):
            _run(c.set_feature_state("filter", "off"))
        assert not any("did not accept" in m for m in seen), seen

    def test_the_wait_is_armed_only_after_the_command_is_dispatched(self):
        """A failed dispatch must leave no expectation behind to time out on."""
        c = _trigger_coord()
        c.api.set_filter_state = AsyncMock(side_effect=RuntimeError("network"))
        with pytest.raises(RuntimeError):
            _run(c.set_feature_state("filter", "off"))
        assert c._pending_changes == {}, (
            f"armed a wait for a command that was never sent: {c._pending_changes}")


class TestScheduleTargetTempControl:
    """The schedule target is a box, and a change to it is logged.

    20-40 °C in 0.5 steps is 41 slider positions in a narrow control. A mis-drag is
    silent and expensive: on 2026-08-13 an accidental 39.5 → 38.0 moved the planned
    start 98 minutes later, corrected 29 s afterwards, and the only evidence was
    `sched=38.0°C` buried inside an unrelated sensor line.
    """

    def test_it_is_a_box_not_a_slider(self):
        from homeassistant.components.number import NumberMode
        from custom_components.mspa.number import MSpaScheduleTargetTemp
        assert MSpaScheduleTargetTemp._attr_mode is NumberMode.BOX

    def test_bubble_level_is_left_alone(self):
        """Three positions, and a wrong one is harmless — a slider suits it."""
        from custom_components.mspa.number import MspaBubbleLevelNumber
        assert "_attr_mode" not in vars(MspaBubbleLevelNumber)

    def test_a_change_is_logged_with_both_values(self):
        from custom_components.mspa import number as number_mod
        from custom_components.mspa.number import MSpaScheduleTargetTemp
        ent = MSpaScheduleTargetTemp.__new__(MSpaScheduleTargetTemp)
        ent.coordinator = MagicMock()
        ent.coordinator.schedule_target_temp = 39.5
        ent.async_write_ha_state = MagicMock()
        seen = []
        with patch.object(number_mod._LOGGER, "info",
                          side_effect=lambda m, *a: seen.append(m % a)):
            _run(MSpaScheduleTargetTemp.async_set_native_value(ent, 38.0))
        assert seen and "39.5" in seen[0] and "38.0" in seen[0], seen
        assert ent.coordinator.schedule_target_temp == 38.0

    def test_setting_the_same_value_is_not_logged(self):
        from custom_components.mspa import number as number_mod
        from custom_components.mspa.number import MSpaScheduleTargetTemp
        ent = MSpaScheduleTargetTemp.__new__(MSpaScheduleTargetTemp)
        ent.coordinator = MagicMock()
        ent.coordinator.schedule_target_temp = 39.5
        ent.async_write_ha_state = MagicMock()
        seen = []
        with patch.object(number_mod._LOGGER, "info",
                          side_effect=lambda m, *a: seen.append(m % a)):
            _run(MSpaScheduleTargetTemp.async_set_native_value(ent, 39.5))
        assert seen == []
