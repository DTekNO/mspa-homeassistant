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
from custom_components.mspa import predictor


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

    def test_the_thermal_model_is_the_default(self):
        """Changed from buckets on 12.09.2026. The seam is unmoved: same entity ids,
        same meaning, only the arithmetic behind them differs."""
        c = self._coord()
        assert c.prediction_model == "thermal"
        # No frozen plan. Freezing existed because bucket rates were stale by
        # construction; with air as a term there is nothing stale to hold steady.
        assert c.uses_frozen_plan is False
        assert c.heating_minutes(24.0, 39.5) == pytest.approx(
            c.thermal_minutes(24.0, 39.5))

    def test_buckets_remain_selectable_and_keep_their_frozen_plan(self):
        c = self._coord("buckets")
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
        c._window_solar_sum = c._window_solar_n = 0
        c.heat_rate_buckets_norm = [None, None, None]
        c._bucket_base_value_norm = None
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
        c._window_solar_sum = c._window_solar_n = 0
        c.heat_rate_buckets_norm = [None, None, None]
        c._bucket_base_value_norm = None
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

    def test_a_covered_run_averages_the_whole_of_itself(self):
        """However long it is. Those are the hours the spa will actually be heating
        through, so two days and one night is the truth rather than a skewed sample."""
        from datetime import timedelta
        now = self._now()
        rows = self._rows(now, [10.0] * 48)
        _mean, n, kind = forecast_window_mean(rows, now + timedelta(hours=40), 40.0)
        assert kind == "window" and n == 40, f"{n} hours of a 40-hour run"

    def test_an_uncovered_run_falls_back_to_one_whole_cycle(self):
        """Past the forecast the window is a stand-in rather than the run, and then an
        unbalanced slice of the diurnal cycle biases the mean by up to two degrees on
        nothing but where it happened to fall. Twenty-four hours is the only span whose
        mean does not depend on its anchor."""
        from datetime import timedelta
        from custom_components.mspa.predictor import FORECAST_DIURNAL_H
        now = self._now()
        rows = self._rows(now, [10.0] * 30)
        _mean, n, kind = forecast_window_mean(rows, now + timedelta(hours=40), 40.0)
        assert kind == "partial"
        assert n <= FORECAST_DIURNAL_H + 1, f"{n} hours; want one cycle"

    def test_the_diurnal_fallback_removes_the_anchor_bias(self):
        """The measurement the number is chosen from. On a profile swinging 8 to 21 °C a
        24-hour mean reads the same wherever it starts; 30 hours swings 1.7 °C."""
        from datetime import timedelta
        now = self._now()
        day = [11, 10, 9, 9, 8, 8, 9, 11, 13, 15, 17, 19,
               20, 21, 21, 20, 19, 18, 16, 15, 14, 13, 12, 11]
        rows = self._rows(now, day * 3)              # 72 hours, 0..71
        # Uncovered because a 40-hour window reaches back before the forecast starts,
        # while the clipped 24 hours are all present. Each is a whole cycle at a
        # different anchor and they must agree.
        got = [forecast_window_mean(rows, now + timedelta(hours=e), 40.0)
               for e in (26, 30, 34)]
        assert all(k == "partial" and n == 24 for _m, n, k in got), got
        means = [m for m, _n, _k in got]
        assert max(means) - min(means) < 0.05, f"anchor still matters: {means}"

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

    def test_a_run_starting_now_is_not_called_partial(self):
        """met.no's first row is the next whole hour, so a window for a run starting now
        almost always begins a few minutes before the forecast does. Without a step of
        slack at the head, "partial" was stamped on nearly every in-flight estimate and
        stopped distinguishing anything."""
        from datetime import timedelta
        now = self._now()
        first_row = now + timedelta(minutes=7)          # the next whole hour
        rows = [(first_row + timedelta(hours=i), 10.0) for i in range(48)]
        _m, _n, kind = forecast_window_mean(rows, now + timedelta(hours=8.5), 8.5)
        assert kind == "window", "a 7-minute sliver at the head is not a partial forecast"

    def test_but_a_genuinely_short_forecast_still_is(self):
        """The slack is one period, not a licence."""
        from datetime import timedelta
        now = self._now()
        rows = [(now + timedelta(hours=i), 10.0) for i in range(48)]
        _m, _n, kind = forecast_window_mean(rows, now + timedelta(hours=8), 30.0)
        assert kind == "partial"

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
        # "partial" rather than "window": the run is long enough that its window starts
        # before the forecast does, so this is the clipped cycle rather than the run.
        assert mean == pytest.approx(-5.0) and kind in ("window", "partial")
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

