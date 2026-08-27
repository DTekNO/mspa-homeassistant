"""Tests for the physical (Newton's law) model that is reported but never applied.

The integration ships three learned buckets plus an ambient correction bolted on
outside them. Newton's law would replace all of it with two parameters — and the
air-temperature term falls out of the physics instead of being learned, which is the
argument for it. See ROADMAP, *Alternative: a physical heating model instead of
buckets*, and the block comment at the foot of predictor.py.

Nothing here drives a prediction. What these tests protect is the *measurement*: that
the fit recovers a spa it is shown, that it declines rather than invents when it cannot,
and that a session carries the retrospective comparison so weeks of finished sessions
can settle whether adopting the model would have done better.

Run with: python -m pytest tests/test_physical_model.py -v
"""
import math
import random

import pytest

from custom_components.mspa.predictor import (
    NEWTON_MIN_N,
    newton_fit,
    newton_free_fit,
    newton_heating_minutes,
    physical_constants,
)


TAU, LIFT = 25.0, 45.0        # a spa that holds 45 °C above air, time constant 25 h


def _rate(water, air, tau=TAU, lift=LIFT):
    """The law itself: dT/dt = (T_air + P/k - T_water) / tau."""
    return (air + lift - water) / tau


def _traverses(n=60, noise=0.0, seed=7, waters=(25.0, 33.5, 38.25),
               air_range=(-5.0, 22.0), tau=TAU, lift=LIFT):
    """Band traverses from a spa that obeys the law exactly, plus optional noise."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        air = rng.uniform(*air_range)
        water = rng.choice(waters)
        rows.append({
            "usable": True,
            "band": 1,
            "rate": _rate(water, air, tau, lift) + (rng.gauss(0, noise) if noise else 0.0),
            "water_mean": water,
            "ambient_mean": air,
        })
    return rows


class TestTheFitRecoversASpa:
    """Shown a spa that obeys the law, the fit must report that spa's parameters."""

    def test_it_recovers_tau_and_the_asymptote(self):
        fit = newton_fit(_traverses(noise=0.02))
        assert fit["tau_h"] == pytest.approx(TAU, rel=0.02)
        assert fit["asymptote_lift_c"] == pytest.approx(LIFT, rel=0.02)

    def test_the_asymptote_is_a_lift_not_a_temperature(self):
        """`P/k` is how far above air the heater holds, so the same spa observed through
        a cold week and a warm one must report the same number. Reporting it absolute
        would make it look like a ceiling the spa cannot exceed, which is only true at
        the air temperature it happened to be measured at."""
        cold = newton_fit(_traverses(air_range=(-10.0, -5.0), seed=1))
        warm = newton_fit(_traverses(air_range=(15.0, 20.0), seed=1))
        assert cold["asymptote_lift_c"] == pytest.approx(LIFT, rel=0.02)
        assert warm["asymptote_lift_c"] == pytest.approx(LIFT, rel=0.02)

    def test_tau_is_measurable_before_the_weather_has_moved(self):
        """Worth being explicit about, because it is the constrained fit's one soft
        spot. The gap regressor varies through the *water* as well as the air, so three
        band midpoints give it a 13 °C spread on a single still day — `tau` comes out
        fine. The lift does not: separating it from the air term is what needs a range
        of weather, and until there is one the lift leans on the law being true rather
        than on evidence that it is. `newton_free_fit` is where that gets checked."""
        rows = _traverses(n=40, noise=0.02, air_range=(13.0, 13.4), seed=5)
        still = newton_fit(rows)
        assert still is not None
        assert still["tau_h"] == pytest.approx(TAU, rel=0.05)
        # The air coefficient over that same data is barely determined at all: its
        # standard error is forty times the water coefficient's, and two thirds of the
        # 1/tau = 0.04 it is trying to measure.
        free = newton_free_fit(rows)
        assert free["se_air"] > 20 * free["se_water"]

    def test_the_closed_form_matches_integrating_the_law(self):
        """t = tau x ln((A - T0)/(A - T1)). If that drifts from the differential
        equation it came from, every retrospective number is quietly wrong."""
        w0, w1, air = 22.0, 39.5, 12.0
        closed = newton_heating_minutes(w0, w1, air, TAU, LIFT)
        water, hours, dt = w0, 0.0, 1.0 / 3600.0
        while water < w1:
            water += _rate(water, air) * dt
            hours += dt
        assert closed == pytest.approx(hours * 60.0, rel=1e-3)


