"""The localised companions to the Ready at and Heat Schedule text sensors.

Those two build their display strings server-side with strftime and English
literals, which no per-user setting can reach: Home Assistant's time format is a
frontend preference with no server-side equivalent, and an entity state is one
string shared by every viewer.  The companions hand the job to the two frontend
mechanisms that can do it — a timestamp state, which is rendered in the viewer's
own format and timezone, and an enum state, whose whole value is a translation
key.

What these tests protect, in order of how loudly it fails in production:

  * an enum sensor whose state is not in its own `options` raises ValueError
    inside HA on every update — a token added without its option is a dead
    entity, and nothing else in the suite would catch it;
  * an option without a translation renders as the raw token on the dashboard;
  * a companion that disagrees with the sensor it mirrors is worse than either
    entity alone, because now the spa has two opinions about when it is ready.

Run with: python -m pytest tests/test_localised_sensors.py -v
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.mspa.sensor import (
    MSpaHeatScheduleSensor,
    MSpaHeatScheduleStartSensor,
    MSpaHeatScheduleStatusSensor,
    MSpaReadinessSensor,
    MSpaReadyAtTimeSensor,
    MSpaReadyStatusSensor,
)
from tests.test_schedule_scenarios import (
    _NOW_UTC,
    MockConfigEntry,
    MockCoordinator,
    _HeatScheduleStub,
)

_PKG = Path(__file__).parent.parent / "custom_components" / "mspa"


@pytest.fixture(autouse=True)
def freeze_time():
    """Same frozen clock the schedule scenarios use; the sensors read dt_util."""
    with patch("custom_components.mspa.sensor.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = _NOW_UTC
        mock_dt.as_utc.side_effect = (
            lambda dt: dt.astimezone(timezone.utc) if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc))
        mock_dt.as_local.side_effect = lambda dt: dt
        mock_dt.now.return_value = _NOW_UTC
        yield mock_dt


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _ReadinessStub:
    """The readiness sensor's display derivation, without an HA entity behind it."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._eta_display = None
        self._eta_wall = None
        self._eta_plan_key = None
        self._eta_closing = False

    available = True
    display_ready_at = MSpaReadinessSensor.display_ready_at
    # staticmethod on the real class; unwrapped by the class access above, so it
    # has to be rewrapped or `self` gets passed as the datetime.
    _round_eta = staticmethod(MSpaReadinessSensor._round_eta)
    _slew_eta = MSpaReadinessSensor._slew_eta
    _replan_key = MSpaReadinessSensor._replan_key
    native_value = MSpaReadinessSensor.native_value


class _ScheduleStub(_HeatScheduleStub):
    display_schedule = MSpaHeatScheduleSensor.display_schedule
    native_value = MSpaHeatScheduleSensor.native_value


def _ready_time(readiness):
    return MSpaReadyAtTimeSensor.native_value.fget(
        type("S", (), {"_readiness": readiness})())


def _ready_status(readiness):
    stub = type("S", (), {"_readiness": readiness,
                          "_KINDS": MSpaReadyStatusSensor._KINDS})()
    return MSpaReadyStatusSensor.native_value.fget(stub)


def _ready_compact(readiness):
    stub = type("S", (), {"_readiness": readiness,
                          "native_value": _ready_time(readiness)})()
    return MSpaReadyAtTimeSensor.extra_state_attributes.fget(stub)["compact"]


def _sched_start(schedule):
    return MSpaHeatScheduleStartSensor.native_value.fget(
        type("S", (), {"_schedule": schedule})())


def _sched_status(schedule):
    return MSpaHeatScheduleStatusSensor.native_value.fget(
        type("S", (), {"_schedule": schedule})())


def _state_of(readiness):
    """The deprecated text sensor's state, with its slew side effects applied."""
    return MSpaReadinessSensor.native_value.fget(readiness)


# ═══════════════════════════════════════════════════════════════════════════════
# THE CONTRACT WITH HOME ASSISTANT
# ═══════════════════════════════════════════════════════════════════════════════