class TestTheForecastIsHardenedAgainstWhateverArrives:
    """Home Assistant guarantees almost nothing about a forecast.

    `datetime` is the only Required key on the Forecast TypedDict; `temperature` is
    optional and explicitly nullable. `supported_features` may advertise hourly,
    twice-daily, daily, any combination, or none — a weather entity is not required to
    offer a forecast at all. met.no offered 24 hours of hourly data, then 48. So nothing
    may assume a resolution, a horizon, a field, or that a forecast exists.
    """

    def _rows(self, temps, *, step_h=1, start=None):
        from datetime import datetime, timedelta, timezone
        start = start or datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
        return [(start + timedelta(hours=i * step_h), t) for i, t in enumerate(temps)]

    def _end(self, hours):
        from datetime import datetime, timedelta, timezone
        return datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hours)

    def test_a_daily_forecast_still_answers(self):
        """Entries a day apart contain no sample inside a six-hour window, however good
        the forecast is. The nearest day beats this evening's thermometer for a run
        finishing tomorrow morning."""
        rows = self._rows([5.0, 12.0, 3.0], step_h=24)      # hours 0, 24, 48
        # A six-hour window ending at hour 20 spans [14, 20) and contains no entry.
        mean, n, kind = forecast_window_mean(rows, self._end(20), 6.0)
        assert kind == "nearest" and n == 1
        assert mean == pytest.approx(12.0), "the nearest day is hour 24, not hour 0"
        # And where a daily entry does fall inside, it is used as an ordinary sample.
        _m, _n, kind_in = forecast_window_mean(rows, self._end(30), 6.0)
        assert kind_in == "window"

    def test_junk_entries_are_dropped_not_averaged_in(self):
        rows = self._rows([5.0, 5.0, 5.0])
        rows += [(self._end(3), None), (self._end(4), "not a number"),
                 (None, 5.0), (self._end(5), float("nan"))]
        mean, n, _k = forecast_window_mean(rows, self._end(3), 3.0)
        assert n == 3 and mean == pytest.approx(5.0)

    def test_impossible_temperatures_are_refused(self):
        """A value outside the range of inhabited weather is a unit mix-up or a
        sentinel, not a forecast."""
        rows = self._rows([5.0, 999.0, -999.0, 5.0])
        mean, n, _k = forecast_window_mean(rows, self._end(4), 4.0)
        assert n == 2 and mean == pytest.approx(5.0)

    def test_unordered_input_is_sorted(self):
        rows = list(reversed(self._rows([0.0, 10.0, 20.0])))
        mean, n, kind = forecast_window_mean(rows, self._end(3), 3.0)
        assert kind == "window" and n == 3 and mean == pytest.approx(10.0)

    def test_a_window_far_past_the_forecast_is_declined(self):
        """Borrowing the last known hour is reasonable a day out and absurd a week out."""
        from custom_components.mspa.predictor import FORECAST_MAX_EXTRAPOLATION_H
        rows = self._rows([5.0] * 24)
        assert forecast_window_mean(
            rows, self._end(23 + FORECAST_MAX_EXTRAPOLATION_H - 1), 6.0) is not None
        assert forecast_window_mean(
            rows, self._end(23 + FORECAST_MAX_EXTRAPOLATION_H + 5), 6.0) is None

    def test_degenerate_arguments_do_not_raise(self):
        rows = self._rows([5.0, 5.0])
        for span in (0, -1, None, "six"):
            assert forecast_window_mean(rows, self._end(2), span) is None
        assert forecast_window_mean(None, self._end(2), 6.0) is None
        assert forecast_window_mean([], self._end(2), 6.0) is None


class TestTheForecastFetchIsHardened:
    """The other half: what comes back from the service, and which service to call."""

    def _coord(self, *, features=None, unit="°C", exists=True):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        attrs = {"temperature_unit": unit}
        if features is not None:
            attrs["supported_features"] = features
        state = type("S", (), {"attributes": attrs})()
        c.hass = type("H", (), {
            "states": type("St", (), {
                "get": staticmethod(lambda e: state if exists else None)})()})()
        return c

    def test_the_kinds_tried_follow_what_the_entity_advertises(self):
        assert self._coord(features=2)._supported_forecast_kinds("w.x") == ["hourly"]
        assert self._coord(features=1)._supported_forecast_kinds("w.x") == ["daily"]
        assert self._coord(features=3)._supported_forecast_kinds("w.x") == [
            "hourly", "daily"], "hourly first — it is what the maths wants"
        assert self._coord(features=7)._supported_forecast_kinds("w.x") == [
            "hourly", "twice_daily", "daily"]

    def test_no_advertised_forecast_tries_everything_rather_than_giving_up(self):
        """Some integrations under-declare, and a refused call costs one debug line."""
        assert self._coord(features=0)._supported_forecast_kinds("w.x") == [
            "hourly", "twice_daily", "daily"]
        assert self._coord(features=None)._supported_forecast_kinds("w.x") == [
            "hourly", "twice_daily", "daily"]
        assert self._coord(features="rubbish")._supported_forecast_kinds("w.x") == [
            "hourly", "twice_daily", "daily"]

    def test_a_missing_entity_yields_nothing_rather_than_raising(self):
        assert self._coord(exists=False)._supported_forecast_kinds("w.x") == []

    def test_fahrenheit_is_converted(self):
        """`_convert_forecast` in core converts to the *user's* display unit, not to
        Celsius. Treating °F as °C is not a small error — it is a spa that never heats."""
        c = self._coord(unit="°F")
        rows = c._forecast_rows_from(
            [{"datetime": "2099-01-01T00:00:00+00:00", "temperature": 32.0}], "w.x")
        assert rows[0][1] == pytest.approx(0.0)

    def test_a_daily_high_is_averaged_with_its_low(self):
        """A daily entry's `temperature` is the day's high, with the low in `templow`.
        Planning a night-time heat-up from the high is wrong in the expensive
        direction."""
        c = self._coord()
        rows = c._forecast_rows_from(
            [{"datetime": "2099-01-01T00:00:00+00:00",
              "temperature": 14.0, "templow": 4.0}], "w.x")
        assert rows[0][1] == pytest.approx(9.0)

    def test_entries_without_any_temperature_are_skipped(self):
        c = self._coord()
        rows = c._forecast_rows_from([
            {"datetime": "2099-01-01T00:00:00+00:00"},
            {"datetime": "2099-01-01T01:00:00+00:00", "temperature": None},
            {"temperature": 5.0},
            "not a dict",
            {"datetime": "nonsense", "temperature": 5.0},
            {"datetime": "2099-01-01T02:00:00+00:00", "temperature": 5.0},
        ], "w.x")
        assert len(rows) == 1 and rows[0][1] == pytest.approx(5.0)

    def test_a_forecast_entirely_in_the_past_is_discarded(self):
        """Not the same as a window past the end of a live forecast. This one is a stale
        response, and the coordinator is the only party that knows what time it is."""
        c = self._coord()
        assert c._forecast_rows_from(
            [{"datetime": "2001-01-01T00:00:00+00:00", "temperature": 5.0}], "w.x") == []

    def test_a_future_forecast_is_kept(self):
        """Paired with the test above so that one cannot pass vacuously. It did: the
        timestamps were mocks, every comparison was truthy, and everything looked
        discarded for the right-looking reason."""
        c = self._coord()
        rows = c._forecast_rows_from(
            [{"datetime": "2099-01-01T00:00:00+00:00", "temperature": 5.0}], "w.x")
        assert len(rows) == 1
        assert rows[0][0].year == 2099 and rows[0][0].tzinfo is not None

    def test_a_naive_timestamp_is_taken_as_utc(self):
        c = self._coord()
        rows = c._forecast_rows_from(
            [{"datetime": "2099-01-01T00:00:00", "temperature": 5.0}], "w.x")
        assert rows[0][0].tzinfo is not None

    def test_a_non_utc_timestamp_is_normalised(self):
        c = self._coord()
        rows = c._forecast_rows_from(
            [{"datetime": "2099-01-01T02:00:00+02:00", "temperature": 5.0}], "w.x")
        assert rows[0][0].hour == 0, "must be comparable with every other row"


