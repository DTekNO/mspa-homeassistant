"""The nowcast inside the coordinator: what it records, what resets it, what it publishes.

nowcast.py is the arithmetic and is tested on its own. This is the wiring: that every
crossing reaches it with the weather it was reached through, that the things which
invalidate a window actually discard it, that the live estimate is anchored on a crossing
rather than on a quantised reading, and that the one carried constant is learned from a
completed run and migrates from what the store already holds.

Run with: python -m pytest tests/test_nowcast_wiring.py -v
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.mspa import coordinator as coord_mod
from custom_components.mspa import nowcast as nc
from custom_components.mspa.coordinator import MSpaUpdateCoordinator, _read_weather_extras

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _real_clock():
    """The harness stubs homeassistant, so `dt_util` is a MagicMock and `utcnow()`
    returns one. A crossing's wall clock is what the projection is anchored on, so it
    has to be a real datetime here, and `parse_datetime` has to parse.

    Restored afterwards. `dt_util` is a module-level singleton shared with every other
    test module, and leaving a frozen clock behind broke the forecast-walk tests, which
    build their rows from the real one.
    """
    original = coord_mod.dt_util.utcnow, coord_mod.dt_util.parse_datetime
    coord_mod.dt_util.utcnow = lambda: _NOW
    coord_mod.dt_util.parse_datetime = datetime.fromisoformat
    yield
    coord_mod.dt_util.utcnow, coord_mod.dt_util.parse_datetime = original


def _coord(**over):
    c = object.__new__(MSpaUpdateCoordinator)
    c.ambient_temp = 12.0
    c.ambient_wind = None
    c.ambient_gust = None
    c.ambient_uv = None
    c.config_entry = type("E", (), {"options": {"heater_power_heat": 2200}})()
    c._window_amb_n = 0
    c._window_amb_sum = 0.0
    c._thermal_points = []
    c._thermal_chord = []
    c._thermal_crossings_seen = 0
    c._thermal_hot_rejects = 0
    c._nowcast_crossings = []
    c._nowcast_mixed = False
    c._crossing_log = []
    c._forecast_rows = []
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _cross(c, temps, *, minutes=27.0, start=0.0, air=None):
    """Feed crossings `minutes` apart on the monotonic clock the coordinator uses.

    `air` is a per-interval list when the weather is meant to move; the conditions are
    accumulated poll by poll, as they are in production, so the time weighting is being
    exercised rather than bypassed.
    """
    t = start
    for i, temp in enumerate(temps):
        if i and air is not None:
            c.ambient_temp = air[i - 1]
        if i:
            step = minutes * 60.0
            # Two polls an interval: enough to make the weighting observable.
            for _ in range(2):
                t += step / 2.0
                c.accumulate_nowcast_conditions(t)
        c.record_nowcast_crossing(t, temp)
    return c


class TestEveryCrossingIsRecorded:
    def test_a_crossing_carries_the_air_it_was_reached_through(self):
        c = _cross(_coord(), [30.0, 30.5], air=[4.0])
        assert c._nowcast_crossings[-1].air == pytest.approx(4.0)

    def test_the_opening_crossing_contributes_a_position_not_a_measurement(self):
        """It opens the window, and no interval produced it. Whatever air it carries is
        the instantaneous reading, and `measure` never looks at it — the window weights
        the intervals *between* its boundaries."""
        c = _cross(_coord(), [30.0, 30.5, 31.0, 31.5], air=[4.0, 4.0, 4.0])
        c._nowcast_crossings[0] = c._nowcast_crossings[0]._replace(air=-99.0)
        assert nc.measure(c._nowcast_crossings).air == pytest.approx(4.0)

    def test_the_air_is_time_weighted_across_the_interval(self):
        c = _coord()
        c.record_nowcast_crossing(0.0, 30.0)
        c.ambient_temp = 0.0
        c.accumulate_nowcast_conditions(600.0)            # ten minutes at 0 °C
        c.ambient_temp = 20.0
        c.accumulate_nowcast_conditions(600.0 + 1800.0)   # thirty minutes at 20 °C
        c.record_nowcast_crossing(600.0 + 1800.0, 30.5)
        assert c._nowcast_crossings[-1].air == pytest.approx(
            (0.0 * 600 + 20.0 * 1800) / 2400, rel=1e-6)

    def test_a_restart_gap_is_not_an_interval(self):
        """An hour with no poll is an outage. The crossing after it is still a fact;
        what the weather did during it is not, so it is not averaged in."""
        c = _coord()
        c.record_nowcast_crossing(0.0, 30.0)
        c.ambient_temp = 5.0
        c.accumulate_nowcast_conditions(30.0)
        c.ambient_temp = -40.0
        c.accumulate_nowcast_conditions(30.0 + 7200.0)    # two-hour gap, ignored
        c.record_nowcast_crossing(30.0 + 7200.0, 30.5)
        assert c._nowcast_crossings[-1].air == pytest.approx(5.0)

    def test_the_unmixed_crossings_are_recorded_too(self):
        """The thermal chord discards the first two; the nowcast must not, because its
        settling gate is a test on exactly those crossings."""
        c = _coord()
        c.heating_since = datetime.now(timezone.utc)
        for i in range(4):
            c.note_thermal_crossing(i * 1620.0, 30.0 + i * 0.5)
        assert len(c._nowcast_crossings) == 4
        assert c._thermal_points == [], "the chord still skips them"


class TestWhatDiscardsTheWindow:
    def test_cold_water_added_mid_run(self):
        """A falling reading means the window is no longer measuring one climb."""
        c = _cross(_coord(), [30.0, 30.5, 31.0, 31.5])
        c._nowcast_mixed = True
        c.record_nowcast_crossing(5 * 1620.0, 28.0)
        assert len(c._nowcast_crossings) == 1
        assert c._nowcast_mixed is False, "the tub may be stratified again"

    def test_the_heater_going_off(self):
        c = _cross(_coord(), [30.0, 30.5, 31.0, 31.5])
        c._rate_last_temp = 31.5
        c._rate_last_time = 0.0
        c._rate_prev_temp = 31.5
        c._rate_first_step = False
        c._bucket_base_bucket = None
        c._track_heating_rate(31.5, 0, 10_000.0)          # heat_state 0: off
        assert c._nowcast_crossings == []

    def test_a_reset_forgets_the_settled_latch(self):
        c = _cross(_coord(), [30.0, 30.5, 31.0, 31.5])
        c._nowcast_mixed = True
        c.reset_nowcast_run()
        assert c._nowcast_crossings == [] and c._nowcast_mixed is False


class TestTheSettledLatch:
    def _settled(self, rates, lift=55.0):
        c = _coord(nowcast_lift_c=lift)
        t, w = 0.0, 30.0
        c.record_nowcast_crossing(t, w)
        for r in rates:
            step = (0.5 / r) * 3600.0
            t += step
            c.accumulate_nowcast_conditions(t)
            w += 0.5
            c.record_nowcast_crossing(t, w)
        return c

    def test_unmixed_water_is_not_settled(self):
        assert self._settled([1.80, 1.60, 1.42, 1.30, 1.26, 1.25])._nowcast_mixed is False

    def test_it_settles_once_the_transient_has_passed(self):
        c = self._settled([1.80, 1.60, 1.42, 1.30, 1.26, 1.25, 1.24, 1.26, 1.23, 1.25])
        assert c._nowcast_mixed is True

    def test_once_settled_it_stays_settled(self):
        """A tub does not re-stratify while the heater runs, and the gate must not
        flicker on a rate that later changes for an honest reason — the sun coming out
        moved one band by 9 % on 08.10.2026. A real interruption resets the run instead.
        """
        c = self._settled([1.25] * 6)
        assert c._nowcast_mixed is True
        for extra in (0.90, 0.85, 0.80):
            c.accumulate_nowcast_conditions(c._nowcast_crossings[-1].mono + 1800.0)
            c.record_nowcast_crossing(
                c._nowcast_crossings[-1].mono + (0.5 / extra) * 3600.0,
                c._nowcast_crossings[-1].water + 0.5)
        assert c._nowcast_mixed is True

    def test_nothing_is_published_until_it_settles(self):
        c = self._settled([1.80, 1.60, 1.42])
        assert c.nowcast(39.5) is None, "an unmixed window must not answer"


class TestTheLiveFinish:
    def _running(self, rate=1.25):
        c = _coord(nowcast_lift_c=55.0)
        t, w = 0.0, 30.0
        c.record_nowcast_crossing(t, w)
        for _ in range(8):
            t += (0.5 / rate) * 3600.0
            c.accumulate_nowcast_conditions(t)
            w += 0.5
            c.record_nowcast_crossing(t, w)
        assert c._nowcast_mixed
        return c

    def test_it_is_anchored_on_the_crossing_not_on_the_reading(self):
        """Between boundaries the reading is known only to within half a degree, while a
        crossing time is exact. So the projection starts at the crossing."""
        c = self._running()
        n = c.nowcast(39.5)
        finish = c.nowcast_finish(39.5)
        assert finish is not None
        assert abs((finish - (_NOW + timedelta(minutes=n.minutes))).total_seconds()) < 1

    def test_a_run_on_schedule_is_left_alone(self):
        """Inside the band nothing is overdue, so the finish stands still while the
        clock moves. That is the property the slew used to be needed for."""
        c = self._running()
        on_time = c.nowcast_finish(39.5)
        coord_mod.dt_util.utcnow = lambda: _NOW + timedelta(minutes=5)
        assert c.nowcast_finish(39.5) == on_time

    def test_a_run_that_falls_behind_is_pushed_out(self):
        """If the next boundary is overdue the heater is not delivering what the window
        measured. Without this the finish would sit still while the tub failed to arrive
        at it, and the estimate would quietly become a wish."""
        c = self._running()
        n = c.nowcast(39.5)
        on_time = c.nowcast_finish(39.5)
        coord_mod.dt_util.utcnow = lambda: (
            _NOW + timedelta(minutes=n.step_minutes + 60.0))
        late = c.nowcast_finish(39.5)
        assert (late - on_time).total_seconds() / 60.0 == pytest.approx(60.0, abs=1.0)

    def test_a_faster_window_finishes_sooner(self):
        fast = self._running(rate=1.5).nowcast(39.5)
        slow = self._running(rate=1.0).nowcast(39.5)
        assert fast.minutes < slow.minutes
        assert fast.tau_h < slow.tau_h           # less water, shorter time constant

    def test_no_outdoor_reading_declines_rather_than_inventing_one(self):
        """The gap is the one term the window cannot supply for itself. Declining is
        what hands the answer to the bucket fallback, which is the documented
        no-weather-entity path."""
        c = _coord(nowcast_lift_c=55.0, ambient_temp=None)
        t, w = 0.0, 30.0
        for _ in range(8):
            c.record_nowcast_crossing(t, w)
            t += 1440.0
            c.accumulate_nowcast_conditions(t)
            w += 0.5
        assert c.nowcast(39.5) is None

    def test_the_diagnostics_describe_the_window_that_answered(self):
        c = self._running()
        c.scheduling_temp = lambda: 39.5
        d = c.nowcast_diagnostics()
        assert d["nowcast_settled"] is True
        assert d["nowcast_rate_c_per_h"] == pytest.approx(1.25, rel=0.02)
        assert d["nowcast_window_span_c"] == pytest.approx(1.5)
        assert d["nowcast_lift_c"] == pytest.approx(55.0)
        assert d["nowcast_a_c_per_h"] * d["nowcast_tau_h"] == pytest.approx(55.0, rel=1e-2)

    def test_the_diagnostics_hold_up_before_there_is_a_window(self):
        """They are read on every poll, including the ones with nothing to report."""
        c = _coord()
        c.scheduling_temp = lambda: 39.5
        d = c.nowcast_diagnostics()
        assert d["nowcast_crossings"] == 0
        assert d["nowcast_rate_c_per_h"] is None


class TestTheCarriedLift:
    def test_it_migrates_from_the_learned_pair(self):
        """No store surgery. An installation carrying a learned (A, tau) already carries
        the lift, as their product — 55.2 on this spa on 08.10.2026."""
        c = _coord(thermal_a=1.5047, thermal_tau_h=36.68, nowcast_lift_c=None)
        assert c.nowcast_lift() == pytest.approx(55.2, abs=0.1)

    def test_a_fresh_install_uses_the_seed(self):
        c = _coord(thermal_a=None, thermal_tau_h=None, nowcast_lift_c=None)
        assert c.nowcast_lift() == nc.DEFAULT_LIFT_C

    def test_a_completed_run_moves_it(self):
        c = _coord(nowcast_lift_c=55.0, nowcast_lift_n=3)
        c.learn_nowcast_lift(2.0, 30.0)                   # a run measuring 60
        assert 55.0 < c.nowcast_lift_c < 60.0
        assert c.nowcast_lift_n == 4

    def test_an_implausible_run_does_not(self):
        c = _coord(nowcast_lift_c=55.0, nowcast_lift_n=3)
        c.learn_nowcast_lift(4.0, 200.0)                  # 800: not a spa
        assert c.nowcast_lift_c == 55.0
        assert c.nowcast_lift_n == 3

    def test_tau_is_adopted_outright_and_feeds_the_lift(self):
        """The whole re-parameterisation. tau scales with the water mass, so it is what
        a refill changes and must not be averaged across runs that described different
        tubs; the lift is what survives a refill, so it is what gets the long memory.
        """
        c = _coord(thermal_a=1.50, thermal_tau_h=36.68, thermal_tau_n=5,
                   nowcast_lift_c=55.0, nowcast_lift_n=5)
        # A run whose crossings imply a shorter time constant — less water in the tub.
        c._thermal_points = [(g, 2.00 - g / 28.0) for g in
                             (6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0)]
        c.finalise_thermal_run()
        assert c.thermal_tau_h == pytest.approx(28.0, rel=0.02), "adopted, not blended"
        assert c.nowcast_lift_n == 6
        assert c._thermal_points == []


class TestTheCrossingLog:
    def test_a_row_stands_alone(self):
        c = _cross(_coord(), [30.0, 30.5, 31.0], air=[4.0, 6.0])
        row = c._crossing_log[-1]
        assert row["water"] == 31.0
        assert row["secs"] == pytest.approx(27 * 60, abs=1)
        assert row["air"] == pytest.approx(6.0)
        assert row["rate"] == pytest.approx(0.5 / 0.45, rel=0.01)
        assert row["gap"] is not None
        datetime.fromisoformat(row["t"])              # a real timestamp, not a float

    def test_the_first_row_has_no_interval(self):
        row = _cross(_coord(), [30.0])._crossing_log[-1]
        assert row["secs"] is None and row["rate"] is None

    def test_uv_and_wind_are_written_even_though_nothing_reads_them(self):
        c = _coord()
        c.record_nowcast_crossing(0.0, 30.0)
        c.ambient_uv, c.ambient_wind, c.ambient_gust = 1.2, 3.0, 7.5
        c.accumulate_nowcast_conditions(1620.0)
        c.record_nowcast_crossing(1620.0, 30.5)
        row = c._crossing_log[-1]
        assert row["uv"] == pytest.approx(1.2)
        assert row["wind"] == pytest.approx(3.0)
        assert row["gust"] == pytest.approx(7.5), "kept apart until one is shown to matter"

    def test_it_is_bounded(self):
        from custom_components.mspa.coordinator import _CROSSING_LOG_MAX
        c = _coord()
        c._crossing_log = [{"t": str(i)} for i in range(_CROSSING_LOG_MAX)]
        c.record_nowcast_crossing(0.0, 30.0)
        assert len(c._crossing_log) == _CROSSING_LOG_MAX
        assert c._crossing_log[-1]["water"] == 30.0

    def test_it_is_only_written_when_a_crossing_happened(self):
        """The rates store is rewritten every poll; this one must not be, or a thousand
        rows would mean about 300 MB a day onto the Pi's storage."""
        c = _coord()
        assert c._crossing_log_dirty is False
        c.accumulate_nowcast_conditions(30.0)
        assert c._crossing_log_dirty is False
        c.record_nowcast_crossing(60.0, 30.0)
        assert c._crossing_log_dirty is True


