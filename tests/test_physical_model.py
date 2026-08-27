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
    _usable_rows,
    seed_rows_from_buckets,
    forecast_window_mean,
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
        c.ambient_baseline = 18.4
        c.heat_rate_buckets = None
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
        c.ambient_baseline = 18.4
        c.heat_rate_buckets = None            # nothing recorded and nothing to seed from
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
        c.ambient_baseline = 18.4
        c.heat_rate_buckets = None
        assert c.physical_constants()["heater_power_w"] == 2200


class TestTheShadowRunsInParallel:
    """The physical model computes the same two things the bucket model decides — when
    the water is ready, and when a scheduled run must start — and decides neither. What
    these protect is that it stays a shadow, and that a gap in the record is readable.
    """

    def _coord(self, *, fitted=True, target=39.5, scheduled=None, triggered=False,
               ambient=12.0, buckets=None):
        from datetime import datetime, timedelta, timezone
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = _traverses(n=40, noise=0.02) if fitted else []
        # No buckets by default, so "not fitted" means nothing to fall back on either.
        # Seeding is exercised in TestBucketsCanPrimeTheModel.
        c.heat_rate_buckets = buckets
        c.ambient_baseline = 18.4
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
        # Flat enough that the seed is refused, so "no traverses" still means no fit —
        # these tests are about the fallback, not about seeding.
        c.ambient_baseline = None
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
        assert "self.heating_minutes(" in trigger, (
            "the trigger must price its start through the seam, not its own arithmetic")
        assert "HeatPredictor(" not in trigger


class TestAFreshFillIsNotThrownAway:
    """A refill climbs from far below anything the archive holds. That run is the best
    evidence the physical model will ever get — a large water variation at close to
    constant outdoor temperature, which is what separates `tau` from the air term — and
    it happens twice a year at best, reluctantly, because it is work and it strains the
    well.

    No fill temperature is assumed anywhere. Groundwater arrives near 6 °C, but water
    buffered in an uninsulated outdoor tank equilibrates towards the air, so a fill can
    start anywhere from a couple of degrees to the middle teens, and in late autumn it
    may be colder than the well. What matters is that the span is kept, not where it
    began.

    Until 2026-08-27 it was discarded, because band observations were gated on the
    *bucket* learning range and `in_learning_range(6, 20)` is False.
    """

    def _coord(self):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c._band_stats = {}
        c._window_disturbed = False
        c._window_amb_sum = c._window_amb_n = 0
        c._window_wind_sum = c._window_wind_n = 0
        c.ambient_temp = 8.0
        c.ambient_wind = 2.0
        c.ambient_baseline = 8.0
        c.heat_rate_buckets = None
        return c

    def test_a_sub_twenty_traverse_is_recorded(self):
        c = self._coord()
        c._record_band_observation(0, 6.0, 20.0, 9.0, 1.55, bucket_learnable=False)
        assert len(c._band_observations) == 1
        row = c._band_observations[0]
        assert row["from_temp"] == 6.0 and row["bucket_learnable"] is False
        assert row["water_mean"] == 13.0
        assert row["delta_mean"] == pytest.approx(5.0), "water 13 against air 8"

    def test_and_the_physical_model_uses_it(self):
        c = self._coord()
        c._record_band_observation(0, 6.0, 20.0, 9.0, 1.55, bucket_learnable=False)
        assert len(_usable_rows(c._band_observations)) == 1

    def test_but_no_bucket_learns_from_it(self):
        """The 20 °C floor is a bucket constraint and stays one. The cold bucket is a
        flat chord over 20-30; a span from 6 describes something else, and letting it in
        would distort the rate other sessions depend on."""
        c = self._coord()
        c._record_band_observation(0, 6.0, 20.0, 9.0, 1.55, bucket_learnable=False)
        assert c._band_stats == {}, "the per-band weather fit must not see it"
        c._record_band_observation(0, 20.0, 30.0, 8.0, 1.25, bucket_learnable=True)
        assert c._band_stats, "an in-range traverse still accumulates as before"

    def test_a_refill_gives_the_fit_the_spread_routine_runs_cannot(self):
        """The point of keeping it. Band-2-only traverses all sit at water ~38, so the
        gap varies through the weather alone and `tau` rests on the ambient moving. One
        refill varies the water by 14 °C in a single run."""
        routine = [{"usable": True, "rate": _rate(38.25, a), "water_mean": 38.25,
                    "ambient_mean": a} for a in (11.0, 12.0, 13.0, 12.5, 11.5,
                                                 13.5, 12.0, 11.0, 12.5, 13.0)]
        assert newton_fit(routine) is None, (
            "ten routine top-ups in settled weather determine nothing")
        refill = routine + [
            {"usable": True, "rate": _rate(w, 12.0), "water_mean": w,
             "ambient_mean": 12.0}
            for w in (13.0, 25.0, 33.5)]
        fit = newton_fit(refill)
        assert fit is not None and fit["tau_h"] == pytest.approx(TAU, rel=0.05)

    def test_a_fill_starting_near_air_temperature_is_still_a_plausible_rate(self):
        """Buffered water starts near the air temperature, so the water/air gap is near
        zero — and under the law that is where the rate is *fastest*. Worth pinning,
        because the sampler rejects anything above _MAX_HEAT_RATE and silently dropping
        the fastest hours of the one run that matters would be the same bug in a new
        place. For this spa the law predicts about 2.0 °C/h at zero gap against a ceiling
        of 3.0, and it would take water eighteen degrees *below* the air to breach it."""
        from custom_components.mspa.coordinator import _MAX_HEAT_RATE
        tau, lift = 25.6, 51.0
        at_zero_gap = lift / tau
        assert at_zero_gap < _MAX_HEAT_RATE
        # Water colder than the air — a real possibility for a tank filled in autumn.
        assert (lift + 6.0) / tau < _MAX_HEAT_RATE