class TestTheDegradedModeIsVisible:
    """Borrowed from Better Thermostat, which flags a missing outside-temperature sensor
    as a repair. What makes it worth having is that a degraded mode becomes visible
    rather than silently worse.

    Predictions go on working either way — the current reading, then the seasonal
    average — so this is a warning about accuracy and never an error about function.
    """

    def _coord(self, *, weather="weather.home", entity_state="sunny", raises=False):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        created, deleted = [], []
        state = (None if entity_state is None
                 else type("S", (), {"state": entity_state, "attributes": {}})())
        c = object.__new__(MSpaUpdateCoordinator)
        c._forecast_failed = False
        c._forecast_failures = 0
        c.config_entry = type("E", (), {
            "options": {"weather_entity": weather} if weather else {},
            "entry_id": "abc"})()
        c.hass = type("H", (), {
            "states": type("St", (), {"get": staticmethod(lambda e: state)})()})()
        c._created, c._deleted = created, deleted

        def fake(available, detail=""):
            if raises:
                raise RuntimeError("issue registry exploded")
            (deleted if available else created).append(detail)
        c._async_update_forecast_repair = fake
        return c

    def test_one_failure_is_not_reported(self):
        """A weather integration is briefly unavailable at every restart. A notice that
        appears on every reboot and clears a minute later is one people learn to
        ignore."""
        c = self._coord()
        c._note_forecast_failure("weather.home", "hiccup")
        assert c._created == []
        assert c._forecast_failed is True, "but it is logged straight away"

    def test_a_persistent_failure_is(self):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = self._coord()
        for _ in range(MSpaUpdateCoordinator._FORECAST_FAILURES_BEFORE_REPAIR):
            c._note_forecast_failure("weather.home", "still nothing")
        assert len(c._created) == 1

    def test_the_reason_distinguishes_the_ways_it_breaks(self):
        """A missing entity, an unavailable one and one that simply offers no forecast
        need different fixes, so the notice must not flatten them into one."""
        gone = self._coord(entity_state=None)
        for _ in range(3):
            gone._note_forecast_failure("weather.home", "x")
        assert "missing" in gone._created[0]

        down = self._coord(entity_state="unavailable")
        for _ in range(3):
            down._note_forecast_failure("weather.home", "x")
        assert "unavailable" in down._created[0]

        bare = self._coord()
        for _ in range(3):
            bare._note_forecast_failure("weather.home", "it offers no usable forecast")
        assert "no usable forecast" in bare._created[0]

    def test_recovery_clears_it(self):
        c = self._coord()
        for _ in range(3):
            c._note_forecast_failure("weather.home", "x")
        assert c._created and not c._deleted
        c._forecast_failures = 0
        c._async_update_forecast_repair(True)
        assert c._deleted, "the notice must not outlive the condition"

    def test_a_broken_repair_registry_does_not_take_out_the_poll(self):
        """The notice is the least important thing happening on this poll."""
        c = self._coord(raises=True)
        for _ in range(4):
            c._note_forecast_failure("weather.home", "x")   # must not raise

    def test_the_issue_is_not_offered_as_fixable(self):
        """The fix is in the weather integration or in this integration's options. A
        repair flow that could do neither would be worse than a plain explanation."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_forecast_repair)
        assert "is_fixable=False" in src
        assert "IssueSeverity.WARNING" in src, "a warning, not an error"

    def test_a_failure_retries_sooner_than_a_success_refreshes(self):
        """The two intervals answer different questions and were briefly the same one.
        The success interval exists to avoid asking a working API more often than its
        data changes; it has nothing to say about how soon to retry a broken one.

        While a failure consumed the full half hour, three consecutive failures — the
        point at which the user is told — took an hour and a half. Disabling a weather
        integration therefore produced no repair notice for most of the afternoon, which
        is how this was found.
        """
        import time
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator as K
        c = self._coord()
        c._forecast_next_at = None
        before = time.time()
        c._note_forecast_failure("weather.home", "down")
        wait = c._forecast_next_at - before
        assert wait == pytest.approx(K._FORECAST_RETRY_S, abs=2)
        assert K._FORECAST_RETRY_S < K._FORECAST_TTL_S

    def test_the_user_is_told_within_a_useful_time(self):
        """A repair nobody sees until an hour and a half later is one they conclude is
        broken. Three retries at the failure interval is the whole delay."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator as K
        worst_case_s = K._FORECAST_FAILURES_BEFORE_REPAIR * K._FORECAST_RETRY_S
        assert worst_case_s <= 20 * 60, f"{worst_case_s / 60:.0f} min is too long to wait"
        # But not so eager that a restart blip raises one: a weather integration that is
        # back within a couple of minutes never reaches the third attempt.
        assert K._FORECAST_RETRY_S >= 120

    def test_it_is_translated_rather_than_hardcoded_english(self):
        import json
        from pathlib import Path
        root = Path(__file__).parent.parent / "custom_components" / "mspa"
        for name in ("strings.json", "translations/en.json"):
            d = json.loads((root / name).read_text())
            issue = d["issues"]["weather_forecast_unavailable"]
            assert "{entity_id}" in issue["description"]
            assert "{detail}" in issue["description"]
            # It must say that nothing is broken, because nothing is.
            assert "carry on working" in issue["description"]