class TestItDeclinesRatherThanInvents:
    """Every refusal here is a finding. None of them may be filled in with a substitute
    number, because a plausible-looking wrong answer is worse than an absent one."""

    def test_too_few_traverses_gives_nothing(self):
        assert newton_fit(_traverses(n=NEWTON_MIN_N - 1)) is None

    def test_no_spread_in_the_gap_gives_nothing(self):
        """Thirty traverses all at the same water/air gap place no line at all — the
        slope they imply is whatever else happened to vary."""
        rows = _traverses(n=30, waters=(33.5,), air_range=(13.4, 13.6))
        assert newton_fit(rows) is None

    def test_a_rising_rate_is_refused(self):
        """Rate increasing with the gap is not a spa. Fitting it would report a negative
        time constant and a nonsense asymptote."""
        rows = _traverses(n=30)
        for r in rows:
            r["rate"] = 0.5 + 0.02 * (r["water_mean"] - r["ambient_mean"])
        assert newton_fit(rows) is None

    def test_unusable_traverses_are_excluded(self):
        """A traverse the rate learner refused is not evidence about the spa; it is
        evidence about the evening. It stays in the record and out of the fit."""
        rows = _traverses(n=30)
        assert newton_fit(rows) is not None
        for r in rows:
            r["usable"] = False
        assert newton_fit(rows) is None

    def test_an_unreachable_target_predicts_nothing(self):
        """A spa that cannot reach 40 °C on a January night must decline rather than
        promise a time it will miss."""
        assert newton_heating_minutes(22.0, 60.0, 12.0, TAU, LIFT) is None
        # Asymptote exactly at the target is still unreachable: it arrives at infinity.
        assert newton_heating_minutes(22.0, 12.0 + LIFT, 12.0, TAU, LIFT) is None

    def test_within_the_near_target_band_is_zero_not_a_refusal(self):
        """Matching the shipping model, so the two are comparable at the margin."""
        assert newton_heating_minutes(39.3, 39.5, 12.0, TAU, LIFT) == 0.0


class TestTheLawCanFail:
    """`newton_free_fit` is the only part that can disprove the model, so what it
    reports has to be trustworthy in both directions."""

    def test_a_spa_that_obeys_the_law_gives_a_ratio_of_one(self):
        """The falsifiable prediction: regress rate on water and air separately and the
        coefficients must come out equal and opposite. Nothing about a curve fit forces
        that. On 639 hours from the previous spa it came out 1.04."""
        free = newton_free_fit(_traverses(n=80, noise=0.02))
        assert free["ratio"] == pytest.approx(1.0, abs=0.05)
        assert free["coef_water"] == pytest.approx(-1.0 / TAU, rel=0.05)
        assert free["coef_air"] == pytest.approx(1.0 / TAU, rel=0.05)
        assert free["tau_from_water_h"] == pytest.approx(TAU, rel=0.05)

    def test_a_spa_that_does_not_obey_it_is_reported_as_such(self):
        """Air mattering half as much as water is the shape the current spa's statistics
        actually show (ratio 0.55 against 1.04 on the previous one). The fit must report
        that rather than average it away."""
        rows = _traverses(n=80)
        for r in rows:
            r["rate"] = 1.8 - 0.04 * r["water_mean"] + 0.02 * r["ambient_mean"]
        assert newton_free_fit(rows)["ratio"] == pytest.approx(0.5, abs=0.02)

    def test_collinearity_is_reported_so_a_ratio_is_not_read_alone(self):
        """Over a single heat-up the water climbs while the air does whatever the
        afternoon does. When the two move together the split is not identified, however
        tight the standard errors look — so the correlation travels with the ratio."""
        spread = newton_free_fit(_traverses(n=80, noise=0.02))
        rng = random.Random(3)
        locked = []
        for _ in range(80):
            air = rng.uniform(-5.0, 20.0)
            water = 20.0 + air          # perfectly collinear
            locked.append({"usable": True, "rate": _rate(water, air),
                           "water_mean": water, "ambient_mean": air})
        assert abs(spread["corr_water_air"]) < 0.5
        assert spread["identified"] is True
        # Perfectly collinear: declined outright, because the determinant is zero in
        # exact arithmetic and only floating-point noise away from it in practice.
        assert newton_free_fit(locked) is None
        # Near-collinear: reported, but flagged. Withholding it would hide that the
        # traverses are not varied enough, which is the thing worth knowing.
        rng2 = random.Random(11)
        near = [{"usable": True, "water_mean": 20.0 + a + rng2.gauss(0, 0.4),
                 "ambient_mean": a, "rate": _rate(20.0 + a, a)}
                for a in (rng2.uniform(-5.0, 20.0) for _ in range(80))]
        fit = newton_free_fit(near)
        assert fit is not None and fit["identified"] is False