class TestTheEnumContract:
    """An enum sensor may only ever publish a state it declared up front."""

    def test_every_ready_status_token_is_declared(self):
        assert set(MSpaReadyStatusSensor._KINDS.values()).issubset(
            set(MSpaReadyStatusSensor._attr_options))

    def test_every_schedule_status_token_is_declared(self):
        """Drive all six branches and check each answer against the options.

        The mapping lives inside display_schedule rather than in a table, so the
        only honest way to enumerate it is to make the schedule do each thing.
        """
        entry = MockConfigEntry()
        cases = {
            "not_scheduled": MockCoordinator(scheduled_ready_at=None),
            "ready": MockCoordinator(
                scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
                schedule_target_temp=39.0, water_temp=39.0, ready_latched=True),
            "heating": MockCoordinator(
                scheduled_ready_at=_NOW_UTC + timedelta(hours=4),
                schedule_target_temp=40.0, water_temp=30.0,
                schedule_triggered=True),
            "scheduled": MockCoordinator(
                scheduled_ready_at=_NOW_UTC + timedelta(days=6),
                schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0),
            "waiting": MockCoordinator(
                scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
                schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0),
            "start_now": MockCoordinator(
                scheduled_ready_at=_NOW_UTC + timedelta(minutes=20),
                schedule_target_temp=39.0, water_temp=37.5, heat_rate=2.0),
        }
        seen = {}
        for expected, c in cases.items():
            seen[expected] = _sched_status(_ScheduleStub(c, entry))

        assert seen == {k: k for k in cases}, seen
        assert set(seen.values()).issubset(
            set(MSpaHeatScheduleStatusSensor._attr_options))

    @pytest.mark.parametrize("sensor", [MSpaReadyStatusSensor,
                                        MSpaHeatScheduleStatusSensor])
    def test_options_have_no_duplicates(self, sensor):
        assert len(sensor._attr_options) == len(set(sensor._attr_options))


class TestTheTranslationContract:
    """Every declared option needs English text, or the raw token reaches the UI."""

    @staticmethod
    def _states(path, key):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["entity"]["sensor"][key]["state"]

    @pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
    @pytest.mark.parametrize("sensor,key", [
        (MSpaReadyStatusSensor, "ready_status"),
        (MSpaHeatScheduleStatusSensor, "heat_schedule_status"),
    ])
    def test_every_option_is_translated(self, filename, sensor, key):
        states = self._states(_PKG / filename, key)
        assert set(sensor._attr_options) == set(states), (
            f"{filename}:{key} does not match the sensor's options")
        assert all(v.strip() for v in states.values())

    def test_strings_and_english_agree(self):
        """strings.json is the source hassfest reads; en.json is what ships."""
        with open(_PKG / "strings.json", encoding="utf-8") as fh:
            src = json.load(fh)["entity"]
        with open(_PKG / "translations/en.json", encoding="utf-8") as fh:
            en = json.load(fh)["entity"]
        assert src == en

    def test_the_translation_key_matches_the_file(self):
        """A typo here is silent: the frontend just falls back to the token."""
        assert MSpaReadyStatusSensor._attr_translation_key == "ready_status"
        assert (MSpaHeatScheduleStatusSensor._attr_translation_key
                == "heat_schedule_status")


class TestTheTimestampContract:
    """Without the device class the state is a bare ISO string on the dashboard."""

    @pytest.mark.parametrize("sensor", [MSpaReadyAtTimeSensor,
                                        MSpaHeatScheduleStartSensor])
    def test_declares_the_timestamp_device_class(self, sensor):
        from custom_components.mspa.sensor import SensorDeviceClass
        assert sensor._attr_device_class is SensorDeviceClass.TIMESTAMP

    @pytest.mark.parametrize("sensor", [
        MSpaReadyAtTimeSensor, MSpaReadyStatusSensor,
        MSpaHeatScheduleStartSensor, MSpaHeatScheduleStatusSensor,
    ])
    def test_carries_no_options_unless_it_is_an_enum(self, sensor):
        """HA raises if a non-enum sensor offers options, and vice versa."""
        from custom_components.mspa.sensor import SensorDeviceClass
        is_enum = sensor._attr_device_class is SensorDeviceClass.ENUM
        assert hasattr(sensor, "_attr_options") is is_enum