class TestTheAmbientFallsBackRatherThanStopping:
    """The chain below the forecast. Each rung fails differently and the rung under it
    is always better than nothing."""

    def _coord(self, *, now=None, baseline=None):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c.ambient_temp = now
        c.ambient_baseline = baseline
        return c

    def test_the_reading_is_preferred(self):
        assert self._coord(now=4.0, baseline=15.0).effective_ambient() == (4.0, "now")

    def test_the_seasonal_average_catches_it(self):
        assert self._coord(baseline=15.0).effective_ambient() == (15.0, "baseline")

    def test_nothing_at_all_says_so(self):
        assert self._coord().effective_ambient() == (None, "none")

    def test_the_baseline_rung_exists_for_the_physical_model(self):
        """The bucket correction works on the *deviation* from the baseline, so falling
        back to the baseline gives a factor of exactly 1.0 — the same as no reading.
        Newton's law needs an absolute air temperature, and with none it stops answering
        entirely: the shadow blanks and a spa running on it falls back to buckets. This
        is the difference between degraded and off."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = _traverses(n=40, noise=0.02)
        c.ambient_temp = None
        c.ambient_baseline = 12.0
        c.heat_rate_buckets = None
        assert c.newton_minutes(24.0, 39.5) is not None
        c.ambient_baseline = None
        assert c.newton_minutes(24.0, 39.5) is None


class TestNoReadingIsNotTheSameAsNoAnswer:
    """The ambient learning sensor reported unknown whenever the weather source was down,
    while the shadow sensors kept answering from the seasonal average. Disabling met.no
    to test the repair notice showed the two side by side, and the inconsistency was the
    sensor's rather than the shadow's.
    """

    def _sensor(self, *, ambient, baseline=18.41):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        from custom_components.mspa.sensor import MSpaAmbientLearningSensor
        c = object.__new__(MSpaUpdateCoordinator)
        c.ambient_temp = ambient
        c.ambient_baseline = baseline
        c._band_stats = {}
        c._band_observations = []
        c._prediction_history = []
        c.heat_rate_buckets = None
        c.config_entry = type("E", (), {"options": {"weather_entity": "weather.home"}})()
        s = object.__new__(MSpaAmbientLearningSensor)
        s.coordinator = c
        return s

    def test_no_reading_reports_the_factor_actually_in_effect(self):
        """Which is 1.0: without an ambient temperature the rate is handed back
        untouched, so no correction is being applied and the sensor can say so."""
        assert self._sensor(ambient=None).native_value == 1.0

    def test_a_reading_still_reports_the_real_factor(self):
        v = self._sensor(ambient=5.0).native_value
        assert v is not None and v < 1.0, "colder than baseline must slow the rate"

    def test_the_history_does_not_gain_a_hole(self):
        """A measurement statistic meant to be watched over weeks. A gap is
        indistinguishable from the spa having been switched off."""
        assert self._sensor(ambient=None).native_value is not None
        assert self._sensor(ambient=None, baseline=None).native_value is not None

    def test_the_source_says_whether_the_one_was_measured(self):
        """A bare 1.0 cannot distinguish "no reading" from "conditions sit on the
        baseline", and those are different things to see in a chart."""
        assert self._sensor(ambient=None).extra_state_attributes[
            "ambient_source"] == "baseline"
        assert self._sensor(ambient=None, baseline=None).extra_state_attributes[
            "ambient_source"] == "none"
        assert self._sensor(ambient=18.41).extra_state_attributes[
            "ambient_source"] == "now"


