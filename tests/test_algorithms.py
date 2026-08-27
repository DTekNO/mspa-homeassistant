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
    prediction_model = MSpaUpdateCoordinator.prediction_model
    uses_frozen_plan = MSpaUpdateCoordinator.uses_frozen_plan
    heating_minutes = MSpaUpdateCoordinator.heating_minutes
    _predictor = MSpaUpdateCoordinator._predictor

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


class TestBucketsLearnOverABoundedSpan:
    """A bucket rate is the chord between two edges, so both edges have to exist.

    The hot bucket was open-ended, which let it absorb the 39-40 tail — where a session
    with a 40 °C setpoint spends its slowest hour — and the chord it settled on suited
    neither half of what it covered. Bounded at 39, the rate above is extrapolated
    instead, and the recordings say that costs nothing: across five sessions the final
    half degree runs at 1.05x the degree below it, flat within the noise of a 0.5 °C
    span, and any error there is on the forgiving side.
    """

    def test_a_span_inside_the_range_is_learned(self):
        from custom_components.mspa.predictor import in_learning_range
        assert in_learning_range(37.0, 39.0) is True
        assert in_learning_range(30.0, 37.0) is True
        assert in_learning_range(22.0, 30.0) is True

    def test_the_tail_above_the_hot_bucket_is_not_learned(self):
        """The half degree from 39.0 to 39.5, and anything a 40 °C setpoint adds."""
        from custom_components.mspa.predictor import in_learning_range
        assert in_learning_range(37.0, 39.5) is False
        assert in_learning_range(39.0, 40.0) is False

    def test_below_the_cold_bucket_is_not_learned(self):
        from custom_components.mspa.predictor import in_learning_range
        assert in_learning_range(18.0, 25.0) is False

    def test_the_edges_themselves_count_as_inside(self):
        from custom_components.mspa.predictor import in_learning_range
        assert in_learning_range(20.0, 39.0) is True

    def test_unknowns_do_not_move_a_stored_bucket(self):
        """Other sessions depend on these, so a sample that cannot be placed is dropped."""
        from custom_components.mspa.predictor import in_learning_range
        assert in_learning_range(None, 38.0) is False
        assert in_learning_range(37.0, None) is False
        assert in_learning_range("warm", 38.0) is False