class TestAFillDoesNotPoisonTheBias:
    """A fill's own prediction being wrong does not matter — nobody is watching Ready at
    on a freshly filled spa. What would matter is the fill teaching `prediction_bias`
    something, because the bias outlives the session and is applied to every ordinary
    heat-up afterwards.
    """

    def _record(self, start, target=39.5, est=1800.0, actual=1530.0):
        return {"start_temp": start, "target_temp": target,
                "estimated_minutes": est, "actual_minutes": actual}

    def test_an_ordinary_cold_start_still_teaches_the_bias(self):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        assert MSpaUpdateCoordinator._bias_ratio(self._record(22.0)) == pytest.approx(0.85)

    def test_a_fill_teaches_it_nothing(self):
        """Most of a fill was never priced by a bucket: the cold bucket is a chord over
        20-30, and from 8 °C that chord is extrapolated twelve degrees past its evidence.
        The ratio measures the extrapolation, not the model."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        for start in (2.0, 8.0, 15.0, 19.9):
            assert MSpaUpdateCoordinator._bias_ratio(self._record(start)) is None

    def test_the_boundary_is_the_bucket_learning_floor(self):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        from custom_components.mspa.predictor import HEAT_BUCKET_LEARN_MIN
        assert MSpaUpdateCoordinator._bias_ratio(
            self._record(HEAT_BUCKET_LEARN_MIN)) is not None
        assert MSpaUpdateCoordinator._bias_ratio(
            self._record(HEAT_BUCKET_LEARN_MIN - 0.1)) is None

    def test_the_fill_is_still_recorded_and_still_scored(self):
        """Excluded from the bias, not from the record. The session still reaches
        prediction_history with its errors under both models, which is the comparison the
        whole exercise exists for — and the traverses still reach the physical fit."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator._bias_ratio)
        assert "_prediction_history" not in src and "_band_observations" not in src