class TestTheStorageFileExplainsItself:
    """The whole retrospective is meant to be readable in .storage without Home Assistant
    running. A number with no account of how it was made is not evidence.

    The gap this closes: a session planned while the weather source was down looked
    identical in the file to one planned on a good forecast, and the two deserve very
    different weight when the model is finally judged.
    """

    def _saved(self):
        """The keys the coordinator writes, read out of the source rather than run,
        because reaching the save call needs a live Home Assistant."""
        import ast
        from pathlib import Path
        src = Path(__file__).parent.parent / "custom_components" / "mspa" / "coordinator.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "async_save"
                    and node.args and isinstance(node.args[0], ast.Dict)):
                return {k.value for k in node.args[0].keys
                        if isinstance(k, ast.Constant)}
        raise AssertionError("no async_save({...}) found")

    def test_the_nowcast_constant_is_persisted(self):
        """`lift` is the only thing the nowcast carries between runs, so it is the only
        thing whose loss would change what the live estimate says. `thermal_tau_h` goes
        with it because it is now the last completed run's own measurement rather than a
        cross-run average — see finalise_thermal_run."""
        saved = self._saved()
        for key in ("lift", "lift_n", "thermal_tau_h", "thermal_tau_n"):
            assert key in saved, key

    def test_the_retired_shadow_keys_are_gone(self):
        """The shadow sensors were how the physical model was watched while it was being
        evaluated. They were removed with the model they shadowed, and a store still
        writing their keys would invite a reader to trust a number nothing produces."""
        saved = self._saved()
        for key in ("newton_ready_at", "newton_start_at", "newton_fit",
                    "newton_implied_tub", "newton_ambient_source",
                    "prediction_bias_applied"):
            assert key not in saved, key

    def test_and_what_priced_them(self):
        saved = self._saved()
        for key in ("schedule_ambient", "schedule_ambient_kind",
                    "forecast_resolution", "forecast_hours"):
            assert key in saved, f"{key} — the file cannot explain itself without it"

    def test_the_history_carries_both_models_error(self):
        """The comparison the whole exercise exists for, in the file rather than only on
        a sensor."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_data)
        for key in ("estimated_minutes_newton", "newton_params",
                    "ambient_source", "error_minutes_newton",
                    "error_minutes_biased"):
            assert f'"{key}"' in src, key

    def test_prediction_history_is_persisted_whole(self):
        """It is the record; a sensor attribute is only a view of it."""
        assert "prediction_history" in self._saved()
        assert "active_prediction" in self._saved(), (
            "an in-flight session must survive a restart or its estimate is lost")


class TestBothModelsArePricedTheSameWay:
    """A comparison between two models is worth nothing until they are answering the same
    question, and neither is worth anything against what was displayed unless that was
    the same question too.

    The 2026-08-28 session is what exposed it. The shadow sensor said 744 minutes, priced
    with the forecast averaged across the run; the figure recorded and scored said 684,
    priced with the reading taken at the moment the session opened. The truth was 700.
    Two different quantities, one name, and an hour between them.
    """

    def test_the_opening_record_prices_everything_from_one_ambient(self):
        """Every estimate in the session record, both models, from the same reading."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_data)
        block = src[src.index('"estimated_minutes"'):src.index('"plan_rates"')]
        for key in ("estimated_minutes_seed_only", "estimated_minutes_learned",
                    "estimated_minutes_newton"):
            i = block.index(key)
            assert "_open_amb" in block[i:i + 400], f"{key} is priced from its own ambient"
        assert "_open_amb" in src[src.index('"plan_rates"'):
                                  src.index('"plan_rates"') + 400], (
            "the frozen curve must agree with the estimate it was promised alongside")

    def test_the_bucket_model_takes_the_override_too(self):
        """The same physics through a coarser model: a rate learned in other weather,
        adjusted for the weather expected across this run. Not a Newton-only idea."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c._band_observations = []
        c._band_stats = {}
        c.heat_rate_buckets = [1.24, 1.095, 0.878]
        c.ambient_baseline = 15.94
        c.ambient_temp = 17.0
        c.computed_heat_rate = 0.972
        c.prediction_bias = 1.0
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        # Pinned to buckets: this test is about the bucket path taking the override,
        # and the default is the thermal model since 12.09.2026.
        c.config_entry = type("E", (), {"options": {"prediction_model": "buckets"}})()
        mild = c._heating_minutes_variant(28.5, 39.5, use_fits=False, ambient=17.0)
        cold = c._heating_minutes_variant(28.5, 39.5, use_fits=False, ambient=6.0)
        assert cold > mild, "a cold night must price the bucket run longer too"
        assert c._compute_heating_minutes(28.5, 39.5, ambient=6.0) == pytest.approx(cold)

    def test_the_live_paths_already_did_this(self):
        """Recorded so nobody re-derives it: the scheduler and the Ready at path have
        priced with the forecast since it was added, and they reach both models through
        the one seam. Only the opening record was left behind."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        from custom_components.mspa import sensor as sensor_mod
        trigger = inspect.getsource(MSpaUpdateCoordinator._check_schedule_trigger)
        assert "schedule_ambient" in trigger and "ambient=" in trigger
        seg = inspect.getsource(sensor_mod._segmented_heating_minutes)
        assert "live_ambient_for" in seg and "ambient=" in seg