# ═══════════════════════════════════════════════════════════════════════════════
# AGREEMENT WITH THE SENSOR EACH COMPANION MIRRORS
# ═══════════════════════════════════════════════════════════════════════════════

class TestReadyAtAgreement:

    def _heating(self):
        return _ReadinessStub(MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=20.0, target_temp=40.0, heat_rate=2.0, heater="on"))

    def test_the_timestamp_is_the_time_the_text_state_shows(self):
        """Same instant, and the comparison has to convert to make that visible.

        _fmt_local bakes the *server's* timezone into the string; the companion
        publishes UTC and lets each viewer's profile place it.  That difference is
        the feature, so the test converts rather than pretending it is not there.
        """
        r = self._heating()
        state = _state_of(r)
        assert re.match(r"^\d{2}:\d{2}", state), state
        ts = _ready_time(r)
        assert ts is not None
        assert ts.tzinfo is not None, "a timestamp state must be timezone-aware"
        assert ts.astimezone().strftime("%H:%M") == state[:5]

    def test_the_timestamp_follows_the_slew_not_the_raw_estimate(self):
        """The companion must not re-derive: the text sensor shows a slewed ETA.

        Two entities that disagree about when the spa is ready are worse than one,
        so the companion reads the slewed position rather than computing its own.
        """
        r = self._heating()
        _state_of(r)                                  # establishes the slew position
        held = r._eta_display
        r._eta_display = held - timedelta(hours=3)    # displayed value moves
        assert _ready_time(r) == MSpaReadinessSensor._round_eta(r._eta_display)

    def test_ready_has_a_status_but_no_timestamp(self):
        c = MockCoordinator(water_temp=40.0, target_temp=40.0,
                            near_target=True, ready_latched=True)
        r = _ReadinessStub(c)
        assert _state_of(r) == "Ready"
        assert _ready_time(r) is None
        assert _ready_status(r) == "ready"

    def test_no_opinion_is_unknown_rather_than_an_invented_token(self):
        """kind 'none' publishes nothing; HA translates unknown on its own."""
        r = self._heating()
        with patch("custom_components.mspa.sensor._compute_ready_at",
                   return_value=("none", None)):
            assert _ready_status(r) is None
            assert _ready_time(r) is None

    @pytest.mark.parametrize("kind,token", [
        ("ready", "ready"), ("eta", "heating"), ("sched", "scheduled"),
    ])
    def test_each_kind_maps_to_its_token(self, kind, token):
        r = self._heating()
        when = _NOW_UTC + timedelta(hours=2)
        with patch("custom_components.mspa.sensor._compute_ready_at",
                   return_value=(kind, None if kind == "ready" else when)):
            assert _ready_status(r) == token

    def test_a_scheduled_time_is_shown_verbatim(self):
        """Scheduled times are the user's own and are never slewed."""
        r = self._heating()
        when = _NOW_UTC + timedelta(hours=5)
        r._eta_display = _NOW_UTC + timedelta(hours=99)   # would win if slewed
        with patch("custom_components.mspa.sensor._compute_ready_at",
                   return_value=("sched", when)):
            assert _ready_time(r) == when


