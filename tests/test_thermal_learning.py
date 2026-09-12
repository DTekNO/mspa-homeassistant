"""The thermal model wired into the coordinator.

test_thermal.py covers the arithmetic in isolation. This covers the wiring: that a
heating crossing teaches `A`, that a cooling crossing teaches `tau`, that both survive a
restart, and that a spa with no history behaves like a new one.

The coordinator is built with `object.__new__` and populated by hand, as the other
learning tests do, so the learning can be exercised without an event loop.

Run with: python -m pytest tests/test_thermal_learning.py -v
"""
import pytest

from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.thermal import (
    A_ALPHA, DEFAULT_A, DEFAULT_TAU_H, TAU_ALPHA,
)


def _coord(**over):
    c = object.__new__(MSpaUpdateCoordinator)
    c.ambient_temp = 12.0
    c.config_entry = type("E", (), {"options": {"heater_power_heat": 2200}})()
    for k, v in over.items():
        setattr(c, k, v)
    return c


class TestANewSpaStartsFromTheSeeds:
    """Deleting .storage must give a spa that predicts sensibly from the first poll."""

    def test_the_model_is_seeded_not_empty(self):
        m = _coord().thermal_model()
        assert m.a == DEFAULT_A and m.tau_h == DEFAULT_TAU_H

    def test_it_can_already_answer(self):
        """A blank Ready at on day one is a broken dashboard; a seeded one is not."""
        assert _coord().thermal_minutes(20.0, 39.0, ambient=12.0) > 0

    def test_the_class_defaults_survive_object_new(self):
        """The fixtures build coordinators this way. If these were only set in
        __init__ every learning test in the suite would fail at once — which is
        precisely what happened when they were."""
        c = object.__new__(MSpaUpdateCoordinator)
        assert (c.thermal_a, c.thermal_tau_h) == (None, None)
        assert (c.thermal_a_n, c.thermal_tau_n) == (0, 0)

    def test_diagnostics_say_they_are_seeded(self):
        d = _coord().thermal_diagnostics()
        assert d["seeded_a"] and d["seeded_tau"]
        assert d["a_samples"] == 0 and d["tau_samples"] == 0


class TestLearningAFromHeating:

    def test_the_first_sample_replaces_the_seed_outright(self):
        """A spa far from the seed must converge at once, not crawl towards it."""
        c = _coord()
        c.learn_thermal_a(rate=0.80, water_mean=25.0, ambient=12.0)
        assert c.thermal_a == pytest.approx(0.80 + 13.0 / DEFAULT_TAU_H)
        assert c.thermal_a_n == 1

    def test_later_samples_are_smoothed(self):
        c = _coord()
        c.learn_thermal_a(1.00, 25.0, 12.0)
        first = c.thermal_a
        c.learn_thermal_a(1.20, 25.0, 12.0)
        second_sample = 1.20 + 13.0 / DEFAULT_TAU_H
        assert c.thermal_a == pytest.approx(
            A_ALPHA * second_sample + (1 - A_ALPHA) * first)
        assert c.thermal_a_n == 2

    def test_an_absurd_sample_leaves_the_estimate_alone(self):
        """Discarded, not clamped — a clamped stream reads as a converged answer."""
        c = _coord()
        c.learn_thermal_a(1.00, 25.0, 12.0)
        good = c.thermal_a
        c.learn_thermal_a(99.0, 25.0, 12.0)
        assert c.thermal_a == good and c.thermal_a_n == 1

    def test_missing_air_is_not_guessed_at(self):
        c = _coord()
        c.learn_thermal_a(1.00, 25.0, None)
        assert c.thermal_a is None

    def test_it_uses_the_learned_tau_not_the_seed(self):
        """A and tau are coupled through the subtraction, so a learned tau has to
        reach this or the two parameters describe different spas."""
        c = _coord(thermal_tau_h=20.0)
        c.learn_thermal_a(rate=0.80, water_mean=25.0, ambient=12.0)
        assert c.thermal_a == pytest.approx(0.80 + 13.0 / 20.0)