class TestTheBiasIsMeasuredNotApplied:
    """prediction_bias predates the per-band correction for outdoor temperature and was
    the catch-all for the same thing: conditions today differ from the conditions the
    rates were learned in. The ambient correction now does that explicitly, per band,
    with a mechanism, so the bias became a second blind correction for a cause already
    being corrected — and it cannot tell a systematic error from a strange session.

    Across the seven real sessions recorded by 30.08, mean absolute error was 44.2
    minutes raw and 60.4 with the bias applied.
    """

    def _coord(self, bias=0.9, history=None):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        c.prediction_bias = bias
        c._prediction_history = history or []
        c._band_observations = []
        c._band_stats = {}
        c.heat_rate_buckets = [1.24, 1.095, 0.878]
        c.ambient_baseline = 15.94
        c.ambient_temp = 16.0
        c.computed_heat_rate = 0.972
        c._session_scalar = 1.0
        c._session_fresh_buckets = frozenset()
        c.config_entry = type("E", (), {"options": {}})()
        return c

    def test_an_estimate_no_longer_carries_it(self):
        """The change itself. A bias of 0.9 shortened every estimate by a tenth; on the
        28.08 session that turned a 695-minute prediction into 626 against an actual of
        701."""
        assert self._coord(bias=0.9).heating_minutes(28.0, 39.5) == pytest.approx(
            self._coord(bias=1.0).heating_minutes(28.0, 39.5))

    def test_but_it_is_still_learned(self):
        """Kept so the decision rests on finished sessions rather than on an argument."""
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = self._coord(bias=1.0)
        c._apply_bias_sample(0.8, span_c=11.0)
        assert c.prediction_bias < 1.0, "the EMA must go on tracking"

    def test_the_store_scores_it_both_ways(self):
        """`bias_evaluation` is what makes "measured, not applied" mean something."""
        history = [
            {"actual_minutes": 700.9, "error_minutes": 5.8,
             "error_minutes_biased": 75.3, "error_minutes_newton": -16.0,
             "estimated_minutes_newton": 716.9},
            {"actual_minutes": 511.9, "error_minutes": -0.3,
             "error_minutes_biased": 14.5},
            {"actual_minutes": 3.4, "error_minutes": -679.9,     # cancelled plan
             "error_minutes_biased": -643.8},
        ]
        ev = self._coord(history=history).bias_evaluation()
        assert ev["all"]["sessions"] == 2, "a 3-minute session is not evidence"
        assert ev["all"]["mean_abs_error_min"] == pytest.approx(3.05, abs=0.06)
        assert ev["all"]["mean_abs_error_with_bias_min"] == pytest.approx(44.9)

    def test_the_instrumented_set_is_reported_separately(self):
        """Runs before the instrumentation was finished are not comparable, so the set
        that carries a physical-model estimate is scored on its own."""
        history = [
            {"actual_minutes": 700.9, "error_minutes": 5.8,
             "error_minutes_biased": 75.3, "error_minutes_newton": -16.0,
             "estimated_minutes_newton": 716.9},
            {"actual_minutes": 712.1, "error_minutes": -243.0,   # the 25.08 anomaly
             "error_minutes_biased": -204.3},
        ]
        ev = self._coord(history=history).bias_evaluation()
        assert ev["all"]["sessions"] == 2
        assert ev["instrumented"]["sessions"] == 1
        assert ev["instrumented"]["mean_abs_error_min"] == pytest.approx(5.8)
        assert ev["instrumented"]["mean_abs_error_newton_min"] == pytest.approx(16.0)

    def test_the_opening_eta_shows_what_is_predicted(self):
        """It read estimated_minutes_biased, which is now a counterfactual — displaying
        it would show a time the integration no longer stands behind."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator.session_opening_eta)
        assert 'pred["estimated_minutes"]' in src
        assert "estimated_minutes_biased" not in src.split('"""')[2]


