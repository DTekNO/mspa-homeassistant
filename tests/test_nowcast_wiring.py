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
    has to be a real datetime here.

    Restored afterwards. `dt_util` is a module-level singleton shared with every other
    test module, and leaving a frozen clock behind broke the forecast-walk tests, which
    build their rows from the real one.
    """
    original = coord_mod.dt_util.utcnow
    coord_mod.dt_util.utcnow = lambda: _NOW
    yield
    coord_mod.dt_util.utcnow = original


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
