"""The connectivity sensor: can an automation tell that the spa has gone?

Written after 07.10.2026, when the spa's portable RCD tripped at 22:08 UTC during a
heat-up and nobody knew until the following morning. Every mspa entity went
`unavailable`, which is the one state an automation cannot usefully trigger on and
is indistinguishable from a restart or a reload.

Two behaviours matter and both are tested here. The entity must stay available when
everything around it is not, and it must sit out the short outages: the daily
broadband blip (~30 s) and the vendor cloud missing single polls (23 of them in ten
hours on 07.10, while the spa never left the wifi).

Run with: python -m pytest tests/test_connectivity.py -v
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.mspa import binary_sensor as bs_mod
from custom_components.mspa.binary_sensor import MSpaConnectivity, connectivity
from custom_components.mspa.const import (
    CONF_OFFLINE_AFTER_MINUTES, DEFAULT_OFFLINE_AFTER_MINUTES,
)

_NOW = datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)
_GRACE = DEFAULT_OFFLINE_AFTER_MINUTES * 60


def _freeze(monkeypatch, now=None):
    """Pin the module's clock. Patching the shared dt_util would reach other code."""
    monkeypatch.setattr(bs_mod, "dt_util", SimpleNamespace(utcnow=lambda: now or _NOW))


def _sensor(last_seen=_NOW, options=None, update_success=True):
    coord = MagicMock()
    coord.device_id = "dev"
    coord.last_seen_utc = last_seen
    coord.last_update_success = update_success
    coord.config_entry = MagicMock()
    coord.config_entry.options = options if options is not None else {}
    s = object.__new__(MSpaConnectivity)
    s.coordinator = coord
    return s


class TestGraceWindow:
    """Short gaps are not outages."""

    @pytest.mark.parametrize("seconds", [0, 30, 60, 300, _GRACE])
    def test_short_silence_is_still_connected(self, seconds):
        state, offline = connectivity(_NOW - timedelta(seconds=seconds), _NOW, _GRACE)
        assert state is True
        assert offline == pytest.approx(seconds)

    def test_the_starlink_blip_does_not_register(self):
        # Every entity drops for about 30 s once a day. See starlink-daily-blip.
        assert connectivity(_NOW - timedelta(seconds=30), _NOW, _GRACE)[0] is True

    def test_a_missed_cloud_poll_does_not_register(self):
        # 07.10.2026: 23 of these in ten hours, each exactly one 30 s poll cycle,
        # while the router still showed the spa associated the whole time.
        assert connectivity(_NOW - timedelta(seconds=30), _NOW, _GRACE)[0] is True

    def test_past_the_window_is_offline(self):
        state, offline = connectivity(_NOW - timedelta(seconds=_GRACE + 1), _NOW, _GRACE)
        assert state is False
        assert offline == pytest.approx(_GRACE + 1)

    def test_the_real_outage_registers(self):
        # The PRCD trip: gone from 22:08 to the next morning.
        state, offline = connectivity(
            datetime(2026, 10, 7, 22, 8, 47, tzinfo=timezone.utc), _NOW, _GRACE)
        assert state is False
        assert offline / 3600 == pytest.approx(8.85, abs=0.05)

    def test_never_seen_is_unknown_not_offline(self):
        # A fresh install with bad credentials has never seen the spa either;
        # reporting "disconnected" there would be a guess.
        assert connectivity(None, _NOW, _GRACE) == (None, None)

    def test_a_clock_step_backwards_does_not_go_negative(self):
        state, offline = connectivity(_NOW + timedelta(minutes=5), _NOW, _GRACE)
        assert state is True
        assert offline == 0.0


class TestAlwaysAvailable:
    """The entity that reports outages cannot opt out of existing during one."""

    def test_available_when_the_coordinator_has_failed(self):
        s = _sensor(last_seen=_NOW - timedelta(hours=9), update_success=False)
        assert s.available is True

    def test_and_then_reports_offline(self, monkeypatch):
        s = _sensor(last_seen=_NOW - timedelta(hours=9), update_success=False)
        _freeze(monkeypatch)
        assert s.is_on is False

    def test_available_before_the_spa_has_ever_answered(self):
        assert _sensor(last_seen=None, update_success=False).available is True


class TestEntity:
    def test_connected(self, monkeypatch):
        _freeze(monkeypatch)
        assert _sensor().is_on is True

    def test_unknown_when_never_seen(self, monkeypatch):
        _freeze(monkeypatch)
        assert _sensor(last_seen=None).is_on is None

    def test_attributes_carry_the_raw_material(self, monkeypatch):
        _freeze(monkeypatch)
        last = _NOW - timedelta(minutes=90)
        a = _sensor(last_seen=last).extra_state_attributes
        assert a["last_seen"] == last.isoformat()
        assert a["offline_minutes"] == pytest.approx(90.0)
        assert a["grace_minutes"] == pytest.approx(DEFAULT_OFFLINE_AFTER_MINUTES)

    def test_attributes_when_never_seen(self, monkeypatch):
        _freeze(monkeypatch)
        a = _sensor(last_seen=None).extra_state_attributes
        assert a["last_seen"] is None and a["offline_minutes"] is None


class TestConfiguredWindow:
    def test_option_is_honoured(self, monkeypatch):
        _freeze(monkeypatch)
        s = _sensor(last_seen=_NOW - timedelta(minutes=20),
                    options={CONF_OFFLINE_AFTER_MINUTES: 30})
        assert s.is_on is True
        assert s.extra_state_attributes["grace_minutes"] == pytest.approx(30.0)

    @pytest.mark.parametrize("bad", [None, "", "abc", 0, -5])
    def test_a_nonsense_window_falls_back_to_the_default(self, bad):
        s = _sensor(options={CONF_OFFLINE_AFTER_MINUTES: bad})
        assert s._grace_seconds == DEFAULT_OFFLINE_AFTER_MINUTES * 60

    def test_a_string_number_is_accepted(self):
        # Number selectors can hand back a string from a hand-edited entry.
        s = _sensor(options={CONF_OFFLINE_AFTER_MINUTES: "25"})
        assert s._grace_seconds == 25 * 60