class TestTheOneShotRecordsWhatItNeedsTo:
    """Two quantities that were inferred rather than measured on the session of 28.08,
    and between them accounted for fifty minutes of error that happened to cancel.

    The start temperature was inferred from the crossing two minutes later; the air was
    inferred from band means recorded for a different purpose. Neither inference is
    available in general, and heat-ups from cold are rare enough that a session which
    fails to record them is a season wasted.
    """

    def _coord(self, *, sensor="sensor.outside", state="12.0", unit="°C"):
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        c = object.__new__(MSpaUpdateCoordinator)
        st = (None if state is None else
              type("S", (), {"state": state,
                             "attributes": {"unit_of_measurement": unit}})())
        c.hass = type("H", (), {
            "states": type("St", (), {"get": staticmethod(lambda e: st)})()})()
        c.config_entry = type("E", (), {
            "options": {"outdoor_sensor": sensor} if sensor else {}})()
        return c

    def test_it_reads_a_local_thermometer(self):
        assert self._coord().read_outdoor_sensor() == pytest.approx(12.0)
        assert self._coord().outdoor_sensor == "sensor.outside"

    def test_fahrenheit_is_converted(self):
        assert self._coord(state="53.6", unit="°F").read_outdoor_sensor() == (
            pytest.approx(12.0))

    def test_a_bad_reading_is_absent_rather_than_wrong(self):
        """This is being recorded to judge a forecast against, so a contaminated mean is
        worse than a missing one."""
        for state in ("unavailable", "unknown", "", "n/a", "999", "-999"):
            assert self._coord(state=state).read_outdoor_sensor() is None
        assert self._coord(state=None).read_outdoor_sensor() is None
        assert self._coord(sensor=None).read_outdoor_sensor() is None

    def test_it_has_to_be_configured(self):
        """A local thermometer is an ordinary sensor entity with nothing marking it out
        from the twenty-odd others in a house, and guessing from the name picks the
        oven."""
        assert self._coord(sensor=None).outdoor_sensor is None

    def test_but_it_is_not_offered_to_users(self):
        """A development instrument, kept out of the options dialog on purpose.

        It drives nothing a user can see, so asking someone to pick an entity for it
        buys them nothing and commits us to supporting it from the day it ships. This
        guards the way back in: the option is easy to re-add by reflex when working on
        the surrounding code. Set it by editing the config entry instead — see the note
        on CONF_OUTDOOR_SENSOR in const.py.
        """
        from tests.test_config_flow_translations import _schema_keys_by_step
        for step, keys in _schema_keys_by_step().items():
            assert "outdoor_sensor" not in keys, f"{step} asks the user for it"
        from pathlib import Path
        root = Path(__file__).parent.parent / "custom_components" / "mspa"
        for name in ("strings.json", "translations/en.json"):
            assert "outdoor_sensor" not in (root / name).read_text(encoding="utf-8"), name

    def test_and_a_hidden_option_survives_the_options_dialog(self):
        """Submitting Options must not wipe what the dialog does not manage.

        async_create_entry replaces the options wholesale, so without this the thermometer
        would vanish the first time anyone opened Options and pressed Submit — silently,
        and only visible weeks later as a gap in the comparison it was collecting for.

        Read from source rather than imported: config_flow pulls in voluptuous and the HA
        selector helpers, which the stubs in conftest do not provide.
        """
        from pathlib import Path
        src = (Path(__file__).parent.parent / "custom_components" / "mspa"
               / "config_flow.py").read_text(encoding="utf-8")
        save = src[src.index("class OptionsFlowHandler"):]
        save = save[:save.index("data_schema = vol.Schema({")]
        assert "OPTION_KEYS" in save, "the save path no longer preserves hidden options"
        assert "async_create_entry(title=\"\", data=user_input)" not in save, (
            "saving the raw input again — a hidden option would be wiped")

    def test_the_managed_option_list_matches_the_form(self):
        """OPTION_KEYS decides what gets wiped, so drift silently deletes an option."""
        import ast
        from pathlib import Path
        from tests.test_config_flow_translations import (
            _conf_constants, _schema_keys_by_step)
        root = Path(__file__).parent.parent / "custom_components" / "mspa"
        tree = ast.parse((root / "config_flow.py").read_text(encoding="utf-8"))
        conf = _conf_constants()
        declared = None
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "OPTION_KEYS"
                            for t in node.targets)):
                declared = {
                    e.value if isinstance(e, ast.Constant) else conf[e.id]
                    for e in node.value.args[0].elts}
        assert declared is not None, "OPTION_KEYS has gone"
        form = _schema_keys_by_step()[("options", "init")]
        assert declared == form, (
            f"only in form: {form - declared}; only in OPTION_KEYS: {declared - form}")

    def test_the_session_record_carries_both_air_figures(self):
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_data)
        for key in ("forecast_air_c", "measured_air_at_start_c",
                    "measured_air_mean_c", "measured_air_samples",
                    "forecast_air_error_c"):
            assert f'"{key}"' in src, key
        # And absent rather than null without a thermometer: a record full of nulls
        # reads as a sensor that failed, an absent key as one never configured, and an
        # analysis has to tell those apart.
        assert "if _measured is not None:" in src
        assert "if self._session_air_n:" in src

    def test_and_the_start_temperature_both_ways(self):
        """The reading is the last threshold crossed, so the truth is up to half a band
        above it — always in the same direction, and worth 25 minutes on a long run."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        src = inspect.getsource(MSpaUpdateCoordinator._async_update_data)
        for key in ("start_temp", "start_temp_extrapolated",
                    "estimated_minutes_from_extrapolated"):
            assert f'"{key}"' in src, key
        assert '"error_minutes_from_extrapolated"' in src, (
            "recording the alternative estimate is no use unless it is scored")

    def test_nothing_yet_depends_on_the_measured_air(self):
        """Recorded, not adopted. Planning still runs on the forecast — changing what is
        being measured in the same week as measuring it would answer neither question."""
        import inspect
        from custom_components.mspa.coordinator import MSpaUpdateCoordinator
        for name in ("forecast_ambient_for", "live_ambient_for", "effective_ambient",
                     "newton_minutes"):
            src = inspect.getsource(getattr(MSpaUpdateCoordinator, name))
            assert "read_outdoor_sensor" not in src, f"{name} consults it already"


class TestRateNormalisation:
    """The transform that stops a bucket storing the weather it was learned under."""

    def test_round_trip_is_exact(self):
        k = 1.0 / 62.0
        for rate, air in ((1.15, 17.5), (0.88, 3.0), (1.30, -8.0)):
            normed = predictor.normalise_rate(rate, air, k)
            assert predictor.expand_rate(normed, air, k) == pytest.approx(rate, abs=1e-12)

    def test_at_the_reference_it_does_nothing(self):
        k = 1.0 / 62.0
        assert predictor.normalise_rate(
            0.9, predictor.AMBIENT_REF_C, k) == pytest.approx(0.9)

    def test_warmer_air_normalises_down(self):
        """A rate measured in the warm is a slower rate at the reference."""
        k = 1.0 / 62.0
        assert predictor.normalise_rate(1.15, 17.5, k) < 1.15
        assert predictor.normalise_rate(1.15, 2.0, k) > 1.15

    def test_additive_correction_matches_the_exact_newton_chord(self):
        """The bucket learns a chord, not a point rate — the shift must still be right.

        This is the assumption the whole design rests on: that a constant offset in air
        moves a *chord* by the same constant. If it were false the correction would need
        an integral per band instead of one addition.
        """
        tau, p_over_c = 60.0, 1.35

        def chord(t1, t2, air):
            asymptote = air + tau * p_over_c
            hours = tau * math.log((asymptote - t1) / (asymptote - t2))
            return (t2 - t1) / hours

        for lo, hi in ((20.0, 30.0), (30.0, 37.0), (37.0, 39.0)):
            for air in (-5.0, 0.0, 10.0, 20.0, 30.0):
                exact = chord(lo, hi, air) - chord(lo, hi, predictor.AMBIENT_REF_C)
                additive = (air - predictor.AMBIENT_REF_C) / tau
                assert exact == pytest.approx(additive, abs=1e-3)

    def test_none_in_none_out(self):
        assert predictor.normalise_rate(None, 10.0, 0.016) is None
        assert predictor.normalise_rate(1.0, None, 0.016) is None
        assert predictor.expand_rate(1.0, 10.0, None) is None


class TestBandAmbientK:
    """Which sensitivity gets used, and whether it says so honestly."""

    def test_no_fit_falls_back_to_the_cooling_prior(self):
        k, source = predictor.band_ambient_k(None)
        assert source == "prior"
        assert k == pytest.approx(predictor.BAND_K_PRIOR)

    def test_a_fit_over_too_narrow_a_range_is_not_used(self):
        """Thirty traverses all taken between 12 and 14 °C is noise wearing a number."""
        fit = {"n": 30, "slope": 0.02}
        _, source = predictor.band_ambient_k(fit, amb_range=3.0)
        assert source == "prior"

    def test_a_fit_with_range_and_evidence_is_used(self):
        k, source = predictor.band_ambient_k({"n": 10, "slope": 0.02}, amb_range=12.0)
        assert (k, source) == (0.02, "fitted")

    def test_an_implausible_slope_is_refused(self):
        """A sensitivity outside the clamp says the fit found something that isn't air."""
        for slope in (0.9, -0.02, 0.0):
            _, source = predictor.band_ambient_k({"n": 30, "slope": slope}, amb_range=20.0)
            assert source == "prior", slope

    def test_too_few_observations_even_with_range(self):
        _, source = predictor.band_ambient_k({"n": 2, "slope": 0.02}, amb_range=20.0)
        assert source == "prior"


