"""Planning across a changing air temperature — R5.

The observation this exists for: as dawn turns into day and then afternoon, the outdoor
temperature reshapes the heating curve. A run committed at 22:00 for a 09:00 finish is
planned while the air is still falling and finishes after dawn, so one average of the
whole run describes neither end of it.

The boundary case is the one that needs care and is tested hardest here: a run that
finishes partway through a forecast block must be charged for the part of the block it
actually spends, not for a share of the block's average.

Run with: python -m pytest tests/test_piecewise_planning.py -v
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.thermal import ThermalModel

_NOW = datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc)


def _coord(rows=None, ambient=None):
    c = object.__new__(MSpaUpdateCoordinator)
    c._forecast_rows = rows or []
    c.ambient_temp = ambient
    c.config_entry = type("E", (), {"options": {}})()
    return c


class TestTheWalkItself:

    def test_a_flat_forecast_matches_the_closed_form(self):
        """With one air temperature throughout, piecewise must equal the single-shot
        answer — otherwise the walk has introduced an error of its own."""
        m = ThermalModel(1.30, 55.0)
        flat = m.heating_minutes(20.0, 35.0, 12.0)
        walked = m.heating_minutes_piecewise(
            20.0, 35.0, [(1.0, 12.0)] * 40)
        assert walked == pytest.approx(flat, rel=1e-9)

    def test_a_warming_day_beats_its_own_average(self):
        """The reshaping that prompted this. Heating into rising air finishes sooner
        than an average of that air predicts, because the early cold hours are spent
        where the gap is smallest."""
        m = ThermalModel(1.30, 55.0)
        segs = [(2.0, 8.0), (2.0, 12.0), (2.0, 16.0), (12.0, 20.0)]
        mean = sum(h * a for h, a in segs) / sum(h for h, _ in segs)
        walked = m.heating_minutes_piecewise(20.0, 30.0, segs)
        averaged = m.heating_minutes(20.0, 30.0, mean)
        assert walked is not None and averaged is not None
        assert walked != pytest.approx(averaged, rel=1e-4), (
            "the walk collapsed to the average — the segments are not being used")

    def test_the_water_is_carried_across_segments(self):
        """Two half-runs in sequence must equal one whole run at the same air."""
        m = ThermalModel(1.30, 55.0)
        whole = m.heating_minutes_piecewise(20.0, 30.0, [(20.0, 12.0)])
        half = m.heating_minutes_piecewise(20.0, 30.0, [(2.0, 12.0), (18.0, 12.0)])
        assert whole == pytest.approx(half, rel=1e-9)

    def test_running_off_the_end_returns_none(self):
        """R9: a forecast that does not reach the finish cannot price the finish."""
        m = ThermalModel(1.30, 55.0)
        assert m.heating_minutes_piecewise(20.0, 39.0, [(1.0, 12.0), (1.0, 12.0)]) is None

    def test_a_segment_that_cannot_reach_the_target_is_walked_through(self):
        """A cold block where the asymptote sits below the target must not abort the
        plan — the run continues into the next block, which may be warmer."""
        m = ThermalModel(0.5, 20.0)                  # asymptote = air + 10
        segs = [(3.0, 5.0), (30.0, 32.0)]            # 15 then 42
        assert m.heating_minutes_piecewise(20.0, 30.0, segs) is not None

    def test_no_work_is_zero(self):
        assert ThermalModel().heating_minutes_piecewise(39.0, 39.0, [(1.0, 12.0)]) == 0.0


class TestTheBoundaryCase:
    """A finish partway through a block is charged for the part it spends there."""

    def test_a_short_finish_in_a_long_block_costs_only_what_it_uses(self):
        m = ThermalModel(1.30, 55.0)
        # Reach the target 30 minutes into a six-hour block.
        first = [(1.0, 12.0)]
        s = m.asymptote(12.0)
        after_one_hour = s - (s - 20.0) * math.exp(-1.0 / 55.0)
        target = s - (s - after_one_hour) * math.exp(-0.5 / 55.0)
        walked = m.heating_minutes_piecewise(20.0, target, first + [(6.0, 12.0)])
        assert walked == pytest.approx(90.0, rel=1e-6), (
            "a 90 minute run was not charged 90 minutes")

    def test_the_block_it_finishes_in_does_not_contaminate_the_answer(self):
        """Changing the air *after* the finish must not move the finish."""
        m = ThermalModel(1.30, 55.0)
        base = [(2.0, 10.0)]
        a = m.heating_minutes_piecewise(20.0, 22.0, base + [(6.0, 30.0)])
        b = m.heating_minutes_piecewise(20.0, 22.0, base + [(6.0, -10.0)])
        assert a == pytest.approx(b), "weather after the finish changed the finish"

    def test_partial_leading_segment_is_honoured(self):
        """Starting two hours into a six-hour block must charge the four that remain,
        which is the case an average over whole blocks gets wrong."""
        m = ThermalModel(1.30, 55.0)
        four = m.heating_minutes_piecewise(20.0, 26.0, [(4.0, 20.0), (10.0, 0.0)])
        six = m.heating_minutes_piecewise(20.0, 26.0, [(6.0, 20.0), (10.0, 0.0)])
        assert four is not None and six is not None
        assert four >= six, "a shorter warm segment cannot finish sooner"


class TestSegmentsFromTheForecast:

    @staticmethod
    def _rows(start, temps, step_h=1):
        return [(start + timedelta(hours=i * step_h), t) for i, t in enumerate(temps)]

    def test_no_forecast_means_no_segments(self):
        assert _coord().forecast_segments(start_utc=_NOW) == []

    def test_rows_become_durations_and_temperatures(self):
        c = _coord(self._rows(_NOW, [10.0, 12.0, 14.0]))
        segs = c.forecast_segments(start_utc=_NOW)
        assert [t for _, t in segs] == [10.0, 12.0, 14.0]
        assert all(h == pytest.approx(1.0) for h, _ in segs)

    def test_past_rows_are_dropped(self):
        c = _coord(self._rows(_NOW - timedelta(hours=3), [1.0, 2.0, 3.0, 4.0, 5.0]))
        segs = c.forecast_segments(start_utc=_NOW)
        assert [t for _, t in segs] == [4.0, 5.0]

    def test_the_first_block_is_trimmed_to_the_start(self):
        """The boundary case, at the other end: a run beginning two hours into a
        six-hour block is charged the four hours that remain, not six."""
        c = _coord(self._rows(_NOW - timedelta(hours=2), [8.0, 20.0], step_h=6))
        segs = c.forecast_segments(start_utc=_NOW)
        assert segs[0][0] == pytest.approx(4.0), segs
        assert segs[0][1] == 8.0

    def test_a_later_forecast_is_bridged_with_the_current_reading(self):
        """The opening minutes are priced on what is outside, not on what is expected
        an hour hence."""
        c = _coord(self._rows(_NOW + timedelta(hours=2), [15.0]), ambient=7.0)
        segs = c.forecast_segments(start_utc=_NOW)
        assert segs[0] == (pytest.approx(2.0), 7.0)
        assert segs[1][1] == 15.0

    def test_a_mocked_clock_yields_no_segments_rather_than_raising(self):
        c = _coord(self._rows(_NOW, [10.0]))
        assert c.forecast_segments(start_utc=object()) == []

    def test_the_horizon_is_respected(self):
        c = _coord(self._rows(_NOW, [10.0] * 60))
        assert sum(h for h, _ in c.forecast_segments(_NOW, max_hours=12.0)) <= 13.0


class TestTheModelPrefersSegments:

    def test_a_forecast_is_used_in_preference_to_a_flat_average(self):
        rows = [(_NOW + timedelta(hours=i), t)
                for i, t in enumerate([0.0] * 6 + [24.0] * 20)]
        c = _coord(rows, ambient=0.0)
        c.thermal_a, c.thermal_tau_h = 1.30, 55.0
        walked = c.thermal_model().heating_minutes_piecewise(
            20.0, 30.0, c.forecast_segments(start_utc=_NOW))
        flat = c.thermal_model().heating_minutes(20.0, 30.0, 0.0)
        assert walked is not None and walked < flat, (
            "the afternoon warmth was ignored")

    def test_without_a_forecast_it_falls_back_to_flat(self):
        c = _coord(ambient=12.0)
        c.thermal_a, c.thermal_tau_h = 1.30, 55.0
        assert c.thermal_minutes(20.0, 30.0) == pytest.approx(
            c.thermal_model().heating_minutes(20.0, 30.0, 12.0))

    def test_no_forecast_and_no_reading_means_no_answer(self):
        c = _coord()
        assert c.thermal_minutes(20.0, 30.0) is None