class TestReadingTheWeatherExtras:
    def _hass(self, attrs, state="sunny"):
        return SimpleNamespace(states=SimpleNamespace(
            get=lambda _eid: SimpleNamespace(state=state, attributes=attrs)))

    def test_wind_and_gust_come_back_separately(self):
        uv, wind, gust = _read_weather_extras(
            self._hass({"uv_index": 1.2, "wind_speed": 3.0,
                        "wind_gust_speed": 7.5, "wind_speed_unit": "m/s"}), "weather.x")
        assert (uv, wind, gust) == (1.2, 3.0, 7.5)

    @pytest.mark.parametrize("unit,factor", [
        ("km/h", 1 / 3.6), ("mph", 0.44704), ("kn", 0.514444), ("m/s", 1.0),
    ])
    def test_wind_is_normalised_to_metres_per_second(self, unit, factor):
        _, wind, _ = _read_weather_extras(
            self._hass({"wind_speed": 10.0, "wind_speed_unit": unit}), "weather.x")
        assert wind == pytest.approx(10.0 * factor)

    def test_an_absent_uv_index_is_none_rather_than_a_zero(self):
        """Zero is a reading: it is night. Absent is not, and a model fitted later must
        be able to tell them apart."""
        uv, _, _ = _read_weather_extras(self._hass({"wind_speed": 1.0}), "weather.x")
        assert uv is None

    def test_no_entity_and_an_unavailable_entity_both_answer_nothing(self):
        assert _read_weather_extras(self._hass({}), None) == (None, None, None)
        assert _read_weather_extras(
            self._hass({"uv_index": 1.0}, state="unavailable"),
            "weather.x") == (None, None, None)