class TestBucketShape:
    """The free test on the three learned rates."""

    def test_the_live_buckets_are_monotonic(self):
        shape = predictor.bucket_shape([1.187, 1.038, 0.885])
        assert shape["monotonic"] is True

    def test_the_documented_flat_pathology_is_caught(self):
        """1.03/0.99/1.01 is the real reading that sent a seeded fit to tau 512 h."""
        shape = predictor.bucket_shape([1.03, 0.99, 1.01])
        assert shape["monotonic"] is False

    def test_a_single_tau_leaves_no_residual(self):
        """Rates generated from one tau must come back collinear."""
        tau, p_over_c = 60.0, 1.4
        spans = ((20.0, 30.0), (30.0, 37.0), (37.0, 39.0))
        rates = [p_over_c - (((lo + hi) / 2.0) - predictor.AMBIENT_REF_C) / tau
                 for lo, hi in spans]
        shape = predictor.bucket_shape(rates)
        assert shape["collinearity_residual"] == pytest.approx(0.0, abs=1e-9)
        assert shape["implied_tau_h"][0] == pytest.approx(tau, abs=0.1)
        assert shape["implied_tau_h"][1] == pytest.approx(tau, abs=0.1)

    def test_the_live_buckets_are_not_on_one_tau(self):
        """Measured 04.09.2026: ~57 h cold-to-mid against ~29 h mid-to-hot."""
        shape = predictor.bucket_shape([1.187, 1.038, 0.885])
        cold_mid, mid_hot = shape["implied_tau_h"]
        assert cold_mid == pytest.approx(57.0, abs=1.0)
        assert mid_hot == pytest.approx(29.4, abs=1.0)
        assert abs(shape["collinearity_residual"]) > 0.01

    def test_declines_without_three_rates(self):
        assert predictor.bucket_shape([1.1, None, 0.9]) is None
        assert predictor.bucket_shape([]) is None
        assert predictor.bucket_shape(None) is None


class TestSolAir:
    """Sun enters as a lift on the outdoor temperature, and is pinned off for now."""

    def test_pinned_at_zero_it_changes_nothing(self):
        assert predictor.SOLAR_ALPHA_K == 0.0
        assert predictor.sol_air_temp(12.0, 1.0) == pytest.approx(12.0)

    def test_with_a_coefficient_it_lifts_the_air(self):
        assert predictor.sol_air_temp(12.0, 0.5, alpha=10.0) == pytest.approx(17.0)

    def test_no_sun_is_the_air_itself(self):
        assert predictor.sol_air_temp(12.0, 0.0, alpha=10.0) == pytest.approx(12.0)

    def test_no_air_is_no_answer(self):
        assert predictor.sol_air_temp(None, 1.0, alpha=10.0) is None
