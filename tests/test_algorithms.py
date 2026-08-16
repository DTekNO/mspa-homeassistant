"""Unit tests for MSpa pure algorithm functions.

Covers:
  ambient_rate_factor  — ambient temperature correction for heating rate buckets
  _anchor_eta_utc      — anchor-based ETA computation
  prediction bias      — incremental EMA, monotonicity, clamping, history replay

No Home Assistant runtime required; conftest.py stubs the HA package tree.
Run with: python -m pytest tests/test_algorithms.py -v
"""
import pytest
from datetime import datetime, timezone, timedelta

from custom_components.mspa.const import (
    ambient_rate_factor,
    AMBIENT_FACTOR_MIN,
    AMBIENT_FACTOR_MAX,
    BIAS_CLAMP_MIN,
    BIAS_CLAMP_MAX,
)
from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.predictor import extrapolate_within_band
from custom_components.mspa.sensor import _anchor_eta_utc, _segmented_heating_minutes


# ── Minimal coordinator stub for anchor tests ─────────────────────────────────

class _Coord:
    # No session in flight, so the ETA falls through to the live rates — which is
    # what these anchor tests were written against.  Borrowed from the real
    # coordinator rather than restated.
    _prediction = None
    session_plan = MSpaUpdateCoordinator.session_plan
    session_settled = MSpaUpdateCoordinator.session_settled
    session_opening_eta = MSpaUpdateCoordinator.session_opening_eta
    shadow_eta = MSpaUpdateCoordinator.shadow_eta

    def __init__(
        self,
        *,
        water_temp: float = 35.0,
        target_temp: float = 40.0,
        heat_rate: "float | None" = 2.0,
        device_heat_perhour: int = 0,
        anchor_offset_minutes: float = -30.0,
    ):
        self.computed_heat_rate = heat_rate
        self.computed_cool_rate = None
        self.prediction_bias = 1.0
        self._session_scalar = 1.0
        self._session_fresh_buckets = {0, 1, 2}
        self.ambient_temp = None
        self.ambient_baseline = None
        self.heat_rate_buckets = [heat_rate, heat_rate, heat_rate] if heat_rate else [None, None, None]
        self.heating_since = None
        self.temp_anchor_time = datetime.now(timezone.utc) + timedelta(minutes=anchor_offset_minutes)
        self.temp_anchor_temp = water_temp
        self.temp_anchor_target = target_temp
        self._last_data = {
            "water_temperature": str(water_temp),
            "target_temperature": str(target_temp),
            "device_heat_perhour": device_heat_perhour,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ambient_rate_factor
# ═══════════════════════════════════════════════════════════════════════════════

class TestAmbientRateFactor:

    def test_no_data_returns_one(self):
        """Without weather data factor is always 1.0 (no correction)."""
        assert ambient_rate_factor(2, None, None) == 1.0
        assert ambient_rate_factor(2, None, 15.0) == 1.0
        assert ambient_rate_factor(2, 5.0, None) == 1.0

    def test_at_baseline_returns_one(self):
        """When ambient equals baseline the factor is exactly 1.0."""
        assert ambient_rate_factor(2, 15.0, 15.0) == pytest.approx(1.0)

    def test_cold_bucket_zero_sensitivity(self):
        """Cold bucket (idx 0) has zero sensitivity — factor always 1.0."""
        assert ambient_rate_factor(0, 0.0, 15.0) == pytest.approx(1.0)
        assert ambient_rate_factor(0, 30.0, 15.0) == pytest.approx(1.0)

    def test_hot_bucket_cold_night_slows(self):
        """Hot bucket (idx 2) slows on cold night: sensitivity=0.06/°C.
        Baseline 15°C, outdoor 5°C → delta=−10 → factor=1+0.06*(−10)=0.4."""
        assert ambient_rate_factor(2, 5.0, 15.0) == pytest.approx(0.4)

    def test_hot_bucket_warm_night_clamped(self):
        """Hot bucket speeds on warm night: 25°C vs baseline 15°C → factor=1.6 → clamped to MAX."""
        assert ambient_rate_factor(2, 25.0, 15.0) == pytest.approx(AMBIENT_FACTOR_MAX)

    def test_extreme_cold_clamped_at_min(self):
        """Extreme cold is clamped at AMBIENT_FACTOR_MIN."""
        assert ambient_rate_factor(2, -85.0, 15.0) == pytest.approx(AMBIENT_FACTOR_MIN)

    def test_mid_bucket_moderate_sensitivity(self):
        """Mid bucket (idx 1) has 0.02/°C sensitivity.
        Baseline 15°C, outdoor 5°C → factor=1+0.02*(−10)=0.8."""
        assert ambient_rate_factor(1, 5.0, 15.0) == pytest.approx(0.8)

    def test_invalid_bucket_returns_one(self):
        assert ambient_rate_factor(-1, 5.0, 15.0) == 1.0
        assert ambient_rate_factor(3, 5.0, 15.0) == 1.0

    def test_ambient_factor_applied_in_heat_bucket_rate(self):
        """_heat_bucket_rate applies ambient factor when no fresh session data."""
        from custom_components.mspa.sensor import _heat_bucket_rate
        c = _Coord(heat_rate=None)
        c.heat_rate_buckets = [None, None, 1.0]     # only hot bucket
        c._session_fresh_buckets = set()             # no fresh data this session
        c._session_scalar = 1.0
        c.ambient_temp = 5.0        # cold night
        c.ambient_baseline = 15.0   # 10°C below baseline
        # hot bucket rate = 1.0 * (1 + 0.06 * −10) = 0.4
        assert _heat_bucket_rate(c, 38.0) == pytest.approx(0.4)

    def test_fresh_session_data_bypasses_ambient(self):
        """Fresh session observations are used verbatim — ambient model not applied."""
        from custom_components.mspa.sensor import _heat_bucket_rate
        c = _Coord(heat_rate=None)
        c.heat_rate_buckets = [None, None, 1.0]
        c._session_fresh_buckets = {2}   # hot bucket observed this session
        c._session_scalar = 1.0
        c.ambient_temp = 5.0
        c.ambient_baseline = 15.0
        assert _heat_bucket_rate(c, 38.0) == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# _anchor_eta_utc
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnchorEtaUtc:

    def test_returns_none_when_no_anchor(self):
        c = _Coord()
        c.temp_anchor_time = None
        assert _anchor_eta_utc(c, 40.0, datetime.now(timezone.utc)) is None

    def test_returns_none_when_no_rate(self):
        c = _Coord(heat_rate=None, device_heat_perhour=0)
        assert _anchor_eta_utc(c, 40.0, datetime.now(timezone.utc)) is None

    def test_returns_future_datetime_when_heating(self):
        """5°C remaining at 2°C/h = 150 min total; anchor 30 min ago → ~120 min ahead."""
        c = _Coord(water_temp=35.0, target_temp=40.0, heat_rate=2.0, anchor_offset_minutes=-30.0)
        now_utc = datetime.now(timezone.utc)
        result = _anchor_eta_utc(c, 40.0, now_utc)
        assert result is not None
        diff_minutes = (result - now_utc).total_seconds() / 60
        assert 100 < diff_minutes < 140, f"Expected ~120 min ahead, got {diff_minutes:.1f}"

    def test_stable_anchor_does_not_drift(self):
        """Two calls with the same now_utc return identical timestamps."""
        c = _Coord(water_temp=35.0, target_temp=40.0, heat_rate=2.0, anchor_offset_minutes=-30.0)
        now_utc = datetime.now(timezone.utc)
        r1 = _anchor_eta_utc(c, 40.0, now_utc)
        r2 = _anchor_eta_utc(c, 40.0, now_utc)
        assert r1 == r2


# ═══════════════════════════════════════════════════════════════════════════════
# Prediction bias — incremental EMA, monotone with respect to evidence
# ═══════════════════════════════════════════════════════════════════════════════

def _bias_coord(bias: float = 1.0, history: "list | None" = None) -> MSpaUpdateCoordinator:
    """Coordinator instance via object.__new__ for bias-only tests."""
    c = object.__new__(MSpaUpdateCoordinator)
    c.prediction_bias = bias
    c._prediction_history = history if history is not None else []
    return c


def _session(start: float, target: float, est: float, actual: float) -> dict:
    return {"start_temp": start, "target_temp": target,
            "estimated_minutes": est, "actual_minutes": actual}


class TestBiasRatio:
    """_bias_ratio decides which sessions are admissible evidence."""

    def test_ratio_uses_raw_estimate(self):
        # The bias must converge on the rate model's error, not on its own output,
        # so the ratio is taken against estimated_minutes (raw), never the biased one.
        r = MSpaUpdateCoordinator._bias_ratio(
            {**_session(34.0, 39.5, 341.6, 329.4), "estimated_minutes_biased": 362.2}
        )
        assert r == pytest.approx(329.4 / 341.6)

    def test_rejects_short_run(self):
        assert MSpaUpdateCoordinator._bias_ratio(_session(38.0, 39.5, 60, 62)) is None

    def test_rejects_outlier_ratio(self):
        assert MSpaUpdateCoordinator._bias_ratio(_session(30.0, 40.0, 100, 500)) is None

    def test_rejects_missing_values(self):
        assert MSpaUpdateCoordinator._bias_ratio(_session(30.0, 40.0, 0, 300)) is None
        assert MSpaUpdateCoordinator._bias_ratio({}) is None


class TestBiasMonotonicity:
    """The regression that motivated the rewrite: the bias moved the wrong way.

    Production data showed bias 1.055 → 1.060 after a session whose ratio was
    1.0157, then → 1.063 after a ratio of 0.9643.  Both samples were BELOW the
    bias in force, so both should have pulled it down.
    """

    def test_below_current_bias_always_lowers_it(self):
        c = _bias_coord(bias=1.055)
        c._apply_bias_sample(1.0157)
        assert c.prediction_bias < 1.055

    def test_above_current_bias_always_raises_it(self):
        c = _bias_coord(bias=1.0)
        c._apply_bias_sample(1.08)
        assert c.prediction_bias > 1.0

    def test_production_sequence_now_converges_downward(self):
        c = _bias_coord(bias=1.055)
        c._apply_bias_sample(304.7 / 300.0)    # 29 Jul, ratio 1.0157
        after_first = c.prediction_bias
        c._apply_bias_sample(329.4 / 341.6)    # 30 Jul, ratio 0.9643
        assert after_first < 1.055
        assert c.prediction_bias < after_first
        assert c.prediction_bias < 1.03        # heading toward the ~0.99 the data implies

    def test_repeated_neutral_samples_converge_to_one(self):
        c = _bias_coord(bias=1.10)
        for _ in range(25):
            c._apply_bias_sample(1.0)
        assert c.prediction_bias == pytest.approx(1.0, abs=1e-3)

    def test_clamped_to_configured_range(self):
        hi = _bias_coord(bias=1.1)
        for _ in range(50):
            hi._apply_bias_sample(3.0)
        assert hi.prediction_bias == pytest.approx(BIAS_CLAMP_MAX)

        lo = _bias_coord(bias=0.9)
        for _ in range(50):
            lo._apply_bias_sample(0.3)
        assert lo.prediction_bias == pytest.approx(BIAS_CLAMP_MIN)


class TestBiasSeedFromHistory:
    """Upgrade path: replay persisted history through the new EMA."""

    def test_empty_history_gives_neutral_bias(self):
        c = _bias_coord(bias=1.06, history=[])
        c._seed_prediction_bias_from_history()
        assert c.prediction_bias == 1.0

    def test_replays_chronologically_and_weights_recent_higher(self):
        # Old sessions ran slow (ratio 1.10), recent ones ran to plan (1.00).
        # The result must sit nearer the recent evidence than the old.
        history = [_session(30.0, 40.0, 100, 110) for _ in range(5)]
        history += [_session(30.0, 40.0, 100, 100) for _ in range(3)]
        c = _bias_coord(bias=99.0, history=history)
        c._seed_prediction_bias_from_history()
        assert 1.0 <= c.prediction_bias < 1.04

    def test_skips_inadmissible_records(self):
        history = [_session(38.0, 39.5, 60, 90),      # too short a run
                   _session(30.0, 40.0, 100, 500)]    # outlier ratio
        c = _bias_coord(bias=1.06, history=history)
        c._seed_prediction_bias_from_history()
        assert c.prediction_bias == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# READY AT AND THE SCHEDULER MUST AGREE AT A SESSION START
# ═══════════════════════════════════════════════════════════════════════════════

class TestSameStartingPoint:
    """Observed 2026-08-10: the scheduler started at 16:14 predicting a 01:30 finish
    while Ready at immediately said 01:10 — ~20 min apart on the same spa.

    The maths was shared; the *starting point* was not. Heat Schedule works back from
    `now + heating_minutes(current → target)`, while Ready at measured from
    `temp_anchor_time`, which records when the reading last *changed*. During a
    cool-down that is a cooling transition, so elapsed cooling minutes were counted
    as heating progress.
    """

    _NOW = datetime(2026, 8, 10, 16, 14, tzinfo=timezone.utc)

    def _coord(self, *, anchor_age_min, heating_since_min=0.0):
        c = _Coord(water_temp=29.5, target_temp=39.5, heat_rate=1.0)
        c.temp_anchor_time = self._NOW - timedelta(minutes=anchor_age_min)
        c.temp_anchor_temp = 29.5
        c.temp_anchor_target = 39.5
        c.heating_since = self._NOW - timedelta(minutes=heating_since_min)
        return c

    def _scheduler_finish(self, c):
        """What Heat Schedule implies: now + the heating time still to do."""
        mins = _segmented_heating_minutes(29.5, 39.5, c)
        return self._NOW + timedelta(minutes=mins)

    def test_they_agree_when_heating_has_just_started(self):
        """The anchor is 40 min old from cooling; heating began now."""
        c = self._coord(anchor_age_min=40.0, heating_since_min=0.0)
        eta = _anchor_eta_utc(c, 39.5, self._NOW)
        drift = abs((eta - self._scheduler_finish(c)).total_seconds()) / 60
        assert drift < 1.0, f"{drift:.0f} min apart at the moment heating began"

    def test_a_stale_cooling_anchor_no_longer_pulls_the_eta_earlier(self):
        c = self._coord(anchor_age_min=40.0, heating_since_min=0.0)
        eta = _anchor_eta_utc(c, 39.5, self._NOW)
        assert eta > self._NOW, "ETA landed in the past"
        # 10 °C at 1 °C/h is ~10 h; a 40-minute cooling anchor must not shave that
        assert (eta - self._NOW).total_seconds() / 3600 > 9.0

    def test_the_anchor_takes_over_once_it_is_the_newer_of_the_two(self):
        """Its purpose is an ETA stable between readings, and that must survive."""
        c = self._coord(anchor_age_min=5.0, heating_since_min=60.0)
        eta_now = _anchor_eta_utc(c, 39.5, self._NOW)
        eta_later = _anchor_eta_utc(c, 39.5, self._NOW + timedelta(minutes=2))
        assert eta_now == eta_later, "ETA drifted with the clock between readings"


class TestTempAnchorOnlyMovesOnRealChange:
    """The anchor must move at a crossing and stay put in between.

    Regression for a bug shipped 2026-08-11 and caught 2026-08-12 by noticing that the
    Heat Schedule sensor and the coordinator logged start times 6 min apart 30 s apart.
    Change detection compared the raw reading against `temp_anchor_temp`, which
    band-centre anchoring had made a *midpoint* — so it never matched, the anchor
    re-fired every poll, and `temp_anchor_time` reset each time.

    Three things broke together, silently, and none was visible from outside:
      * the half-step correction decayed geometrically back to the raw reading
      * elapsed-time measurement never accumulated, so extrapolation started late
      * the clamp was measured from a moving anchor, so the estimate could leave the
        reading's band — the one guard that was explicitly asked for
    """

    def _coord(self):
        c = MSpaUpdateCoordinator.__new__(MSpaUpdateCoordinator)
        c.temp_anchor_time = None
        c.temp_anchor_temp = None
        c.temp_anchor_target = None
        c.temp_anchor_rising = None
        c._anchor_prev_reading = None
        return c

    def test_steady_reading_does_not_re_anchor(self):
        c = self._coord()
        c._update_temp_anchor(32.5, 39.5)          # first sighting
        c._update_temp_anchor(32.0, 39.5)          # a crossing
        anchored_at, anchored_temp = c.temp_anchor_time, c.temp_anchor_temp
        for _ in range(20):                        # ten minutes of 30 s polls
            c._update_temp_anchor(32.0, 39.5)
        assert c.temp_anchor_time is anchored_at, "anchor time reset without a change"
        assert c.temp_anchor_temp == anchored_temp, "anchor drifted without a change"

    def test_crossing_anchors_at_the_midpoint_of_the_two_readings(self):
        c = self._coord()
        c._update_temp_anchor(32.5, 39.5)
        c._update_temp_anchor(32.0, 39.5)
        assert c.temp_anchor_temp == pytest.approx(32.25)

    def test_midpoint_does_not_decay_over_repeated_polls(self):
        """The half-step correction is the whole point; it must not bleed away."""
        c = self._coord()
        c._update_temp_anchor(32.5, 39.5)
        c._update_temp_anchor(32.0, 39.5)
        for _ in range(50):
            c._update_temp_anchor(32.0, 39.5)
        assert c.temp_anchor_temp == pytest.approx(32.25)

    def test_direction_follows_the_crossing(self):
        c = self._coord()
        c._update_temp_anchor(32.5, 39.5)
        c._update_temp_anchor(32.0, 39.5)
        assert c.temp_anchor_rising is False
        c._update_temp_anchor(32.5, 39.5)
        assert c.temp_anchor_rising is True

    def test_first_sighting_has_no_direction(self):
        """Nothing to compare against, so the extrapolation must decline it."""
        c = self._coord()
        c._update_temp_anchor(32.0, 39.5)
        assert c.temp_anchor_rising is None
        assert c.temp_anchor_temp == pytest.approx(32.0)

    def test_target_change_re_anchors(self):
        c = self._coord()
        c._update_temp_anchor(32.0, 39.5)
        first = c.temp_anchor_time
        c._update_temp_anchor(32.0, 38.0)
        assert c.temp_anchor_time is not first
        assert c.temp_anchor_target == 38.0

    def test_a_jump_of_more_than_one_band_is_not_midpointed(self):
        """Two bands at once means a reading was missed, so the threshold is unknown."""
        c = self._coord()
        c._update_temp_anchor(32.5, 39.5)
        c._update_temp_anchor(31.0, 39.5)
        assert c.temp_anchor_temp == pytest.approx(31.0)
        assert c.temp_anchor_rising is None

    def test_estimate_stays_inside_the_readings_own_band(self):
        """With the midpoint correct, the clamp bounds the estimate to +/-0.25 of the
        reading — which is the guard that was asked for, and which the bug defeated."""
        c = self._coord()
        c._update_temp_anchor(32.5, 39.5)
        c._update_temp_anchor(32.0, 39.5)
        reading = 32.0
        for hours in (0.0, 0.5, 1.0, 2.0, 100.0):
            est = extrapolate_within_band(
                c.temp_anchor_temp, hours, 0.36, cooling=not c.temp_anchor_rising)
            assert reading - 0.25 - 1e-9 <= est <= reading + 0.25 + 1e-9, (
                f"estimate {est} left the band of reading {reading} after {hours} h")


class TestShadowPlan:
    """The private rate curve that owns the displayed ready time during a session.

    Replayed over the four recorded heat-ups it is within 30 minutes at the halfway
    mark, 14 by three-quarters and 2 at the end — and it reaches those same figures
    when the stored rates it started from are offset 30% either way, which is the
    property the design exists for. See analysis/shadow_recalibrate.py.
    """

    _T0 = datetime(2026, 8, 14, 4, 0, tzinfo=timezone.utc)

    def _plan(self, start=33.0, target=39.5, opening_min=426):
        from custom_components.mspa.predictor import ShadowPlan
        return ShadowPlan((1.10, 0.99, 0.79), start, target,
                          self._T0 + timedelta(minutes=opening_min))

    def _feed(self, plan, steps):
        """steps: (minutes since start, temperature)."""
        for mins, temp in steps:
            plan.crossing(temp, self._T0 + timedelta(minutes=mins))
        return plan

    def test_bands_are_seeded_from_the_stored_buckets(self):
        p = self._plan()
        assert p.rates == [1.10, 0.99, 0.79]

    def test_the_opening_estimate_stands_until_something_qualifies(self):
        p = self._feed(self._plan(), [(30, 33.5), (60, 34.0), (90, 34.5)])
        assert p.eta == self._T0 + timedelta(minutes=426)
        assert p.revisions == 0

    def test_nothing_is_measured_until_the_warm_up_is_over(self):
        """The opening crossings time where the water sat in its band and the heater
        coming up, not steady heating — 2026-08-12 implied 7.9 °C/h over its first."""
        p = self._plan(start=37.0, target=39.5, opening_min=190)
        # 0.5 °C in 2 minutes is 15 °C/h; nothing here may reach the curve.
        self._feed(p, [(2, 37.5), (30, 38.0), (60, 38.5)])
        assert p.eta == self._T0 + timedelta(minutes=190)
        assert p.revisions == 0

    def test_a_top_up_shorter_than_the_warm_up_still_gets_its_last_look(self):
        """38.0 → 39.5 never climbs the 2 °C that starts a measurement, so the final
        half-degree is the only correction it will ever get. It has to stay reachable."""
        p = self._plan(start=38.0, target=39.5, opening_min=120)
        self._feed(p, [(40, 38.5)])
        assert p.revisions == 0
        self._feed(p, [(80, 39.0)])
        assert p.revisions == 1

    def test_the_settle_shrinks_as_the_target_nears(self):
        p = self._plan()
        assert p.settle_for(22.0) == pytest.approx(17.5 / 3.0)   # a long cold-start run
        assert p.settle_for(35.0) == pytest.approx(1.5)
        assert p.settle_for(39.0) == pytest.approx(1.0)          # floored, not 0.17

    def test_it_recalibrates_once_it_has_measured_its_share(self):
        """37.0 → 38.0 measures 1.0 °C with 2.5 °C left, so the settle is the floor."""
        p = self._plan()
        self._feed(p, [(200, 37.0), (260, 38.0)])
        assert p.revisions == 1
        # 1.0 °C in 60 min is 1.0 °C/h against a shadow of 0.79, so the curve speeds up
        assert p.rates[2] > 0.79

    def test_it_holds_while_the_measurement_is_short_for_what_remains(self):
        """Anchored at 35.0 with 4.5 °C to go, one degree is not yet a third of it."""
        p = self._plan()
        self._feed(p, [(0, 33.5), (60, 34.0), (120, 35.0), (180, 36.0)])
        assert p.revisions == 0
        self._feed(p, [(240, 36.5)])                # 1.5 °C measured — now it qualifies
        assert p.revisions == 1

    def test_a_band_recalibrates_only_once(self):
        """The anchor stays put, so without this the span keeps growing past the settle
        and every later crossing in the band would revise the curve again."""
        p = self._plan(start=30.0)
        self._feed(p, [(30, 30.5), (60, 31.5), (90, 32.0)])     # warm-up, then anchored
        self._feed(p, [(150, 33.0), (210, 34.5)])               # 2.5 °C measured — fires
        assert p.revisions == 1
        self._feed(p, [(270, 35.0), (330, 35.5), (390, 36.0)])  # same band, must not
        assert p.revisions == 1

    def test_a_boundary_re_anchors_without_a_warm_up(self):
        """A band edge is an exact position, so it needs no allowance — unlike a start,
        where the water sits somewhere unknown inside its 0.5 °C band."""
        p = self._plan()
        self._feed(p, [(0, 33.5), (60, 35.0), (120, 36.5)])     # recalibrates in 30-37
        before = p.revisions
        self._feed(p, [(180, 37.0), (240, 38.0)])               # 37.0 anchors at once
        assert p.revisions == before + 1

    def test_a_faster_spa_pulls_the_estimate_earlier(self):
        p = self._plan()
        self._feed(p, [(200, 37.0), (230, 38.0)])   # 1.0 °C in 30 min = 2.0 °C/h
        assert p.eta < self._T0 + timedelta(minutes=426)

    def test_drift_is_capped_relative_to_where_it_started(self):
        p = self._plan()
        self._feed(p, [(200, 37.0), (205, 38.0)])   # 12 °C/h, physically impossible
        assert max(p.rates) <= 1.10 * 2.0 + 1e-9

    def test_the_final_half_degree_gets_a_look(self):
        p = self._plan()
        self._feed(p, [(200, 37.0), (260, 38.0)])
        before = p.revisions
        self._feed(p, [(320, 39.0)])
        assert p.revisions == before + 1

    def test_a_zero_rate_never_produces_an_estimate(self):
        from custom_components.mspa.predictor import ShadowPlan
        p = ShadowPlan((1.10, 0.0, 0.79), 33.0, 39.5, self._T0)
        assert p.minutes(33.0, 39.5) is None