# ── The run this was designed on ──────────────────────────────────────────────

_RUN_0810 = [
    # 08.10.2026, UTC. The power came back at 07:30 after a PRCD trip and the
    # integration restarted the heat-up into a tub that had been standing all night.
    ("07:30:42", 28.0), ("07:36:46", 28.5), ("07:56:46", 29.0), ("08:17:46", 29.5),
    ("08:40:46", 30.0), ("09:05:16", 30.5), ("09:32:16", 31.0), ("09:56:47", 31.5),
    ("10:21:46", 32.0), ("10:41:46", 32.5), ("11:11:16", 33.0), ("11:33:17", 33.5),
    ("11:57:18", 34.0), ("12:18:47", 34.5), ("12:50:17", 35.0),
]
_AIR_0810 = [
    ("06:17:38", 7.2), ("07:15:26", 7.3), ("07:21:26", 7.4), ("07:33:26", 7.6),
    ("07:39:26", 7.8), ("07:51:26", 8.1), ("07:57:26", 8.2), ("08:03:27", 8.3),
    ("08:15:27", 8.7), ("08:21:27", 8.9), ("08:33:27", 9.2), ("08:45:27", 9.3),
    ("08:57:28", 9.4), ("09:03:26", 9.6), ("09:15:27", 9.9), ("09:21:27", 10.1),
    ("09:33:27", 10.2), ("09:45:27", 10.3), ("09:51:26", 10.4), ("10:03:27", 10.6),
    ("10:15:26", 10.9), ("10:21:27", 11.3), ("10:33:29", 11.5), ("10:45:27", 11.6),
    ("10:51:26", 11.8), ("11:15:27", 11.9), ("11:21:27", 12.1), ("11:33:28", 12.3),
    ("11:45:27", 12.4), ("11:57:27", 12.5), ("12:03:27", 12.7), ("12:15:27", 13.0),
]
# The store's own values that morning. Their product is the lift, which is the whole
# migration: nothing had to be edited for the nowcast to start from the right place.
_LIFT_0810 = 1.5048 * 36.68
_TARGET_0810 = 39.5