class TestLearningTauFromCooling:

    def test_a_cooling_stretch_teaches_tau(self):
        import math
        c = _coord()
        hours = 55.0 * math.log(26.0 / 22.0)          # 38 -> 34 at air 12, tau 55
        c.learn_thermal_tau(38.0, 34.0, 12.0, hours)
        assert c.thermal_tau_h == pytest.approx(55.0, rel=1e-6)
        assert c.thermal_tau_n == 1

    def test_later_samples_move_it_slowly(self):
        import math
        c = _coord(thermal_tau_h=55.0, thermal_tau_n=1)
        hours = 30.0 * math.log(26.0 / 22.0)          # a 30 h sample
        c.learn_thermal_tau(38.0, 34.0, 12.0, hours)
        assert c.thermal_tau_h == pytest.approx(
            TAU_ALPHA * 30.0 + (1 - TAU_ALPHA) * 55.0, rel=1e-6)

    def test_a_rising_temperature_is_not_a_cooling_sample(self):
        c = _coord()
        c.learn_thermal_tau(34.0, 38.0, 12.0, 2.0)
        assert c.thermal_tau_h is None

    def test_a_tiny_gap_is_refused(self):
        """Near the air temperature the logarithm is mostly measurement noise."""
        c = _coord()
        c.learn_thermal_tau(13.4, 13.1, 13.0, 4.0)
        assert c.thermal_tau_h is None


class TestTheTwoParametersStayIndependent:
    """The whole point of splitting them: one is the spa, the other is today."""

    def test_learning_a_does_not_move_tau(self):
        c = _coord(thermal_tau_h=48.0, thermal_tau_n=3)
        c.learn_thermal_a(1.0, 25.0, 12.0)
        assert c.thermal_tau_h == 48.0 and c.thermal_tau_n == 3

    def test_learning_tau_does_not_move_a(self):
        import math
        c = _coord(thermal_a=1.25, thermal_a_n=4)
        c.learn_thermal_tau(38.0, 34.0, 12.0, 55.0 * math.log(26.0 / 22.0))
        assert c.thermal_a == 1.25 and c.thermal_a_n == 4


class TestPersistence:

    def test_the_saved_shape_is_two_numbers_and_two_counts(self):
        """If this grows, the model has stopped being simple."""
        import inspect
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_data)
        for key in ("thermal_a", "thermal_tau_h", "thermal_a_n", "thermal_tau_n"):
            assert f'"{key}"' in src, key

    def test_a_store_without_the_keys_leaves_the_seeds_standing(self):
        """A cleared .storage, or one written before the model existed, must give
        new-spa behaviour rather than an exception."""
        stored = {"heat_rate": 0.9}
        c = _coord()
        c.thermal_a = stored.get("thermal_a")
        c.thermal_tau_h = stored.get("thermal_tau_h")
        c.thermal_a_n = stored.get("thermal_a_n", 0) or 0
        assert c.thermal_model().a == DEFAULT_A
        assert c.thermal_diagnostics()["seeded_a"] is True


class TestDiagnostics:

    def test_implied_volume_and_loss_are_reported(self):
        c = _coord(thermal_a=1.30, thermal_tau_h=55.0)
        d = c.thermal_diagnostics()
        assert 1200 < d["implied_litres"] < 1700
        assert 15 < d["loss_w_per_k"] < 60

    def test_the_chord_view_is_derived_and_decreasing(self):
        d = _coord(thermal_a=1.30, thermal_tau_h=55.0).thermal_diagnostics()
        r = d["chord_rates"]
        assert r[0] > r[1] > r[2]

    def test_no_air_means_no_chords_rather_than_wrong_ones(self):
        d = _coord(ambient_temp=None).thermal_diagnostics()
        assert d["chord_rates"] is None and d["asymptote_c"] is None