class TestShadowPlan:
    """The private rate curve that owns the displayed ready time during a session.

    Replayed over the four recorded heat-ups it is within 30 minutes at the halfway
    mark, 14 by three-quarters and 2 at the end — and it reaches those same figures
    when the stored rates it started from are offset 30% either way, which is the
    property the design exists for. See analysis/shadow_recalibrate.py.
    """

    _T0 = datetime(2026, 8, 14, 4, 0, tzinfo=timezone.utc)

    def _plan(self, start=33.0, target=39.5, opening_min=426, start_time=True):
        """A plan with a real start_time, so the rescaling path is exercised.

        It was built without one, which left _band_entry with no clock — so every
        rescaling returned before it did anything and this whole class was blind to that
        half of crossing(). Pass start_time=False for the case a caller really does omit
        it, which must still re-anchor.
        """
        from custom_components.mspa.predictor import ShadowPlan
        return ShadowPlan((1.10, 0.99, 0.79), start, target,
                          self._T0 + timedelta(minutes=opening_min),
                          start_time=self._T0 if start_time else None)

    def _feed(self, plan, steps):
        """steps: (minutes since start, temperature)."""
        for mins, temp in steps:
            plan.crossing(temp, self._T0 + timedelta(minutes=mins))
        return plan

    def test_bands_are_seeded_from_the_stored_buckets(self):
        """Four bands, three stored buckets: each band takes the bucket covering its
        own midpoint, so the sub-20 band and the 20-30 band both draw the cold rate.
        Nothing below HEAT_BUCKET_LEARN_MIN was ever measured — the rate down there is
        the cold chord extrapolated, and the band exists to mark where that ends and to
        let the plan re-anchor at 20 rather than carry the extrapolation to 30."""
        p = self._plan()
        assert p.rates == [1.10, 1.10, 0.99, 0.79]

    def test_the_opening_estimate_stands_until_something_qualifies(self):
        p = self._feed(self._plan(), [(30, 33.5), (60, 34.0), (90, 34.5)])
        assert p.eta == self._T0 + timedelta(minutes=426)
        assert p.revisions == 0

    def test_a_session_inside_one_band_holds_its_opening_estimate(self):
        """37.0 → 38.5 crosses no edge and never reaches the final half degree, so there
        is nothing it is entitled to revise against."""
        p = self._plan(start=37.0, target=39.5, opening_min=190)
        # 0.5 °C in 2 minutes is 15 °C/h; nothing here may reach the curve.
        self._feed(p, [(2, 37.5), (30, 38.0), (60, 38.5)])
        assert p.eta == self._T0 + timedelta(minutes=190)
        assert p.revisions == 0

    def test_a_top_up_that_crosses_no_edge_still_gets_its_last_look(self):
        """38.0 → 39.5 is entirely inside the hot band, so the final half degree is the
        only correction it will ever get. It has to stay reachable."""
        p = self._plan(start=38.0, target=39.5, opening_min=120)
        self._feed(p, [(40, 38.5)])
        assert p.revisions == 0
        self._feed(p, [(80, 39.0)])
        assert p.revisions == 1

    def test_a_band_edge_revises(self):
        """30.0 and 37.0 are the only temperatures at which a band has been traversed
        end to end, so they are the only ones that may move the estimate."""
        p = self._plan()
        self._feed(p, [(200, 37.0)])
        assert p.revisions == 1
        assert p.eta == self._T0 + timedelta(minutes=200 + p.minutes(37.0, 39.5))

    def test_nothing_between_the_edges_revises(self):
        """The old design measured a sub-span here and compared it against the band's
        chord. Inside 30-37 the real rate runs from 1.33 to 1.05 °C/h, so that comparison
        finds a difference that is arithmetically real and physically meaningless — on
        2026-08-20 it turned an 8-minute error into a 58-minute one."""
        p = self._plan(start=30.0, opening_min=560)
        self._feed(p, [(30, 30.5), (90, 31.5), (150, 32.5), (240, 34.0), (330, 35.5),
                       (400, 36.5)])
        assert p.revisions == 0
        assert p.eta == self._T0 + timedelta(minutes=560)

    def test_a_completed_band_rescales_the_curve(self):
        """A revision at an edge re-anchors *and* rescales, by how wrong the band just
        finished turned out to be: planned minutes over actual minutes, across the same
        temperature span.

        This class used to assert the opposite — that the rates never move — and gave the
        recorded evidence for it. That evidence was against a different measurement: a
        rate taken over a fixed span *into* the band just entered, compared against that
        band's chord. A degree at 0.5 °C reporting is worth about +/-50%, and a chord is
        exact only over a full traverse, so the comparison oscillated. Comparing elapsed
        against planned over one identical span is like for like, and measured over a
        whole band. Re-simulated over five sessions in analysis/shadow_recalibrate.py it
        takes the whole-session error from 62.5 to 43.4 minutes and the worst
        first-revision error from 192 to 57.

        Note this test could not have failed before: _plan built its ShadowPlan without a
        start_time, so there was no clock to measure a band against and every rescaling
        returned before doing anything.
        """
        p = self._plan(start=30.0, opening_min=560)
        before = list(p.rates)
        planned = p.minutes(30.0, 37.0)
        self._feed(p, [(120, 31.0), (300, 34.0), (520, 37.0)])
        assert p.revisions == 1
        # 520 minutes actually taken against whatever the curve had predicted.
        factor = planned / 520.0
        assert p.rates == pytest.approx([r * factor for r in before])

    def test_a_band_run_slower_than_planned_slows_the_curve(self):
        """The correction is signed, not a one-way optimism."""
        p = self._plan(start=30.0, opening_min=560)
        planned = p.minutes(30.0, 37.0)
        self._feed(p, [(int(planned * 2), 37.0)])
        assert p.rates[0] < 1.10, "a band that took twice as long must slow the curve"

    def test_a_band_too_short_to_trust_is_not_applied(self):
        """The amplification guard: a measurement is worth applying only where the
        journey ahead is comparable to the journey measured.

        A session entering just below a boundary measures one degree and would apply it
        to the nine remaining. In the recorded set that is session B, where an unguarded
        ratio of 2.48 needed a later 0.37 to undo it.
        """
        p = self._plan(start=29.0, opening_min=620)
        before = list(p.rates)
        self._feed(p, [(20, 30.0)])                  # one degree, nine and a half to go
        assert p.rates == before, "a one-degree band must not rescale the whole curve"
        assert p.revisions == 1, "but the re-anchor still happens"

    def test_without_a_start_time_it_still_re_anchors(self):
        """A plan built with no clock has nothing to measure its first band against, and
        must degrade to the old behaviour rather than fail."""
        p = self._plan(start=30.0, opening_min=560, start_time=False)
        before = list(p.rates)
        self._feed(p, [(520, 37.0)])
        assert p.rates == before
        assert p.revisions == 1

    def test_each_edge_revises_once(self):
        p = self._plan(start=29.0, opening_min=700)
        self._feed(p, [(60, 29.5), (120, 30.0)])
        assert p.revisions == 1
        self._feed(p, [(180, 30.5), (240, 31.0), (400, 34.0)])
        assert p.revisions == 1
        self._feed(p, [(560, 37.0)])
        assert p.revisions == 2

    def test_a_slow_first_band_pushes_the_estimate_out(self):
        """Re-anchoring carries the whole of the elapsed time, so a band that took
        longer than planned moves the finish out by exactly that much — without
        touching the rates for what is still to come."""
        p = self._plan(start=30.0, opening_min=560)
        planned_to_37 = p.minutes(30.0, 37.0)
        self._feed(p, [(planned_to_37 + 90, 37.0)])     # an hour and a half slow
        assert p.eta == self._T0 + timedelta(
            minutes=planned_to_37 + 90 + p.minutes(37.0, 39.5))

    def test_the_final_half_degree_uses_the_rate_just_below_it(self):
        """The one place a sub-span is trusted: it sits immediately below the span it
        predicts and the horizon is a single crossing. Across five recorded sessions the
        final half degree runs at 1.05x the degree below it — flat within the noise of a
        0.5 °C span."""
        p = self._plan(start=33.0, opening_min=426)
        self._feed(p, [(200, 37.0)])                     # edge: band entry is 37.0 here
        at_39 = 200 + 120                                # 2.0 °C in 120 min = 1.0 °C/h
        self._feed(p, [(at_39, 39.0)])
        # 0.5 °C left at the measured 1.0 °C/h is 30 minutes, not the curve's 0.79.
        assert p.eta == self._T0 + timedelta(minutes=at_39 + 30)

    def test_the_final_look_happens_once(self):
        p = self._plan(start=38.0, target=39.5, opening_min=120)
        self._feed(p, [(40, 38.5), (80, 39.0)])
        assert p.revisions == 1
        self._feed(p, [(100, 39.0), (110, 39.0)])
        assert p.revisions == 1

    def test_a_faster_spa_pulls_the_estimate_earlier(self):
        p = self._plan()
        self._feed(p, [(200, 37.0), (230, 38.0)])   # 1.0 °C in 30 min = 2.0 °C/h
        assert p.eta < self._T0 + timedelta(minutes=426)

    def test_an_impossible_reading_between_edges_changes_nothing(self):
        """Only a band edge rescales, so a wild reading part-way through a band cannot.

        The curve is still rescaled at the 37.0 edge by the band that genuinely completed
        — that is the point of the change — but the 12 °C/h step above it touches nothing.
        """
        p = self._plan()
        self._feed(p, [(200, 37.0)])
        settled = list(p.rates)
        self._feed(p, [(205, 38.0)])                # 12 °C/h, physically impossible
        assert p.rates == settled

    def test_the_final_look_also_records_what_it_measured(self):
        """The last band is the one nothing else corrects, and it used to be the one the
        curve never learned.

        The final look measures the rate immediately below the target and uses it for the
        remaining half degree, but it left `rates` describing the band it had just
        disproved — so a session ended with its curve wrong about the only stretch that
        carries most of the ambient sensitivity. It now writes the same evidence back.
        The estimate is unchanged: over a span inside one band, scaling by
        planned/actual and using the measured rate are the same number.
        """
        p = self._plan(start=30.0, opening_min=560)
        self._feed(p, [(520, 37.0)])
        before = list(p.rates)
        eta_before_final = p.eta
        self._feed(p, [(640, 39.0)])                 # 2.0 °C in 120 min = 1.0 °C/h
        assert p.eta != eta_before_final, "the final look must still revise"
        assert p.rates != before, "and must no longer leave the curve untouched"
        assert p.rates[-1] == pytest.approx(1.0, rel=1e-6), (
            "the top band should end at the rate the session actually achieved")

    def test_a_target_above_the_learned_range_measures_its_own_tail(self):
        """The top shadow band runs from 37 upward with no edge above it, while the
        stored bucket only learns to 39 — so a 40 °C target has half a degree of pure
        extrapolation in it. The final look lands at 39.5 and measures 37→39.5, which
        includes that slower tail, and the recorded rate should come out below one taken
        to 39.0 alone."""
        fast = self._plan(start=30.0, target=39.5, opening_min=560)
        self._feed(fast, [(520, 37.0), (640, 39.0)])          # 1.00 °C/h over 37→39
        slow = self._plan(start=30.0, target=40.0, opening_min=560)
        self._feed(slow, [(520, 37.0), (700, 39.5)])          # 0.83 °C/h over 37→39.5
        assert slow.rates[-1] < fast.rates[-1], (
            "a span carrying the tail past 39 must record a lower rate")

    def test_the_curve_cannot_drift_without_limit(self):
        """A cumulative clamp, not a per-step one, so a factor bringing the curve back
        toward its seed is never blocked. Inert on all five recorded sessions — the
        largest cumulative drift there is 1.32 — so this guards against data not yet
        seen."""
        from custom_components.mspa.predictor import (
            SHADOW_FACTOR_MAX, SHADOW_FACTOR_MIN)
        p = self._plan(start=30.0, opening_min=560)
        seed = list(p.rates)
        self._feed(p, [(1, 37.0)])                  # seven degrees in a minute
        drift = p.rates[0] / seed[0]
        assert drift <= SHADOW_FACTOR_MAX + 1e-9, f"drifted {drift:.2f}"
        assert drift >= SHADOW_FACTOR_MIN - 1e-9

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


