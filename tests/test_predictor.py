"""Tests for the shared heat-up prediction model.

The heating-time maths used to exist twice — once in `sensor` for the display and
once in `coordinator` for the trigger, the latter documented as mirroring the former
"to avoid a circular import" that did not exist. They had drifted, so the Heat
Schedule sensor and the scheduler could disagree about the same spa. These tests pin
the single implementation and the three places the old copies differed.

Run with: python -m pytest tests/test_predictor.py -v
"""
import pytest

from custom_components.mspa.predictor import (
    HEAT_BUCKET_T1,
    HEAT_BUCKET_T2,
    NEAR_TARGET_BAND,
    HeatPredictor,
    ambient_rate_factor,
    bucket_index,
)

BUCKETS = (1.03, 0.99, 0.75)


def _p(**kw):
    kw.setdefault("buckets", BUCKETS)
    return HeatPredictor(**kw)


class TestBuckets:

    def test_boundaries(self):
        assert bucket_index(HEAT_BUCKET_T1 - 0.1) == 0
        assert bucket_index(HEAT_BUCKET_T1) == 1
        assert bucket_index(HEAT_BUCKET_T2 - 0.1) == 1
        assert bucket_index(HEAT_BUCKET_T2) == 2

    def test_each_segment_uses_its_own_rate(self):
        """A span crossing both boundaries must not be priced at one flat rate."""
        p = _p()
        mins = p.heating_minutes(29.5, 39.5)
        flat = (39.5 - 29.5) / BUCKETS[0] * 60
        assert mins > flat, "the slower upper buckets were ignored"


class TestNearTargetBand:
    """A gap under the near-target hysteresis is 'already there'.

    The two old copies disagreed here: the coordinator returned 0 while the sensor
    returned ~24 minutes for a 0.3 °C gap, so the trigger considered the session
    finished while the display still promised nearly half an hour.
    """

    def test_sub_band_gap_is_zero(self):
        assert _p().heating_minutes(39.2, 39.5) == 0.0

    def test_a_gap_at_the_band_is_real_work(self):
        assert _p().heating_minutes(39.5 - NEAR_TARGET_BAND, 39.5) > 0

    def test_already_at_or_past_target(self):
        assert _p().heating_minutes(39.5, 39.5) == 0.0
        assert _p().heating_minutes(40.0, 39.5) == 0.0


class TestFallbacks:

    def test_missing_bucket_uses_the_nearest_not_the_lowest(self):
        """An unobserved hot bucket is better approximated by mid than by cold.

        The coordinator's old copy scanned 0,1,2 and substituted the *cold* rate for
        a near-setpoint one, over-predicting the rate where losses matter most.
        """
        p = HeatPredictor(buckets=(1.10, 1.00, None))
        assert p.bucket_rate(38.0) == pytest.approx(1.00)

    def test_a_bucket_corrupted_to_zero_is_ignored(self):
        p = HeatPredictor(buckets=(1.10, 0.0, 0.90))
        assert p.bucket_rate(33.0) in (pytest.approx(1.10), pytest.approx(0.90))

    def test_flat_rate_then_device_rate(self):
        assert HeatPredictor(buckets=(None,) * 3, flat_rate=1.2).bucket_rate(25.0) == pytest.approx(1.2)
        assert HeatPredictor(buckets=(None,) * 3, device_rate=0.8).bucket_rate(25.0) == pytest.approx(0.8)

    def test_no_data_at_all_returns_none(self):
        assert HeatPredictor(buckets=(None,) * 3).bucket_rate(25.0) is None
        assert HeatPredictor(buckets=(None,) * 3).heating_minutes(25.0, 39.5) is None


class TestCorrectionPrecedence:
    """Observed-this-session wins, then the session scalar, then the weather model."""

    def test_fresh_bucket_is_used_verbatim(self):
        p = _p(fresh_buckets={1}, session_scalar=1.5, ambient_temp=0.0, ambient_baseline=14.0)
        assert p.bucket_rate(33.0) == pytest.approx(BUCKETS[1])

    def test_session_scalar_supersedes_the_weather_model(self):
        p = _p(session_scalar=1.5, ambient_temp=0.0, ambient_baseline=14.0)
        assert p.bucket_rate(33.0) == pytest.approx(BUCKETS[1] * 1.5)

    def test_weather_model_applies_when_nothing_observed(self):
        p = _p(ambient_temp=4.0, ambient_baseline=14.0)
        expected = BUCKETS[1] * ambient_rate_factor(1, 4.0, 14.0)
        assert p.bucket_rate(33.0) == pytest.approx(expected)

    def test_missing_ambient_data_is_neutral(self):
        assert _p().bucket_rate(33.0) == pytest.approx(BUCKETS[1])


class TestBiasAndEffectiveRate:

    def test_bias_scales_the_time(self):
        base = _p().heating_minutes(29.5, 39.5)
        assert _p(prediction_bias=0.96).heating_minutes(29.5, 39.5) == pytest.approx(base * 0.96)

    def test_effective_rate_is_the_span_over_the_time(self):
        p = _p(prediction_bias=0.96)
        mins = p.heating_minutes(29.5, 39.5)
        assert p.effective_rate(29.5, 39.5) == pytest.approx(10.0 / (mins / 60.0))

    def test_effective_rate_is_none_without_work_to_do(self):
        assert _p().effective_rate(39.5, 39.5) is None


class TestIndependence:
    """The model must stay free of Home Assistant so it can be extracted."""

    def test_no_homeassistant_or_package_imports(self):
        """Parsed from the AST, not scanned as text — the docstring mentions both."""
        import ast
        import pathlib

        tree = ast.parse(
            pathlib.Path("custom_components/mspa/predictor.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append("." * (node.level or 0) + (node.module or ""))
        assert not [m for m in imported if "homeassistant" in m], imported
        assert not [m for m in imported if m.startswith(".")], imported
