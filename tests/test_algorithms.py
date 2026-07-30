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
from custom_components.mspa.sensor import _anchor_eta_utc


# ── Minimal coordinator stub for anchor tests ─────────────────────────────────

class _Coord:
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