class TestBucketsCanPrimeTheModel:
    """A bucket *is* physical data already digested — "this spa climbs at r °C/h across
    this span" is one point on the line the law describes. Three buckets are three
    points, and a straight line needs two, so a spa that has been learning for months
    should not have to start the physical model from nothing. Eight traverses is several
    weeks of ordinary use.
    """

    TAU, LIFT, AIR = 25.6, 51.0, 18.4

    def _chords(self, tau=None, lift=None):
        """What a bucket would actually learn: the chord rate, span over time, which is a
        harmonic mean along the span rather than the rate at its middle."""
        import math
        tau, lift = tau or self.TAU, lift or self.LIFT
        A = self.AIR + lift
        return [(hi - lo) / (tau * math.log((A - lo) / (A - hi)))
                for lo, hi in ((20, 30), (30, 37), (37, 39))]

    def test_three_buckets_are_enough_to_place_the_line(self):
        fit = newton_fit([], seed=seed_rows_from_buckets(self._chords(), self.AIR))
        assert fit["seeded"] is True
        assert fit["tau_h"] == pytest.approx(self.TAU, rel=0.02)
        assert fit["asymptote_lift_c"] == pytest.approx(self.LIFT, rel=0.02)

    def test_the_midpoint_approximation_costs_almost_nothing(self):
        """A bucket learns the chord rate, and the seed places it at the span's midpoint.
        The two differ, but by well under a percent — small next to the baseline
        approximation sitting alongside it."""
        fit = newton_fit([], seed=seed_rows_from_buckets(self._chords(), self.AIR))
        assert abs(fit["tau_h"] / self.TAU - 1) < 0.02
        assert abs(fit["asymptote_lift_c"] / self.LIFT - 1) < 0.02

    def test_flat_buckets_are_refused_rather_than_believed(self):
        """The reason the gate exists. This spa's live buckets have read 1.03/0.99/1.01,
        and a line through those implies a body that sheds almost no heat — tau 512 h and
        a lift of 531 °C, water that would never stop rising. Seeding from that would be
        worse than not seeding at all."""
        assert newton_fit([], seed=seed_rows_from_buckets(
            [1.03, 0.99, 1.01], self.AIR)) is None

    def test_a_properly_shaped_bucket_curve_is_accepted(self):
        """The same spa's statistics-derived shape, which is what the buckets should look
        like — the contrast is the whole finding."""
        fit = newton_fit([], seed=seed_rows_from_buckets(
            [1.263, 1.086, 0.841], self.AIR))
        assert fit is not None and fit["seeded"] is True
        assert 10.0 < fit["tau_h"] < 80.0
        assert 20.0 < fit["asymptote_lift_c"] < 100.0

    def test_real_traverses_replace_the_seed_rather_than_blend_with_it(self):
        """A blend would go on carrying the bucket model's shape into a fit whose whole
        purpose is to replace it."""
        seed = seed_rows_from_buckets(self._chords(), self.AIR)
        real = _traverses(n=NEWTON_MIN_N, noise=0.02)
        assert newton_fit(real, seed=seed)["seeded"] is False
        assert newton_fit(real, seed=seed)["tau_h"] == pytest.approx(
            newton_fit(real)["tau_h"])

    def test_the_seed_never_reaches_the_falsification_test(self):
        """`newton_free_fit` exists to test the law against independent evidence. Points
        manufactured from a three-bucket model cannot test anything, and a ratio of 1.0
        derived from them would be arithmetic wearing a result."""
        import inspect
        assert "seed" not in inspect.signature(newton_free_fit).parameters
        assert newton_free_fit(seed_rows_from_buckets(self._chords(), self.AIR)) is None

    def test_no_baseline_means_no_seed(self):
        """Every seeded point is attributed to the baseline, so without one there is no
        temperature to attribute them to."""
        assert seed_rows_from_buckets([1.2, 1.0, 0.8], None) == []
        assert seed_rows_from_buckets(None, 18.4) == []

    def test_unlearned_buckets_are_skipped_not_zeroed(self):
        rows = seed_rows_from_buckets([1.2, None, 0.8], self.AIR)
        assert len(rows) == 2
        assert [r["water_mean"] for r in rows] == [25.0, 38.0]


class TestTheSolarConfoundIsRecorded:
    """Sun on the shell is an unmodelled heat input, and it is the confound most likely
    to be mistaken for a result.

    A well-insulated tub can stop cooling altogether on a bright afternoon, and during a
    *heating* traverse the same gain inflates the measured rate. Because sun correlates
    with warm air and with daytime, that pushes the fitted air coefficient up — which is
    exactly the coefficient `newton_free_fit` checks. A ratio above 1.0 could be Newton's
    law failing, or it could be the sun, and nothing recorded before this could separate
    them.

    Not modelled and not corrected for. Recorded, so the question becomes answerable.
    """

    def _coord(self, condition=None):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c._band_stats = {}
        c._window_disturbed = False
        c._window_amb_sum = c._window_amb_n = 0
        c._window_wind_sum = c._window_wind_n = 0
        c._window_condition_counts = {}
        c.ambient_temp = 14.0
        c.ambient_wind = 2.0
        c.ambient_condition = condition
        return c

    def test_the_condition_travels_with_the_traverse(self):
        c = self._coord("sunny")
        c._record_band_observation(2, 37.0, 39.0, 2.0, 1.0)
        assert c._band_observations[0]["condition"] == "sunny"

    def test_it_is_the_modal_condition_not_the_last(self):
        """A five-hour band can start overcast and end in sun. What matters for a solar
        confound is which it mostly was."""
        c = self._coord("cloudy")
        c._window_condition_counts = {"cloudy": 12, "sunny": 4}
        c.ambient_condition = "sunny"
        c._record_band_observation(1, 30.0, 37.0, 5.0, 1.1)
        assert c._band_observations[0]["condition"] == "cloudy"

    def test_no_weather_entity_means_no_condition_rather_than_a_crash(self):
        c = self._coord(None)
        c._record_band_observation(2, 37.0, 39.0, 2.0, 1.0)
        assert c._band_observations[0]["condition"] is None

    def test_nothing_consumes_it_yet(self):
        """Recorded for a question nobody is answering. It must not have quietly become
        an input — correcting for sun on one recorded string would be worse than not
        correcting at all."""
        import inspect
        from custom_components.mspa import predictor
        assert "condition" not in inspect.getsource(predictor.newton_fit)
        assert "condition" not in inspect.getsource(predictor.newton_free_fit)
        assert "condition" not in inspect.getsource(predictor._usable_rows)