class TestHeatScheduleAgreement:

    def _waiting(self):
        return _ScheduleStub(MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0),
            MockConfigEntry())

    def test_the_timestamp_is_the_time_the_text_state_shows(self):
        # No timezone conversion here, unlike the Ready at case: the schedule text
        # goes through dt_util.as_local, which the fixture patches to identity.
        s = self._waiting()
        state = s.native_value
        m = re.match(r"^Start at (\d{2}:\d{2})", state)
        assert m, state
        start = _sched_start(s)
        assert start is not None
        assert start.strftime("%H:%M") == m.group(1)

    def test_the_held_start_is_shared_not_recomputed(self):
        """Both must report the held start, so they cannot drift apart.

        _slew_start holds the displayed start against ambient drift; a companion
        that called _schedule_data itself would publish the live plan instead and
        the two entities would disagree for hours at a time.
        """
        s = self._waiting()
        s.native_value                                  # establishes the hold
        held = s._start_shown
        assert _sched_start(s) == held

    @pytest.mark.parametrize("token", ["not_scheduled", "ready", "heating",
                                       "scheduled"])
    def test_states_without_a_start_time_publish_none(self, token):
        """Only 'waiting' and 'start_now' have a time; the rest must be blank."""
        s = self._waiting()
        with patch.object(_ScheduleStub, "display_schedule",
                          lambda self: (token, None)):
            assert _sched_start(s) is None
            assert _sched_status(s) == token

    def test_start_now_still_carries_its_time(self):
        """The moment to act on is worth showing even once it has arrived."""
        s = _ScheduleStub(MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(minutes=20),
            schedule_target_temp=39.0, water_temp=37.5, heat_rate=2.0),
            MockConfigEntry())
        assert s.native_value == "Start now"
        assert _sched_status(s) == "start_now"
        assert _sched_start(s) is not None


class TestTheOriginalsAreUntouched:
    """The text sensors are deprecated, not repurposed — dashboards still read them."""

    def test_ready_at_still_renders_a_24_hour_string(self):
        r = _ReadinessStub(MockCoordinator(
            water_temp=20.0, target_temp=40.0, heat_rate=2.0, heater="on"))
        assert re.match(r"^\d{2}:\d{2}( \+\d+d)?$", _state_of(r))

    def test_heat_schedule_still_renders_start_at(self):
        s = _ScheduleStub(MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0),
            MockConfigEntry())
        assert re.match(r"^Start at \d{2}:\d{2}( \+\d+d)?$", s.native_value)


class TestTheCompactAttribute:
    """The escape hatch for places the localised state will not fit.

    A picture-elements state-label renders a timestamp state as "11 September 2026 at
    14:00", which is right and unusable in the corner of a photo. That element prints an
    `attribute:` raw, so a pre-formatted string put there arrives on screen verbatim.
    Server-formatted and therefore not localised — which is exactly why it is an
    attribute and not the state.
    """

    def _heating(self):
        return _ReadinessStub(MockCoordinator(
            near_target=False, ready_latched=False,
            water_temp=20.0, target_temp=40.0, heat_rate=2.0, heater="on"))

    def test_it_has_the_shape_the_deprecated_state_had(self):
        """A dashboard moving off the text sensor should see the same kind of string.

        Not asserted byte-for-byte against that state: the old _fmt_local formats in the
        *machine's* timezone and _fmt_compact in Home Assistant's configured one. They
        agree on a normal install and the new one is right where they do not.
        """
        r = self._heating()
        compact = _ready_compact(r)
        assert re.match(r"^\d{2}:\d{2}( \+\d+d)?$", compact), compact
        assert compact[:5] == _ready_time(r).strftime("%H:%M")   # as_local is identity here

    def test_it_is_none_when_there_is_no_time(self):
        """A stale badge is worse than none: it would say a spa still had time to go."""
        r = self._heating()
        with patch("custom_components.mspa.sensor._compute_ready_at",
                   return_value=("ready", None)):
            assert _ready_time(r) is None
            assert _ready_compact(r) is None

    def test_the_schedule_start_carries_no_prefix(self):
        """"Start at" belongs to whatever displays it, not to the value."""
        s = _ScheduleStub(MockCoordinator(
            scheduled_ready_at=_NOW_UTC + timedelta(hours=12),
            schedule_target_temp=40.0, water_temp=20.0, heat_rate=2.0),
            MockConfigEntry())
        state = s.native_value
        stub = type("S", (), {"_schedule": s,
                              "native_value": _sched_start(s)})()
        compact = MSpaHeatScheduleStartSensor.extra_state_attributes.fget(
            stub)["compact"]
        assert compact and not compact.startswith("Start at")
        assert state == f"Start at {compact}"

    def test_it_stays_out_of_the_state(self):
        """The state must remain a real timestamp or Home Assistant cannot localise it."""
        r = self._heating()
        assert isinstance(_ready_time(r), datetime)