class TestTheRetrospectiveIsRecorded:
    """The comparison has to be out-of-sample and it has to be head to head, or weeks of
    sessions will produce a number that only says the fit can fit its own data."""

    def test_the_estimate_uses_the_fit_from_before_the_session(self):
        """`newton_minutes` reads the fit as it stands when called. Called as a session
        opens, that fit cannot contain the session's own traverses — they have not
        happened. This is what makes the stored error a real prediction error."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = _traverses(n=40, noise=0.02)
        c.ambient_temp = 12.0
        before = c.newton_minutes(22.0, 39.5)
        assert before is not None
        # A later session's traverses move the fit; the earlier estimate is unaffected
        # because it was already taken.
        c._band_observations = c._band_observations + _traverses(n=40, seed=99, tau=40.0)
        assert c.newton_minutes(22.0, 39.5) != pytest.approx(before, rel=1e-6)

    def test_no_fit_yet_means_no_estimate_rather_than_a_guess(self):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c.ambient_temp = 12.0
        assert c.newton_minutes(22.0, 39.5) is None

    def test_the_parameters_used_are_stored_with_the_session(self):
        """The fit moves. Scoring past sessions against today's parameters months later
        would say nothing about how the model behaved at the time."""
        from custom_components.mspa.coordinator import _newton_params
        params = _newton_params(newton_fit(_traverses(noise=0.02)))
        assert set(params) == {"tau_h", "asymptote_lift_c", "n"}
        assert params["tau_h"] == pytest.approx(TAU, rel=0.02)
        assert _newton_params(None) is None


class TestTheSpecMakesItCheckable:
    """`P/C` is fitted and `P` is known, so `C` follows — and an equivalent volume that
    can be held against the nameplate. That is a second falsification test alongside the
    equal-and-opposite one, and it costs nothing to compute."""

    def test_it_recovers_a_known_tub(self):
        """A 2200 W element in 950 litres losing 44 W/K is close to this spa — the
        element measured against its spec. Shown exactly that, the derivation must
        return it."""
        litres, power, loss_w_per_k = 950.0, 2200.0, 44.0
        heat_capacity = litres * 4186.0                       # J/K
        tau = heat_capacity / loss_w_per_k / 3600.0           # h
        lift = power / loss_w_per_k                           # °C above air
        rows = _traverses(n=60, noise=0.01, tau=tau, lift=lift)
        phys = physical_constants(newton_fit(rows), power)
        assert phys["equivalent_litres"] == pytest.approx(litres, rel=0.03)
        assert phys["loss_w_per_k"] == pytest.approx(loss_w_per_k, rel=0.03)
        assert phys["standing_loss_w_at_20c_gap"] == pytest.approx(880.0, rel=0.03)

    def test_the_volume_is_derived_and_never_supplied(self):
        """The whole value of the number is that it is independent. Nothing in the
        signature accepts a measured volume, so a wrong one cannot reach a prediction."""
        import inspect
        assert set(inspect.signature(physical_constants).parameters) == {
            "fit", "heater_power_w"}

    def test_a_wrong_model_shows_up_as_a_wrong_tub(self):
        """The point of the check. A spa whose losses are twice what the law assumes
        fits fine on its own terms, and gives itself away on the volume."""
        rows = _traverses(n=60, noise=0.01, tau=12.0, lift=LIFT)
        phys = physical_constants(newton_fit(rows), 2200.0)
        assert phys["equivalent_litres"] < 700, (
            "half the time constant at the same lift is half the tub — if this reads "
            "plausible, the check is not checking anything")

    def test_no_power_means_no_derivation(self):
        fit = newton_fit(_traverses(noise=0.02))
        assert physical_constants(fit, None) is None
        assert physical_constants(fit, 0) is None
        assert physical_constants(None, 2200) is None

    def test_the_uncertainty_travels_with_it(self):
        """The intercept is the line extrapolated back to a zero water/air gap, about
        twenty degrees outside anything ever observed, so it is the worse-determined of
        the two parameters and the volume inherits that."""
        noisy = physical_constants(newton_fit(_traverses(n=60, noise=0.08, seed=2)), 2200.0)
        clean = physical_constants(newton_fit(_traverses(n=60, noise=0.01, seed=2)), 2200.0)
        assert noisy["equivalent_litres_se"] > 4 * clean["equivalent_litres_se"]

    def test_the_rated_power_is_the_full_heat_figure(self):
        """Rates are only ever learned while heat_state == 3, so the pre-heat rating
        never applies. Guarded because the two are separate config options and picking
        the wrong one would scale every derived volume by 4/3."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c.config_entry = type("E", (), {"options": {"heater_power_heat": 2200,
                                                    "heater_power_preheat": 1500}})()
        assert c.heater_power_heat_w == 2200
        c.config_entry = type("E", (), {"options": {}})()
        assert c.heater_power_heat_w == 2000        # DEFAULT_HEATER_POWER_HEAT
        c.config_entry = type("E", (), {"options": {"heater_power_heat": 0}})()
        assert c.heater_power_heat_w == 2000, "a zero rating must not divide by zero"

    def test_the_pump_is_not_counted_as_heat(self):
        """It runs for the whole of every traverse, which makes adding it tempting, and
        it was briefly added here. But it turns a pump. The motor is air-cooled in the
        control box, so most of its 60 W leaves to the air and only the hydraulic work
        reaches the water — a fraction nobody has measured. The `total_power` sensor is
        no evidence either way: it sums configured ratings, and it reports draw rather
        than heat."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c.config_entry = type("E", (), {"options": {"heater_power_heat": 2200,
                                                    "pump_power": 60}})()
        c._band_observations = _traverses(n=40, noise=0.02)
        assert c.physical_constants()["heater_power_w"] == 2200


class TestTheShadowRunsInParallel:
    """The physical model computes the same two things the bucket model decides — when
    the water is ready, and when a scheduled run must start — and decides neither. What
    these protect is that it stays a shadow, and that a gap in the record is readable.
    """

    def _coord(self, *, fitted=True, target=39.5, scheduled=None, triggered=False,
               ambient=12.0):
        from datetime import datetime, timedelta, timezone
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = _traverses(n=40, noise=0.02) if fitted else []
        c.ambient_temp = ambient
        c.scheduled_ready_at = scheduled
        c.schedule_target_temp = target if scheduled else None
        c._schedule_triggered = triggered
        c.newton_ready_at = c.newton_start_at = None
        c.scheduling_temp = lambda: 24.0
        return c

    def test_it_shadows_ready_at(self):
        c = self._coord()
        c._update_newton_shadow(24.0, 39.5)
        assert c.newton_ready_at is not None
        assert c.newton_start_at is None, "no schedule pending, so nothing to plan"

    def test_it_shadows_the_planned_start_against_the_scheduled_time(self, monkeypatch):
        """The same subtraction the trigger makes, so the two are differenceable.

        `dt_util.as_utc` is stubbed out by conftest, so it is given a real one here — the
        coordinator deliberately routes through it exactly as `_check_schedule_trigger`
        does, rather than taking a shortcut that would drift from the code it shadows.
        """
        from datetime import datetime, timedelta, timezone
        from custom_components.mspa import coordinator as mod
        monkeypatch.setattr(mod.dt_util, "as_utc",
                            lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc))
        due = datetime.now(timezone.utc) + timedelta(hours=20)
        c = self._coord(scheduled=due)
        c._update_newton_shadow(24.0, 39.5)
        minutes = c.newton_minutes(24.0, 39.5)
        assert (due - c.newton_start_at).total_seconds() / 60 == pytest.approx(
            minutes, rel=1e-6)

    def test_a_fired_schedule_has_no_planned_start_left(self):
        from datetime import datetime, timedelta, timezone
        due = datetime.now(timezone.utc) + timedelta(hours=2)
        c = self._coord(scheduled=due, triggered=True)
        c._update_newton_shadow(24.0, 39.5)
        assert c.newton_start_at is None
        assert c.newton_ready_at is not None, "but it is still heating towards something"

    def test_no_fit_leaves_a_gap_rather_than_a_guess(self):
        c = self._coord(fitted=False)
        c._update_newton_shadow(24.0, 39.5)
        assert c.newton_ready_at is None and c.newton_start_at is None

    def test_an_unreachable_target_leaves_a_gap_too(self):
        """A cold enough night puts the asymptote below the setpoint. That is a real
        answer from the model and the history should show it as absence, not as a
        substituted number from somewhere else."""
        c = self._coord(ambient=-40.0)
        c._update_newton_shadow(24.0, 39.5)
        assert c.newton_ready_at is None

    def test_a_pending_schedule_aims_at_the_schedule_target(self):
        """Mirroring the shipping sensor's choice of target, so the two series are
        answering the same question rather than two different ones."""
        from datetime import datetime, timedelta, timezone
        due = datetime.now(timezone.utc) + timedelta(hours=20)
        hot = self._coord(scheduled=due, target=40.0)
        hot._update_newton_shadow(24.0, 30.0)          # thermostat much lower
        cool = self._coord(scheduled=due, target=32.0)
        cool._update_newton_shadow(24.0, 30.0)
        assert hot.newton_ready_at > cool.newton_ready_at

    def test_the_shadow_drives_nothing(self):
        """The guarantee that matters. `_check_schedule_trigger` computes its own start
        from `_compute_heating_minutes`; nothing in the trigger or the Ready at path may
        read the shadow."""
        import inspect
        from custom_components.mspa import coordinator as mod
        for name in ("_check_schedule_trigger", "_compute_heating_minutes",
                     "_heating_minutes_variant", "scheduling_temp"):
            src = inspect.getsource(getattr(mod.MSpaUpdateCoordinator, name))
            assert "newton" not in src.lower(), (
                f"{name} must not consult the shadow model")
        from custom_components.mspa import sensor as sensor_mod
        assert "newton" not in inspect.getsource(
            sensor_mod._compute_ready_at).lower()


class TestTheModelCanBeSwitchedWithoutMovingAnything:
    """The seam that lets Ready at and the Heat schedule run on either model.

    The property being protected is that switching changes arithmetic and nothing else:
    same entities, same ids, same meaning, so no dashboard or automation has to be
    touched to try the physical model or to go back. It is deliberately not in the config
    flow — see CONF_PREDICTION_MODEL — but the seam has to work, or turning it on later
    is a rewrite rather than a decision.
    """

    def _coord(self, model=None, *, fitted=True, ambient=12.0):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = _traverses(n=40, noise=0.02) if fitted else []
        c.ambient_temp = ambient
        c.ambient_baseline = 15.0
        c.heat_rate_buckets = [1.2, 1.0, 0.8]
        c.computed_heat_rate = 1.0
        c.prediction_bias = 1.0
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        c._band_stats = {}
        c._last_data = {}
        c._newton_fallback_active = False
        c.config_entry = type("E", (), {
            "options": {} if model is None else {"prediction_model": model}})()
        return c

    def test_buckets_are_the_default(self):
        c = self._coord()
        assert c.prediction_model == "buckets"
        assert c.uses_frozen_plan is True
        assert c.heating_minutes(24.0, 39.5) == pytest.approx(
            c._predictor().heating_minutes(24.0, 39.5))

    def test_selecting_newton_changes_the_answer(self):
        buckets, newton = self._coord(), self._coord("newton")
        assert newton.heating_minutes(24.0, 39.5) == pytest.approx(
            newton.newton_minutes(24.0, 39.5))
        assert newton.heating_minutes(24.0, 39.5) != pytest.approx(
            buckets.heating_minutes(24.0, 39.5), rel=1e-3)

    def test_newton_drops_the_frozen_plan(self):
        """The frozen plan and its band-edge revisions exist because a bucket rate is
        weeks old. The physical model re-derives from the current water and outdoor
        temperature every poll, so there is nothing to freeze and nothing to revise
        towards."""
        assert self._coord("newton").uses_frozen_plan is False

    def test_it_never_leaves_a_time_blank(self):
        """The diagnostic shadow sensors leave a gap when the model declines, because
        the gap is the finding. This path must not: a blank Ready at is a broken
        dashboard, so it falls back to buckets."""
        no_fit = self._coord("newton", fitted=False)
        assert no_fit.newton_minutes(24.0, 39.5) is None
        assert no_fit.heating_minutes(24.0, 39.5) is not None

        unreachable = self._coord("newton", ambient=-40.0)
        assert unreachable.newton_minutes(24.0, 39.5) is None
        assert unreachable.heating_minutes(24.0, 39.5) is not None

    def test_the_fallback_is_logged_on_the_edges_only(self):
        """Every poll would be thousands of identical lines a day, and the transition is
        the only part that is news."""
        c = self._coord("newton", fitted=False)
        c.heating_minutes(24.0, 39.5)
        assert c._newton_fallback_active is True
        c.heating_minutes(24.0, 39.5)
        assert c._newton_fallback_active is True, "still down, still not re-announced"
        c._band_observations = _traverses(n=40, noise=0.02)
        c.heating_minutes(24.0, 39.5)
        assert c._newton_fallback_active is False, "recovery must clear the latch"

    def test_the_option_is_not_offered_in_the_config_flow(self):
        """Being evaluated, not offered. A switch in the options dialog would invite
        people to adopt a model no spa has yet shown to work."""
        from pathlib import Path
        root = Path(__file__).parent.parent / "custom_components" / "mspa"
        # Read rather than import, as test_config_flow_translations does: config_flow
        # pulls in homeassistant.helpers.selector, which the stubs do not provide.
        for name in ("config_flow.py", "strings.json", "translations/en.json"):
            assert "prediction_model" not in (root / name).read_text(), (
                f"{name} must not offer the model switch")

    def test_every_production_path_goes_through_the_seam(self):
        """The whole switch rests on there being exactly one entry point. A second
        caller building its own HeatPredictor would silently keep using buckets."""
        import inspect
        from custom_components.mspa import sensor as sensor_mod
        src = inspect.getsource(sensor_mod._segmented_heating_minutes)
        assert "coordinator.heating_minutes" in src
        assert "HeatPredictor.from_coordinator" not in src
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        trigger = inspect.getsource(MSpaUpdateCoordinator._check_schedule_trigger)
        assert "_compute_heating_minutes" in trigger