class TestTheTwoNumbersAreComparable:
    """`Newton start at` is a true shadow: same question, same scheduled time, both
    timestamps, so the pair differences directly. `Newton ready at` is not — the shipping
    Ready at shows the *scheduled time verbatim* while a schedule is pending, and shows
    a string like "Ready" or "11:00 +2d" rather than an estimate at all.

    That asymmetry is accepted rather than fixed: the raw estimate is the useful thing to
    watch, and dressing it up as the display sensor would hide the wandering the shadow
    exists to expose. What is not acceptable is a side-by-side number that answers a
    different question, which is what shipped first.
    """

    def _coord(self, *, scheduled=True):
        from datetime import datetime, timedelta, timezone
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c._band_stats = {}
        c.heat_rate_buckets = [1.24, 1.095, 0.878]
        c.ambient_baseline = 18.41
        c.ambient_temp = 15.0
        c.computed_heat_rate = 0.972
        c.prediction_bias = 1.0
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        c._last_data = {}
        c.newton_ready_at = c.newton_start_at = None
        c.newton_target_temp = c.newton_plan_temp = None
        c.scheduled_ready_at = (datetime.now(timezone.utc) + timedelta(hours=40)
                                if scheduled else None)
        c.schedule_target_temp = 39.5 if scheduled else None
        c._schedule_triggered = False
        c.scheduling_temp = lambda: 33.5
        return c

    def test_the_span_it_aimed_at_is_recorded(self):
        """Otherwise a Ready-at row is unreadable: the target switches between the
        thermostat's and the schedule's depending on whether a schedule is pending."""
        c = self._coord()
        c._update_newton_shadow(33.5, 34.0)
        assert c.newton_target_temp == 39.5, "a pending schedule outranks the thermostat"
        assert c.newton_plan_temp == 33.5

    def test_the_side_by_side_number_answers_the_same_question(self):
        """The bug this replaces. `_minutes_to_target` aims at the *thermostat*, so a spa
        holding at temperature with a schedule set for the day after reported 0 against a
        Newton estimate of five and a half hours — and the obvious reading of that pair
        is that the physical model is broken."""
        from custom_components.mspa.sensor import MSpaNewtonReadyAtSensor
        c = self._coord()
        c._update_newton_shadow(33.5, 34.0)
        s = object.__new__(MSpaNewtonReadyAtSensor)
        s.coordinator = c
        shipping = s._shipping_equivalent()
        buckets = c._predictor().heating_minutes(33.5, 39.5)
        assert shipping == pytest.approx(buckets, abs=0.1)
        assert shipping > 60, "a six-degree climb is hours, not zero"

    def test_it_is_absent_rather_than_wrong_when_there_is_no_span(self):
        from custom_components.mspa.sensor import MSpaNewtonReadyAtSensor
        c = self._coord(scheduled=False)
        c.newton_target_temp = c.newton_plan_temp = None
        s = object.__new__(MSpaNewtonReadyAtSensor)
        s.coordinator = c
        assert s._shipping_equivalent() is None

    def test_the_start_times_remain_directly_differenceable(self):
        """The half that always was comparable, and the one that scores the planner."""
        from custom_components.mspa.sensor import MSpaNewtonStartAtSensor
        from datetime import timezone
        from custom_components.mspa import coordinator as mod
        c = self._coord()
        mod.dt_util.as_utc = lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        c._last_computed_start_at = c.scheduled_ready_at
        c._update_newton_shadow(33.5, 34.0)
        s = object.__new__(MSpaNewtonStartAtSensor)
        s.coordinator = c
        assert c.newton_start_at is not None
        assert s._shipping_equivalent() is not None, "both are timestamps on one axis"