def _at(hhmmss):
    return datetime.fromisoformat("2026-10-08T" + hhmmss + "+00:00")


class TestTheRunItWasDesignedOn:
    """08.10.2026 replayed through the coordinator, from the recorder's own history.

    A model that cannot reproduce a measurement already in hand is not worth deploying,
    and this run is the one the design came out of: a tub refilled to a lower level, a
    power cut at 22:08 the night before, and a learned model reading 22:55 local for
    water that arrived in the early evening.

    It also contains the thing the settling gate exists for. The first band after the
    power came back ran at 4.9 °C/h — the probe sits in the pump housing and the tub had
    been standing all night — against a settled rate near 1.2.
    """

    def _replay(self, upto=None):
        """Feed the real crossings and the real air, polling every 30 s between them."""
        c = _coord(nowcast_lift_c=_LIFT_0810, nowcast_lift_n=1,
                   thermal_a=1.5048, thermal_tau_h=36.68)
        air = [(_at(s), v) for s, v in _AIR_0810]

        def air_at(when):
            seen = air[0][1]
            for t, v in air:
                if t > when:
                    break
                seen = v
            return seen

        rows = _RUN_0810[:upto] if upto else _RUN_0810
        t0, prev, trace = _at(rows[0][0]), None, []
        for s, w in rows:
            when = _at(s)
            mono = (when - t0).total_seconds()
            if prev is not None:
                t = (prev - t0).total_seconds()
                while t < mono:
                    t = min(t + 30.0, mono)
                    c.ambient_temp = air_at(t0 + timedelta(seconds=t))
                    c.accumulate_nowcast_conditions(t)
            coord_mod.dt_util.utcnow = lambda when=when: when
            c.record_nowcast_crossing(mono, w)
            trace.append((s, w, c._nowcast_mixed, c.nowcast(_TARGET_0810),
                          c.nowcast_finish(_TARGET_0810)))
            prev = when
        return c, trace

    def test_the_stratified_opening_is_kept_out(self):
        """Five windows pass before the gate fires, and every one of them is wrong.

        The derived `tau` climbs 18.0 → 24.4 → 26.1 → 28.4 → 29.0 as the tub mixes,
        which is the climb the gate detects. Nothing is published through any of it.
        """
        _, trace = self._replay()
        settled_at = [s for s, _w, mixed, _n, _f in trace if mixed]
        assert settled_at[0] == "10:21:46", settled_at[:1]
        assert all(n is None for _s, _w, mixed, n, _f in trace if not mixed)

    def test_the_first_window_would_have_been_absurd(self):
        """What the gate is worth. Taken at the fourth crossing the window implies
        A = 3.06 °C/h on a 2.2 kW heater — about 650 litres of water — and a tau of 18 h.
        Published, it would have put the finish hours early and then walked it back all
        day, which is the failure the old design is being replaced for.
        """
        c, _ = self._replay(upto=4)
        win = nc.measure(c._nowcast_crossings)
        tau_h, a = nc.derive(win, _LIFT_0810)
        assert a > 3.0 and tau_h < 20.0
        assert c.nowcast(_TARGET_0810) is None, "and the gate refused it"

    def test_the_settled_window_measures_this_water_not_the_stored_tub(self):
        """The finding the whole redesign rests on. Every settled window puts `tau` near
        28 h; the value carried in the store was 36.68, which described about 1257 litres
        against the roughly 1000 actually in the tub after the refill.
        """
        _, trace = self._replay()
        taus = [n.tau_h for _s, _w, mixed, n, _f in trace if mixed]
        assert len(taus) == 7
        assert all(24.0 < t < 30.0 for t in taus), taus
        assert max(taus) < 36.68, "the stored tau described a fuller tub"

    def test_it_agrees_with_what_the_tub_actually_did(self):
        """Every settled crossing puts the finish inside a 40-minute band around 17:00
        UTC — a tub at 28 °C at 07:30 reaching 39.5 in the early evening. The learned
        model said 20:55 UTC that afternoon, which is nearly four hours later.

        The residual scatter is the single-band noise the window exists to average: the
        bands either side of 12:18 ran 1.40 and 0.95 °C/h, and that is real variation in
        the sun on the cover rather than measurement error.
        """
        _, trace = self._replay()
        finishes = [f for _s, _w, mixed, _n, f in trace if mixed]
        assert len(finishes) == 7
        for f in finishes:
            assert abs((f - _at("17:00:00")).total_seconds()) < 2400, f.isoformat()
        assert max(finishes) < _at("20:55:00"), "the learned model's answer"

    def test_the_whole_run_is_in_the_crossing_log(self):
        """Ten days from now the recorder will have purged the states and kept hourly
        statistics, which destroys the crossing times this method rests on."""
        c, _ = self._replay()
        assert len(c._crossing_log) == len(_RUN_0810)
        assert c._crossing_log[1]["secs"] == pytest.approx(364, abs=2)
        assert c._crossing_log[1]["rate"] == pytest.approx(4.945, rel=0.01), (
            "the stratified opening band is recorded, not hidden")