class TestLearnedAmbientFactor:
    """The learned response to weather, and where it stops being trusted.

    The gate is extrapolation distance, not spread. A least-squares line passes through
    the mean of its data, so a fit built from a narrow band of ambients predicts well
    inside that band however badly determined its slope is — it degrades to "the rate
    this spa achieves at this temperature", which beats a bucket and a guessed
    sensitivity. The danger is only away from the evidence, where the slope's uncertainty
    is multiplied by the distance.

    Which is why it is used from the second traverse onward rather than waiting for a
    seasonal spread: where ambient moves slowly, today is nearly always a good forecast
    of tomorrow, and the fit is being asked about conditions it has seen.
    """

    def _fit(self, slope, intercept, n=100.0, mean=10.0, sd=3.0):
        return {"slope": slope, "intercept": intercept, "n": n,
                "ambient_mean": mean, "ambient_sd": sd}

    def _f(self, ambient, fit, seed=1.0, baseline=10.0):
        from custom_components.mspa.predictor import learned_ambient_factor
        return learned_ambient_factor(ambient, baseline, fit, seed)

    def test_no_fit_leaves_the_seed_alone(self):
        assert self._f(5.0, None, seed=0.9) == 0.9

    def test_inside_the_evidence_the_fit_is_used(self):
        # rate = 1.0 + 0.02 x ambient; at 13 against a baseline of 10 the fit says
        # 1.26/1.20 = 1.05, against a seed saying 1.00. The result should sit close to
        # the fit rather than equal it — the shrink term always keeps a little of the
        # seed, which is the safety net working, not a wrong answer.
        fit = self._fit(0.02, 1.0, n=1000.0)
        got = self._f(13.0, fit, seed=1.0)
        assert 1.0 < got <= 1.05
        assert abs(got - 1.05) < abs(got - 1.0), "the fit should dominate, not the seed"

    def test_far_outside_the_evidence_it_hands_back_to_the_seed(self):
        """Three standard deviations out, the fit contributes nothing. A slope fitted
        across a summer must not be extrapolated to a January night."""
        fit = self._fit(0.02, 1.0, mean=10.0, sd=1.0)
        assert self._f(-15.0, fit, seed=0.7) == pytest.approx(0.7)

    def test_it_fades_across_the_gap_rather_than_switching(self):
        fit = self._fit(0.02, 1.0, mean=10.0, sd=1.0)
        near = self._f(11.0, fit, seed=0.7)
        mid = self._f(12.0, fit, seed=0.7)
        far = self._f(14.0, fit, seed=0.7)
        assert near != pytest.approx(0.7), "close in, the fit should dominate"
        assert far == pytest.approx(0.7), "far out, the seed should"
        assert min(near, far) <= mid <= max(near, far), "and it should move between them"

    def test_a_wild_slope_cannot_hurt_at_the_centre_of_its_own_data(self):
        """The reason spread is not the gate.

        A least-squares line passes through the mean of its data, so at that mean it
        predicts the mean rate whatever the slope happens to be. Two fits with wildly
        different slopes but the same mean rate must therefore give the same answer
        there — which is why a narrow band of readings is safe to use where it was
        measured, and only becomes dangerous further away.
        """
        gentle = self._fit(0.01, 1.37, mean=13.0, sd=0.5)   # mean rate 1.5 at 13 °C
        absurd = self._fit(0.50, -5.0, mean=13.0, sd=0.5)   # also 1.5 at 13 °C
        at_centre = [self._f(13.0, f, seed=0.8, baseline=13.0) for f in (gentle, absurd)]
        assert at_centre[0] == pytest.approx(at_centre[1], rel=1e-9)
        # And a degree away, where the slopes genuinely differ, they no longer agree.
        assert self._f(14.0, gentle, seed=0.8, baseline=13.0) != pytest.approx(
            self._f(14.0, absurd, seed=0.8, baseline=13.0), rel=1e-3)

    def test_thin_evidence_leans_on_the_seed(self):
        """With three observations even the intercept is noisy, whatever the spread."""
        thin = self._fit(0.02, 1.0, n=3.0)
        thick = self._fit(0.02, 1.0, n=300.0)
        seed = 0.8
        assert abs(self._f(11.0, thin, seed=seed) - seed) < \
               abs(self._f(11.0, thick, seed=seed) - seed)

    def test_an_impossible_fitted_rate_is_declined(self):
        """A line that predicts a negative rate cannot form a ratio and must not try."""
        assert self._f(5.0, self._fit(0.5, -20.0), seed=0.9) == 0.9

    def test_the_result_is_clamped(self):
        from custom_components.mspa.predictor import (
            AMBIENT_FACTOR_MAX, AMBIENT_FACTOR_MIN)
        wild = self._fit(0.5, 0.1, mean=10.0, sd=10.0)
        for amb in (-20.0, 0.0, 40.0):
            f = self._f(amb, wild, seed=1.0)
            assert AMBIENT_FACTOR_MIN <= f <= AMBIENT_FACTOR_MAX