class TestPlanningUsesTheForecast:
    """A schedule is committed hours before it runs, in weather that will have changed
    by the time it does. Planning from the instantaneous reading is out by +14% on an
    autumn morning and -10% on a winter night, and the sign flips with the time of day —
    so no scalar correction could absorb it.
    """

    def _rows(self, start, temps):
        from datetime import timedelta
        return [(start + timedelta(hours=i), t) for i, t in enumerate(temps)]

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)

    def test_the_window_is_anchored_at_the_finish(self):
        """Where the law puts the weight: the exact solution weights air temperature by
        e^-(t-s)/tau, which is largest for the hours nearest the end of the run."""
        from datetime import timedelta
        now = self._now()
        rows = self._rows(now, [0] * 6 + [10] * 6)      # cold first, mild later
        mean, n, kind = forecast_window_mean(rows, now + timedelta(hours=11), 6.0)
        # Hours 5..10 inclusive: six of them, for a six-hour span. A stamp marks the
        # start of its hour, so the one at the finish belongs to the hour after the run.
        assert kind == "window" and n == 6
        assert mean == pytest.approx((0.0 + 10.0 * 5) / 6)
        assert mean > 8.0, "weighted toward the finish, not the start"
        # And the same run finishing six hours earlier sees the cold half instead.
        early, n_early, _ = forecast_window_mean(rows, now + timedelta(hours=6), 6.0)
        assert n_early == 6 and early == pytest.approx(0.0)

    def test_the_span_is_capped(self):
        from datetime import timedelta
        from custom_components.mspa.predictor import FORECAST_MAX_SPAN_H
        now = self._now()
        rows = self._rows(now, list(range(48)))
        _mean, n, _k = forecast_window_mean(rows, now + timedelta(hours=47), 40.0)
        assert n <= FORECAST_MAX_SPAN_H + 1

    def test_a_schedule_beyond_the_forecast_uses_the_nearest_hours(self):
        """met.no through Home Assistant offers 48 hours and a schedule may be set
        further out, so this is ordinary rather than an edge case. The last hours
        available beat an instantaneous reading from a day and a half earlier."""
        from datetime import timedelta
        now = self._now()
        rows = self._rows(now, [5.0] * 48)
        got = forecast_window_mean(rows, now + timedelta(hours=80), 8.0)
        assert got is not None
        mean, _n, kind = got
        assert kind == "tail" and mean == pytest.approx(5.0)

    def test_no_forecast_means_no_answer_rather_than_a_guess(self):
        from datetime import timedelta
        assert forecast_window_mean([], self._now(), 6.0) is None
        assert forecast_window_mean(self._rows(self._now(), [5, 5]), None, 6.0) is None

    def _coord(self, rows=None, ambient=15.0):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c._band_stats = {}
        c.heat_rate_buckets = [1.24, 1.095, 0.878]
        c.ambient_baseline = 18.41
        c.ambient_temp = ambient
        c.computed_heat_rate = 0.972
        c.prediction_bias = 1.0
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        c._last_data = {}
        c._newton_fallback_active = False
        c._forecast_rows = rows or []
        c.config_entry = type("E", (), {"options": {}})()
        return c

    def test_the_estimate_moves_when_the_forecast_disagrees_with_now(self):
        from datetime import timedelta
        now = self._now()
        # Mild right now, freezing across the run that finishes in the small hours.
        c = self._coord(self._rows(now, [-5.0] * 24), ambient=15.0)
        finish = now + timedelta(hours=12)
        warm = c.heating_minutes(33.5, 39.5)
        mean, kind = c.forecast_ambient_for(finish, 33.5, 39.5)
        cold = c.heating_minutes(33.5, 39.5, ambient=mean)
        assert mean == pytest.approx(-5.0) and kind == "window"
        assert cold > warm, "a freezing night must plan a longer run than a mild evening"

    def test_it_falls_back_to_now_when_there_is_no_forecast(self):
        """The whole feature is optional. No weather entity, an entity without hourly
        support, or a service that raises all leave planning exactly as it was."""
        c = self._coord(rows=[])
        assert c.forecast_ambient_for(self._now(), 33.5, 39.5) is None
        assert c.heating_minutes(33.5, 39.5) is not None

    def test_the_override_reaches_both_models(self):
        buckets = self._coord()
        buckets.config_entry = type("E", (), {"options": {}})()
        newton = self._coord()
        newton.config_entry = type("E", (), {
            "options": {"prediction_model": "newton"}})()
        for c in (buckets, newton):
            mild = c.heating_minutes(33.5, 39.5, ambient=20.0)
            cold = c.heating_minutes(33.5, 39.5, ambient=-5.0)
            assert cold > mild, f"{c.prediction_model} ignored the ambient override"

    def test_the_override_does_not_leak_into_the_live_model(self):
        """It prices one question. The coordinator's own ambient_temp is what every
        other reader sees, and a planning call must not move it."""
        c = self._coord()
        c.heating_minutes(33.5, 39.5, ambient=-30.0)
        assert c.ambient_temp == 15.0