class TestARestartMidRun:
    """A hot deploy is a restart mid-run, and the crossings live on the monotonic clock.

    Without the log the window would have to be collected again from scratch — about two
    hours of showing the learned fallback, which is the one estimate this design exists to
    stop showing.
    """

    def _logged(self, rates, *, age_min=5.0, start=30.0):
        """A crossing log as a run would have written it, ending `age_min` ago."""
        rows, w = [], start + 0.5 * len(rates)
        t = _NOW - timedelta(minutes=age_min)
        for r in reversed(rates):
            rows.append({"t": t.isoformat(timespec="seconds"), "water": w,
                         "air": 12.0, "rate": r})
            t -= timedelta(hours=0.5 / r)
            w -= 0.5
        rows.append({"t": t.isoformat(timespec="seconds"), "water": w, "air": None})
        return list(reversed(rows))

    def test_the_window_comes_back_intact(self):
        c = _coord(nowcast_lift_c=55.0, _crossing_log=self._logged([1.25] * 6))
        c.restore_nowcast_run()
        assert len(c._nowcast_crossings) == 7
        win = nc.measure(c._nowcast_crossings)
        assert win.rate == pytest.approx(1.25, rel=1e-3), "the intervals survived"
        assert c._nowcast_mixed is True, "and so did the settled verdict"

    def test_the_estimate_is_available_immediately(self):
        c = _coord(nowcast_lift_c=55.0, _crossing_log=self._logged([1.25] * 6))
        c.restore_nowcast_run()
        assert c.nowcast(39.5) is not None
        assert c.nowcast_finish(39.5) is not None

    def test_a_stale_log_is_a_previous_run(self):
        """Splicing a run from yesterday onto this one would measure a rate across the
        gap between them."""
        c = _coord(nowcast_lift_c=55.0,
                   _crossing_log=self._logged([1.25] * 6, age_min=600.0))
        c.restore_nowcast_run()
        assert c._nowcast_crossings == []

    def test_an_earlier_run_in_the_same_log_is_cut_away(self):
        """The log is a rolling record, so it holds the run before this one too. The
        window restarts at the gap between them — six hours with the heater off, which no
        0.5 °C band takes."""
        old = self._logged([1.25] * 6, age_min=600.0, start=20.0)
        new = self._logged([1.25] * 5, age_min=5.0, start=30.0)
        c = _coord(nowcast_lift_c=55.0, _crossing_log=old + new)
        c.restore_nowcast_run()
        assert all(x.water >= 30.0 for x in c._nowcast_crossings)
        assert len(c._nowcast_crossings) == len(new)

    def test_an_empty_log_leaves_the_run_empty(self):
        c = _coord(nowcast_lift_c=55.0, _crossing_log=[])
        c.restore_nowcast_run()
        assert c._nowcast_crossings == [] and c._nowcast_mixed is False