class TestTheLiveEtaUsesTheRestOfTheRun:
    """The rolling version, and it is worth more than the schedule case rather than less.

    Mid-run at the pre-dawn minimum the instantaneous reading is the coldest hour of the
    night while every remaining hour is warmer. On an autumn profile that reads 40% long
    at 04:00 — nearly five hours — against 1% for the rolling mean. It is also exactly
    when someone looks at Ready at, and it is what makes an estimate sit still all night
    and then race forward after dawn.
    """

    def _rows(self, start, temps):
        from datetime import timedelta
        return [(start + timedelta(hours=i), t) for i, t in enumerate(temps)]

    def _coord(self, rows, ambient):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c._band_stats = {}
        c.heat_rate_buckets = [1.24, 1.095, 0.878]
        c.ambient_baseline = 18.41
        c.ambient_temp = ambient
        c.computed_heat_rate = 0.972
        c.prediction_bias = 1.0
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        c._last_data = {}
        c._newton_fallback_active = False
        c._forecast_rows = rows
        c.config_entry = type("E", (), {"options": {}})()
        return c

    def test_the_window_is_the_rest_of_the_run(self):
        """Same mechanism as the schedule, anchored at a derived finish instead of a
        given one — so the window is the remainder either way."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        c = self._coord(self._rows(now, [3.0] + [9.0] * 30), ambient=3.0)
        got = c.live_ambient_for(30.0, 39.5)
        assert got is not None
        mean, kind = got
        assert kind == "window"
        assert mean > 8.0, (
            "the run happens through the warming morning, not in the one cold hour "
            "it starts in")

    def test_it_beats_the_instant_where_the_instant_is_worst(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        c = self._coord(self._rows(now, [3.0] + [9.0] * 30), ambient=3.0)
        cold_instant = c.heating_minutes(30.0, 39.5)
        mean, _ = c.live_ambient_for(30.0, 39.5)
        rolling = c.heating_minutes(30.0, 39.5, ambient=mean)
        assert rolling < cold_instant, (
            "planning the whole run at the night minimum is pessimistic")

    def test_the_window_shrinks_as_the_run_proceeds(self):
        """So the mean converges on the near-term forecast of its own accord, rather
        than needing to be told to."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        rows = self._rows(now, [0.0] * 4 + [20.0] * 30)
        early = self._coord(rows, ambient=0.0).live_ambient_for(30.0, 39.5)
        late = self._coord(rows, ambient=0.0).live_ambient_for(39.0, 39.5)
        assert late[0] <= early[0] + 1e-9, (
            "a nearly-finished run must weigh the next hour, not the whole day")

    def test_the_source_is_recorded_on_the_shadow(self):
        """It changes what a row means, so it has to be in the history rather than
        inferred from a date. Rows from before this existed, or from a spell when the
        forecast was unavailable, were priced from a single instant."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        c = self._coord(self._rows(now, [9.0] * 30), ambient=3.0)
        c.scheduled_ready_at = None
        c.schedule_target_temp = None
        c._schedule_triggered = False
        c.newton_ready_at = c.newton_start_at = None
        c.newton_target_temp = c.newton_plan_temp = None
        c.newton_ambient_source = "now"
        c.scheduling_temp = lambda: 30.0
        c._update_newton_shadow(30.0, 39.5)
        assert c.newton_ambient_source == "forecast_window"
        c._forecast_rows = []
        c._update_newton_shadow(30.0, 39.5)
        assert c.newton_ambient_source == "now", "no forecast must say so, not pretend"