class TestADropIsTheMostInterestingRow:
    def test_it_is_logged_before_the_window_is_dropped(self):
        """An unexplained gap in the log would be worse than a row with a negative rate
        in it, and the restore path already cuts the window at a fall rather than
        reading across one."""
        c = _cross(_coord(), [30.0, 30.5, 31.0, 31.5])
        rows_before = len(c._crossing_log)
        c.record_nowcast_crossing(5 * 1620.0, 28.0)
        assert len(c._crossing_log) == rows_before + 1
        assert c._crossing_log[-1]["water"] == 28.0
        assert c._crossing_log[-1]["rate"] < 0, "the fall is visible in the file"
        assert len(c._nowcast_crossings) == 1, "and the window still restarts"


class TestASoakAndARefillAreNotAHeatUp:
    """08.10.2026, and what it cost.

    A clean run took the tub 28.0 → 38.5 over nine hours. Then it was used, then 150 L at
    well temperature went in, and the water fell to 34.5. The heater never left full heat
    through any of it, so nothing cancelled the session — the only cancellation was on the
    heater stopping — and when the water came back to 39.5 at 01:27 the next morning it
    was recorded as one eighteen-hour heat-up to target.

    That single row took the scored mean absolute error from 63 to 147 minutes and drove
    `prediction_bias` to its 1.1 ceiling, which makes every later estimate 10 %
    pessimistic. Every individual band inside the session was correctly discarded by
    `_window_looks_unmeasurable`; nothing applied the same test at session scale.
    """

    def _running(self, start=28.0, target=39.5):
        c = _coord()
        c._prediction = {"start_temp": start, "peak_temp": start,
                         "target_temp": target, "estimated_minutes": 540.0,
                         "start_time": _NOW.isoformat()}
        c._shadow = object()
        c._thermal_points = [(20.0, 1.2), (22.0, 1.1)]
        return c

    def test_the_peak_follows_the_water_up(self):
        c = self._running()
        for w in (28.5, 31.0, 38.5):
            c._note_session_disturbance(w)
        assert c._prediction is not None
        assert c._prediction["peak_temp"] == 38.5

    def test_ordinary_overshoot_is_not_a_disturbance(self):
        """The reading is quantised to 0.5 °C and thermostat overshoot is one step."""
        c = self._running()
        c._note_session_disturbance(38.5)
        c._note_session_disturbance(38.0)
        assert c._prediction is not None, "one step down is not a refill"

    def test_the_0810_fall_cancels_the_session(self):
        c = self._running()
        c._note_session_disturbance(38.5)
        c._note_session_disturbance(34.5)          # soak, then 150 L of well water
        assert c._prediction is None
        assert c._shadow is None

    def test_the_mixed_volume_chord_points_are_cleared(self):
        """They described the tub before 150 L went into it. Left in place they would be
        fitted together with the reheat's, giving a tau for neither volume."""
        c = self._running()
        c._note_session_disturbance(38.5)
        c._note_session_disturbance(34.5)
        assert c._thermal_points == []

    def test_the_nowcast_window_goes_with_it(self):
        c = self._running()
        c._nowcast_crossings = [nc.Crossing(0.0, 38.0, 12.0)]
        c._nowcast_mixed = True
        c._note_session_disturbance(38.5)
        c._note_session_disturbance(34.5)
        assert c._nowcast_crossings == [] and c._nowcast_mixed is False

    def test_it_does_nothing_outside_a_session(self):
        c = _coord()
        c._prediction = None
        c._note_session_disturbance(20.0)          # must not raise
        assert c._prediction is None

    def test_an_unreadable_temperature_cannot_cancel_a_session(self):
        """A missing reading is not evidence of anything, and discarding a nine-hour
        measurement on one is the more expensive mistake."""
        c = self._running()
        c._note_session_disturbance(38.5)
        c._note_session_disturbance(None)
        c._note_session_disturbance("unavailable")
        assert c._prediction is not None
