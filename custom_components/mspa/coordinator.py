"""DataUpdateCoordinator for MSpa integration."""
import logging
from datetime import timedelta, datetime, timezone
from .mspa_api import MSpaApiClient
from .predictor import (
    HEAT_BUCKET_LEARN_MAX,
    HEAT_BUCKET_LEARN_MIN,
    HeatPredictor,
    ShadowPlan,
    in_learning_range,
    learning_anchor_zone,
    ambient_rate_factor,
    bucket_index,
    extrapolate_within_band,
    newton_fit,
    newton_free_fit,
    newton_heating_minutes,
    physical_constants,
)

from typing import Any, Dict
import asyncio

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.storage import Store
from homeassistant.const import UnitOfTemperature
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HEATER_POWER_HEAT,
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    IDLE_SCAN_INTERVAL,
    ACTIVE_SCAN_INTERVAL,
    RAPID_SCAN_INTERVAL,
    RAPID_POLL_TIMEOUT,
    EXTERNAL_CHANGE_INTERVAL,
    EXTERNAL_CHANGE_TIMEOUT,
    IDLE_STABLE_THRESHOLD,
    CONF_TRACK_TEMPERATURE_UNIT,
    CONF_RESTORE_STATE,
    CONF_ALWAYS_ENFORCE_UNIT,
    CONF_WEATHER_ENTITY,
    CONF_PREDICTION_MODEL,
    DEFAULT_PREDICTION_MODEL,
    PREDICTION_MODEL_NEWTON,
    CONF_SCHEDULE_TARGET_TEMP,
    DEFAULT_SCHEDULE_TARGET_TEMP,
    AMBIENT_BASELINE_ALPHA,
    AMBIENT_BASELINE_DEFAULT,
    ambient_rate_factor,
    BIAS_EMA_ALPHA,
    BIAS_ALPHA_MAX,
    BIAS_SPAN_FULL_C,
    BIAS_CLAMP_MIN,
    BIAS_CLAMP_MAX,
    BIAS_MIN_DELTA_C,
    BIAS_RATIO_MIN,
    BIAS_RATIO_MAX,
)

import time
from homeassistant.const import ATTR_STATE, ATTR_TEMPERATURE


_LOGGER = logging.getLogger(__name__)

# Rate-sampling constants (heating and cooling trackers).
# Bounds are intentionally generous: even a tiny spa at max power can't exceed
# ~5 °C/h, and below the minimum the signal is just sensor noise / drift.
_MIN_RATE_SAMPLE_HOURS = 3 / 60   # 3 minutes minimum between samples

# A scheduled ready time older than this is treated as abandoned rather than as a
# window that has just opened, so it is cleared without commanding the heater.
# Generous enough not to disturb the deliberate "fire as soon as the window opens"
# behaviour, which tests pin at 5 and 10 minutes late.
_SCHEDULE_STALE_AFTER = timedelta(hours=1)

# Soft start: the circulation pump must be running before the heater is commanded.
# The spa refuses to heat without flow, and the MSpa Link app never issues a bare
# heater-on — it starts the pump first.  Our climate entity, the heater switch, the
# set_heater service and the scheduler all could, so the ordering is enforced in
# set_feature_state, the one point they all pass through.
#
# The command ack confirms the cloud accepted it, not that water is moving, so a
# short settle follows.  The API throttle already spaces consecutive commands by
# 0.4 s; this makes the margin explicit and tunable.  Set to 0 to rely on the ack
# alone.
_PUMP_SETTLE_SECONDS = 1.5


_heat_bucket_index = bucket_index      # shared with the sensor via predictor.py
_MIN_HEAT_RATE = 0.05             # °C/h — below this is noise / flat
_MAX_HEAT_RATE = 3.0              # °C/h — above this is an outlier
_EMA_ALPHA = 0.25                 # smoothing factor (lower = slower to adapt)
# Span, in °C, at which a growing-window sample earns the full _EMA_ALPHA. Shorter
# spans earn proportionally less, because they measure proportionally less of the
# bucket they are updating.
_BUCKET_SPAN_FULL_C = 4.0

# A gap this large between water and setpoint defines a genuine heating
# session, as opposed to thermostat cycling near the setpoint (±0.5–1 °C).
# Used to start prediction tracking, to reset the session scalar, and to
# release the readiness latch when the user raises the setpoint.
_NEW_SESSION_DELTA = 2.0          # °C

# The settle guard: both must be met before a partial span is trusted.
#
# Written for a plan that recomputed the ETA at every 0.5 °C crossing, and measured
# against that over the four recorded sessions in analysis/settle_time.py — replanning
# converges to 0 min at the finish where holding carries its opening error all the way
# in (10 min mean, 35 on the worst session), while before the settle point the opposite
# is true, because a recompute from a partial span is badly wrong and the opening
# crossings measure band position rather than heating.
#
# It no longer gates the live ETA. ShadowPlan owns that, revises only at band edges
# where the elapsed time is fact, and is returned before this guard is consulted (see
# sensor.py). Two uses remain: nulling `progress_deviation` until a deviation measures
# heating rather than band position, and the fallback ETA for a session whose plan was
# cancelled, which does still recompute from the anchor.
#
# Note it is a one-shot gate — once both are past it stays true for the session — and
# that a session starting below the cold bucket passes it well before its first band
# edge at 20 °C, so it has already fallen away by the time anything can revise.
_PLAN_SETTLE_MINUTES = 90.0
_PLAN_SETTLE_DEGREES = 1.5

# How far the water may cool below the temperature at which it latched before
# "Ready" is withdrawn.  The latch advertises "still warm enough to use
# without waiting"; once the water has given up this much heat that claim is
# no longer credible, however the thermostat happens to be set.  Larger than
# _NEW_SESSION_DELTA so a lowered thermostat followed by ordinary cycling
# doesn't withdraw Ready prematurely.
_LATCH_COOL_OFF = 3.0             # °C

# How long the thermostat must hold a new value before the plan is rebuilt for it.
# The spa is polled once a second after a user action, so a dial turned from 38 to 39.5
# reports every value in between; acting on each would discard the session measurement
# over and over. A minute covers turning a dial, and is short enough that a deliberate
# change still appears while you are looking at the card.
_TARGET_SETTLE_S = 60

# Full-heat mode: the device reports heat_state 3 while actually heating
# (0 = off, 2 = preheat).  Rate sampling and prediction tracking both key off
# this so they start and stop on the same signal.
_HEAT_STATE_FULL = 3
# Observed values for the `heat_state` field.  4 is the resting state, confirmed by
# correlating a whole recorded cool-down against it; 2 precedes 3 on every heat-up.
_HEAT_STATE_NAMES = {0: "off", 1: "idle", 2: "preheat", 3: "heating", 4: "standby"}

# Reported water temperature is quantised to this step, so a reading is only ever
# accurate to half of it.
_TEMP_BAND_C = 0.5

# Maps coordinator _pending_changes keys (transformed names) to their raw API
# command dict keys.  Used to prune _pending_raw_command incrementally as each
# pending change is confirmed, so that a retry payload never re-sends fields
# that were already acknowledged by the spa.
_PENDING_TO_RAW_KEY: dict[str, str] = {
    "heater": "heater_state",
    "filter": "filter_state",
    "jet": "jet_state",
    "ozone": "ozone_state",
    "uvc": "uvc_state",
    "bubble": "bubble_state",
    "bubble_level": "bubble_level",
    "target_temperature": "temperature_setting",
    "temperature_unit": "temperature_unit",
}

# Status payload keys that are explicitly restructured/renamed in transformed_data.
# All other keys from the payload are passed through verbatim.
_STRUCTURED_STATUS_KEYS = frozenset({
    "water_temperature", "temperature_setting",
    "heater_state", "filter_state", "bubble_state",
    "jet_state", "ozone_state", "uvc_state",
    "bubble_level", "fault",
})

def _read_weather_entity(hass: HomeAssistant, entity_id: str | None) -> tuple[float | None, float | None]:
    """Read temperature (°C) and wind speed (m/s) from a HA weather entity.

    Returns (temp_celsius, wind_m_s).  Either value is None when unavailable.
    Normalises temperature and wind speed to SI units regardless of the HA
    unit system, so the Gaussian kernel uses consistent scales.
    """
    if not entity_id:
        return None, None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None, None

    attrs = state.attributes

    # --- Temperature → °C ---
    temp = attrs.get("temperature")
    if temp is not None:
        try:
            temp = float(temp)
            unit = attrs.get("temperature_unit", "°C")
            if unit in ("°F", "F"):
                temp = (temp - 32) * 5 / 9
        except (ValueError, TypeError):
            temp = None

    # --- Wind speed → m/s ---
    # Prefer wind_gust_speed over wind_speed: gusts disrupt the thermal
    # boundary layer around the cover more aggressively than steady wind,
    # making them the dominant driver of convective heat loss.
    # Falls back to wind_speed when gust data isn't provided by the integration.
    raw_wind = attrs.get("wind_gust_speed") or attrs.get("wind_speed")
    wind = None
    if raw_wind is not None:
        try:
            wind = float(raw_wind)
            unit = attrs.get("wind_speed_unit", "m/s")
            if unit in ("km/h", "kph"):
                wind = wind / 3.6
            elif unit in ("mph",):
                wind = wind * 0.44704
            elif unit in ("kn", "knot", "knots"):
                wind = wind * 0.514444
            # "m/s" needs no conversion
        except (ValueError, TypeError):
            wind = None

    return temp, wind



def _round_or_none(value, digits: int = 1):
    """Round where there is something to round. Estimates are legitimately absent."""
    return None if value is None else round(float(value), digits)


def _newton_params(fit):
    """The two parameters an estimate was made with, rounded for a stored record.

    Kept with the session rather than only on the sensor because the fit moves: a
    retrospective run months later would otherwise score every past session against
    today's parameters and learn nothing about how the model behaved at the time.
    """
    if not fit:
        return None
    return {
        "tau_h": round(fit["tau_h"], 2),
        "asymptote_lift_c": round(fit["asymptote_lift_c"], 2),
        "n": fit["n"],
    }


def _error_against(estimate, actual_minutes):
    """Minutes the estimate was out by: positive is late, negative early."""
    if estimate is None or actual_minutes is None:
        return None
    return round(float(actual_minutes) - float(estimate), 1)


class MSpaUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from MSpa Hot Tub."""

    def __init__(self, hass: HomeAssistant, config_entry: Dict[str, Any]) -> None:
        """Initialize."""
        # Obfuscate sensitive data for logging
        def obfuscate_value(key, value):
            if key == "password":
                return "***"
            elif key == "account_email" and value:
                parts = value.split("@")
                if len(parts) == 2 and parts[0]:
                    return f"{parts[0][:3]}***@{parts[1]}"
                return "***"
            return value

        safe_data = {k: obfuscate_value(k, v) for k, v in config_entry.data.items()}
        _LOGGER.debug(f"MSpaUpdateCoordinator initializing {safe_data}")
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.config = config_entry.data
        self.account_email = self.config["account_email"]
        self.password = self.config["password"]  # Already MD5 hashed
        self.region = self.config.get("region", "ROW")  # Default to ROW for safety

        self._last_data = {}
        self.api = MSpaApiClient(
            hass=hass,
            account_email=self.account_email,
            password=self.password,
            coordinator=self,
            region=self.region,
            device_id=self.config.get("device_id"),
        )
        self._rapid_poll_until = None  # Timestamp when to stop rapid polling
        self._external_change_until = None  # Timestamp when to stop external-change polling
        self._pending_changes = {}  # Track expected changes (transformed keys)
        self._pending_raw_command = {}  # Raw API payload for retrying unconfirmed commands
        self._command_retry_count = 0  # Number of retries attempted for current command
        self._last_heat_state = None  # Track heat state changes
        self._last_is_online = None  # Track power on/off transitions
        self._saved_state = {}  # Store state before power off for restoration
        self._last_snapshot = {}  # Store last known state for change detection
        self._last_state_change_time: float = 0.0  # monotonic time of last detected state change
        self._power_cycle_detected = False  # Flag to track if we just detected a power cycle

        # Computed heating rate tracking (°C / hour, exponential moving average).
        # Updated on every poll where the heater is in full-heat mode (heat_state == 3)
        # and at least _MIN_RATE_SAMPLE_HOURS have elapsed since the last valid sample.
        # Outliers (caused by adding hot/cold water, sensor noise etc.) are rejected.
        self.computed_heat_rate: float | None = None  # °C/h, None until enough data
        self._rate_last_temp: float | None = None     # °C at the window anchor
        self._rate_prev_temp: float | None = None     # °C at the previous poll
        # The bucket's value when the current growing window opened.  Nested samples
        # recompute from this rather than compounding on each other — see the bucket
        # write in _track_heating_rate.
        # Ambient accumulated across the current measuring window, so an observation can
        # carry the mean conditions of the traverse rather than a snapshot at its start.
        # A band can take five hours; the weather moves over that.
        self._window_amb_sum: float = 0.0
        self._window_amb_n: int = 0
        self._window_wind_sum: float = 0.0
        self._window_wind_n: int = 0
        # Set if anything happened during this window that makes the rate unmeasurable.
        self._window_disturbed: bool = False
        # Full-band traverses, each a clean (water span, mean ambient, realised rate)
        # observation. Kept to learn how much the weather moves each band's rate, which
        # is currently a hard-coded guess per band.
        self._band_observations: list[dict] = []
        # Running least-squares sums, one set per band, kept for the life of the spa.
        #
        # The observations above are bounded and will start forgetting after a few
        # months. These do not: five numbers per regressor recover the exact
        # least-squares fit over every traverse ever recorded, at constant storage. The
        # bounded list stays for offline analysis and for trying a different model later;
        # the fit the integration uses comes from here.
        #
        # Both regressors are accumulated. `amb` fits the form the code already applies,
        # rate = a + b x ambient, from which the existing sensitivity falls out. `delta`
        # fits rate against the water/air gap, which is the physical quantity — loss
        # scales with it — and is what a Newton's-law model would want. Keeping both
        # costs ten numbers per band and avoids discovering in the spring that the wrong
        # one was accumulated.
        self._band_stats: dict = {}
        self._bucket_base_value: float | None = None
        self._rate_last_time: float | None = None     # monotonic seconds at last sample
        # True while the rate anchor is phase-uncertain: the temperature reports
        # in 0.5 °C bands, so at heater-on the water sits at an unknown position
        # inside its band.  The FIRST crossing after heater-on measures that
        # random phase, not the heating rate (it reads ~2x fast on average), so
        # it re-anchors only; learning starts from the second crossing.
        self._rate_first_step: bool = False

        # Per-temperature-bucket heating EMAs for improved long-range accuracy.
        # Bucket 0: T < 30°C  (cold water, minimal thermal losses — fastest)
        # Bucket 1: 30 ≤ T < 37°C
        # Bucket 2: T ≥ 37°C  (near setpoint, highest losses — slowest)
        # Each bucket is updated only by observations made in that temperature
        # range, so predictions integrate the actual observed slow-down curve.
        self.heat_rate_buckets: list[float | None] = [None, None, None]

        # Session-level ambient condition scalar.
        # Reset to 1.0 at the start of each genuine heating session (delta > 2°C).
        # Updated from the *first bucket* to receive observations in each session
        # (whichever bucket that is — not hardcoded to bucket-0), comparing
        # observed rate vs the stored base rate for that bucket.  Applied to
        # bucket predictions for segments not yet observed this session so that
        # a cold/warm day is reflected immediately across the whole estimate.
        self._session_scalar: float = 1.0
        self._session_scalar_bucket: int | None = None  # which bucket drives the scalar this session
        self._session_fresh_buckets: set[int] = set()   # buckets that received data this session
        self._heat_was_active: bool = False  # previous-poll heater state

        # Computed passive cooling rate (°C/h, stored positive).
        # Sampled when the heater is off and water temperature is actually dropping.
        self.computed_cool_rate: float | None = None  # °C/h, None until enough data
        self._cool_last_temp: float | None = None     # °C at last accepted sample
        self._cool_first_step: bool = False           # phase-uncertain anchor guard
        self._cool_last_time: float | None = None     # monotonic seconds at last sample

        # Persist learned rates across reloads / HA restarts.
        device_id = self.config.get("device_id", "unknown")
        self._rates_store = Store(hass, version=1, key=f"{DOMAIN}_rates_{device_id}")
        self._rates_loaded = False  # load once on first poll

        # Anchor for time-to-target sensors.
        # Set whenever water_temperature or target_temperature changes so both
        # sensors can count down to a fixed future timestamp between temp steps.
        # UTC datetime full-heat mode last engaged, or None while not heating.  The
        # ETA measures elapsed heating from here when it is later than the anchor: the
        # anchor records when the temperature last *changed*, which during a
        # cool-down is a cooling transition and says nothing about heating progress.
        self.heating_since: datetime | None = None
        # UTC datetime the circulation pump last started, or None while it is off.
        # An anchor recorded before this describes stagnant water in the pump housing
        # rather than the tub, so it cannot be extrapolated from.
        self.circulating_since: datetime | None = None
        self._last_schedule_failure: str | None = None   # dedupes the failure log
        self.temp_anchor_time: datetime | None = None    # UTC datetime of last temp/target change
        self.temp_anchor_temp: float | None = None       # water_temp at that moment
        # Direction of the crossing that set the anchor: True warming, False cooling.
        # Taken from the observed reading change rather than from heater state, because
        # water keeps moving after the heater stops and can warm with it off.
        self.temp_anchor_rising: bool | None = None
        # The raw reading the anchor was derived from.  Change detection has to compare
        # readings with readings: temp_anchor_temp is a band *midpoint*, so comparing a
        # reading against it never matches and the anchor would re-fire every poll.
        self._anchor_prev_reading: float | None = None
        # Private rate curve for the displayed ready time, recalibrated against today's
        # actual heating and discarded when the session ends.  Never feeds the stored
        # buckets, which the scheduler plans from.
        self._shadow: ShadowPlan | None = None
        self.temp_anchor_target: float | None = None     # target_temp at that moment

        # Shared hysteresis flag for the time-to-target sensors.
        # Deactivates when within _NEAR_TARGET_DEACTIVATE of target;
        # reactivates only when _NEAR_TARGET_ACTIVATE away, preventing flicker.
        # Setpoint the plan is built for, and the one waiting to become it.  See the
        # settle in _async_update_data: the dial reports every value it passes through.
        self._settled_target: float | None = None
        self._pending_target: float | None = None
        self._pending_target_at = datetime.now(timezone.utc)
        # Target as seen by the readiness latch last poll, to tell a spa that heated to
        # its setpoint from a setpoint lowered onto the spa.
        self._latch_target: float | None = None

        self.near_target: bool = False
        # Latched True on the False→True transition of near_target (spa first reaches
        # temperature).  Cleared ONLY when the user sets a new scheduled_ready_at.
        # Never cleared automatically by temperature changes — the spa staying warm
        # between sessions must not re-arm predictions.
        self.ready_latched: bool = False
        # Water temperature when the latch was set (peak while at target).  The
        # cool-off release measures against this, so "Ready" is withdrawn once
        # the spa is no longer as warm as it was when it arrived.
        self.ready_latched_temp: float | None = None
        self.scheduled_ready_at: datetime | None = None  # set by MSpaScheduledReadyAt entity
        # Target temperature the scheduler should heat to.  Exposed as a number entity
        # so the user can adjust it from the device panel without entering options.
        self.schedule_target_temp: float = float(
            config_entry.options.get(CONF_SCHEDULE_TARGET_TEMP, DEFAULT_SCHEDULE_TARGET_TEMP)
        )
        # True once the autonomous heat-start command has been sent for the current
        # schedule.  Prevents re-triggering on every poll.  Reset when schedule clears.
        self._schedule_triggered: bool = False
        # Last computed autonomous start time, tracked only to log meaningful shifts
        # in the planned start as outdoor conditions change while waiting.
        self._last_computed_start_at: datetime | None = None
        # The physical model's shadow of the two things the bucket model decides: when
        # the water will be ready, and when a scheduled heat-up has to start. Computed
        # every poll, used for nothing, and exposed as sensor *states* rather than
        # attributes so the recorder keeps their history — attribute history cannot be
        # charted in the UI, and the whole point of these is to be reviewed after a run.
        self._newton_fallback_active = False
        self.newton_ready_at: datetime | None = None
        self.newton_start_at: datetime | None = None

        # Current ambient conditions read from optional weather sensors.
        # None until the first successful sensor read.
        self.ambient_temp: float | None = None
        self.ambient_wind: float | None = None

        # Learned baseline outdoor temperature under which the heating rates were
        # observed.  Slow EMA updated on each accepted heat-rate sample.  Used by
        # the ambient correction to know what "normal" outdoor conditions look
        # like for this spa, so colder-than-baseline nights slow the estimate.
        self.ambient_baseline: float | None = None

        # Prediction accuracy tracker.  Records the initial estimate at the
        # start of a big heating session and compares it to the actual elapsed
        # time when the target is reached.  Results are logged at INFO level
        # (grep for "PREDICTION_RESULT") and the last 10 are persisted in the
        # rates store under the key "prediction_history".
        self._prediction: dict | None = None   # active prediction (see _start_prediction)
        self._prediction_history: list[dict] = []  # last 10 completed predictions

        # Historical bias correction for heating predictions.
        # Derived from average error_percent in prediction_history.
        # A value > 1.0 means predictions have been too optimistic (actual > estimated).
        self.prediction_bias: float = 1.0

        # Extended device detail fetched once on init from /api/device/detail/
        self.device_detail: dict = {}

    @staticmethod
    def _plan_abandoned(prediction: dict | None, new_target) -> bool:
        """True when the setpoint has left the target this plan was made for.

        Extracted so a test can ask the question directly. `_update_near_target` was
        pulled out of `_async_update_data` for the same reason and it was the right
        call: the hand-written copy in the tests drifted from the real block and three
        tests went on passing against a rule the coordinator no longer applied.

        Unknowns answer "not abandoned", so a missing reading can never discard a
        session's measurement — the timing guard on the caller still applies.
        """
        if prediction is None:
            return False
        planned = prediction.get("target_temp")
        if planned is None or new_target is None:
            return False
        try:
            return abs(float(new_target) - float(planned)) >= _TEMP_BAND_C
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _bias_ratio(record: dict) -> float | None:
        """actual/estimated for one session, or None if unusable for the bias.

        The ratio is taken against the *raw* segmented estimate, not the biased
        one, so the bias converges on the rate model's true error rather than
        chasing its own previous output.
        """
        est    = record.get("estimated_minutes") or 0
        actual = record.get("actual_minutes") or 0
        start  = record.get("start_temp") or 0
        target = record.get("target_temp") or 0

        if (target - start) < BIAS_MIN_DELTA_C:
            return None
        if est <= 0 or actual <= 0:
            return None
        ratio = actual / est
        if not (BIAS_RATIO_MIN <= ratio <= BIAS_RATIO_MAX):
            return None
        return ratio

    def _apply_bias_sample(self, ratio: float, span_c: float | None = None) -> None:
        """Fold one completed session's accuracy ratio into prediction_bias.

        An incremental EMA, so the bias is monotone with respect to evidence: a
        ratio below the current bias always pulls it down and vice versa.  The
        previous implementation recomputed a weather-weighted mean over the last
        10 sessions on every call, which could move the bias *up* after a session
        that should have lowered it — and changed it on restart with no new data.

        **The step is weighted by how much the session actually measured.** The
        ratio's precision is set by the ±0.25 °C quantisation at each end of the
        span, so a 0.5 °C top-up determines it to about ±50% while a full cold
        start determines it to a few percent. A fixed step treats those as equal
        evidence, which is why a bias of 0.971 survived three consecutive sessions
        whose raw estimates were accurate to +2, −2 and −0 minutes: it was closing
        only 30% of the gap each time and costing 60, 25 and 14 minutes on the way.

        A full-span session now corrects it almost completely in one run, which is
        the point — the bias is a residual on a rate model that currently predicts
        to better than 0.1%, so a precisely measured residual should be believed.
        Extreme ratios are still bounded by BIAS_CLAMP_MIN/MAX rather than by the
        step size, so weighting up does not widen the worst case.
        """
        alpha = BIAS_EMA_ALPHA
        if span_c:
            alpha = BIAS_ALPHA_MAX * min(1.0, abs(span_c) / BIAS_SPAN_FULL_C)
            alpha = max(alpha, BIAS_EMA_ALPHA)      # never slower than before
        updated = self.prediction_bias + alpha * (ratio - self.prediction_bias)
        self.prediction_bias = max(BIAS_CLAMP_MIN, min(BIAS_CLAMP_MAX, updated))

    def _seed_prediction_bias_from_history(self) -> None:
        """Rebuild the bias by replaying stored history through the EMA.

        Used only on the upgrade path, when persisted history exists but no
        persisted bias does.  Replaying in chronological order preserves the
        accumulated learning while applying the current algorithm to it.
        """
        self.prediction_bias = 1.0
        n = 0
        for record in self._prediction_history:
            ratio = self._bias_ratio(record)
            if ratio is not None:
                self._apply_bias_sample(ratio)
                n += 1
        _LOGGER.debug(
            "Prediction bias seeded from %d historical sessions → %.3f",
            n, self.prediction_bias,
        )

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via direct function call."""
        try:
            # Use cached status if available
            if self.api._last_status is not None:
                status_data = self.api._last_status
                self.api._last_status = None  # Clear after use
            else:
                status_data = await self.api.get_hot_tub_status()

            fault_value = status_data.get("fault", "")
            transformed_data = {
                "water_temperature": float(status_data.get("water_temperature", 0))/2,
                "target_temperature": float(status_data.get("temperature_setting", 0))/2,
                "heater": "on" if status_data.get("heater_state", 0) else "off",
                "filter": "on" if status_data.get("filter_state", 0) else "off",
                "bubble": "on" if status_data.get("bubble_state", 0) else "off",
                "jet": "on" if status_data.get("jet_state", 0) else "off",
                "ozone": "on" if status_data.get("ozone_state", 0) else "off",
                "uvc": "on" if status_data.get("uvc_state", 0) else "off",
                "bubble_level": status_data.get("bubble_level", 1),
                "fault": fault_value if fault_value else "OK",
            }

            # Pass all remaining payload keys through verbatim so new/changed
            # firmware keys appear immediately as diagnostic sensors.
            for key, value in status_data.items():
                if key not in _STRUCTURED_STATUS_KEYS:
                    transformed_data[key] = value

            self._last_data = transformed_data
            _LOGGER.debug("Fetched MSpa transformed data: %s", transformed_data)

            # Read optional weather entity for ambient-condition bias.
            self.ambient_temp, self.ambient_wind = _read_weather_entity(
                self.hass, self.config_entry.options.get(CONF_WEATHER_ENTITY)
            )

            new_temp   = transformed_data.get("water_temperature")
            new_target = transformed_data.get("target_temperature")
            self._update_temp_anchor(new_temp, new_target)

            # --- Prediction accuracy tracking ---
            # Start tracking when a big heating session begins (delta > 2°C).
            # Only start after rates have been loaded from storage to avoid using
            # the unreliable device_heat_perhour fallback on the first poll.
            # Gated on full-heat mode, the same signal the cancellation below
            # uses.  Without it, a setpoint change during rapid polling created
            # and immediately cancelled a prediction on every 1 s poll, because
            # the device drops out of heat_state 3 while it recalculates.
            if (self._rates_loaded
                    and new_temp is not None and new_target is not None
                    and new_target > new_temp
                    and (new_target - new_temp) > _NEW_SESSION_DELTA
                    and transformed_data.get("heat_state") == _HEAT_STATE_FULL
                    and self._prediction is None):
                # Lazy import to reuse the sensor's segmented calculation.
                est_minutes = self._compute_heating_minutes(new_temp, new_target)
                if est_minutes is not None:
                    # Store the raw estimate (without bias) for history tracking.
                    # The bias is derived from raw vs actual, so storing the biased
                    # value would create a feedback loop that erases the correction.
                    raw_minutes = est_minutes / self.prediction_bias if self.prediction_bias else est_minutes
                    self._prediction = {
                        "start_time": datetime.now(timezone.utc).isoformat(),
                        "start_temp": new_temp,
                        "target_temp": new_target,
                        "estimated_minutes": round(raw_minutes, 1),
                        "estimated_minutes_biased": round(est_minutes, 1),
                        # The same session priced both ways, so the learned weather
                        # response can be scored against the seed on real finishes.
                        "estimated_minutes_seed_only": _round_or_none(
                            self._heating_minutes_variant(
                                new_temp, new_target, use_fits=False)),
                        "estimated_minutes_learned": _round_or_none(
                            self._heating_minutes_variant(
                                new_temp, new_target, use_fits=True)),
                        # And the same session priced by the physical model, from the
                        # fit as it stands before this session contributes anything to
                        # it. None until enough traverses exist to fit, and None again
                        # whenever the model says the target is out of reach — both are
                        # findings, so neither is filled in with a substitute.
                        "estimated_minutes_newton": _round_or_none(
                            self.newton_minutes(new_temp, new_target)),
                        # The parameters that estimate was made with, so a retrospective
                        # can tell a model that was wrong from one that had not yet
                        # learned anything.
                        "newton_params": _newton_params(self.newton_fit()),
                        "weather_entity": self.weather_entity,
                        "prediction_bias": round(self.prediction_bias, 3),
                        "session_scalar": self._session_scalar,
                        "ambient_temp": self.ambient_temp,
                        "ambient_wind": self.ambient_wind,
                        "ambient_baseline": self.ambient_baseline,
                        # Hot-bucket (near-setpoint) ambient correction at start —
                        # the dominant factor; the others are derivable from
                        # ambient_temp/baseline via ambient_rate_factor().
                        "ambient_factor_hot": round(
                            ambient_rate_factor(2, self.ambient_temp, self.ambient_baseline), 3
                        ),
                        # The rate curve as it stood when the session opened, with the
                        # ambient correction already folded in.  The live ETA integrates
                        # *this* for the duration, so mid-session learning cannot move
                        # an estimate that is already committed: rates learned today
                        # take effect on the next session, not this one.
                        "plan_rates": [
                            (b * ambient_rate_factor(i, self.ambient_temp,
                                                     self.ambient_baseline))
                            if b else None
                            for i, b in enumerate(self.heat_rate_buckets)
                        ],
                    }
                    self._shadow = ShadowPlan(
                        base_rates=self._prediction["plan_rates"],
                        start_temp=new_temp,
                        target=new_target,
                        opening_eta=(datetime.now(timezone.utc)
                                     + timedelta(minutes=est_minutes)),
                        # So the final half degree has something to measure from when
                        # the session starts inside the last band and never crosses an
                        # edge. See ShadowPlan.crossing.
                        start_time=datetime.now(timezone.utc),
                    )
                    buckets_str = "/".join(
                        f"{b:.2f}" if b is not None else "-"
                        for b in self.heat_rate_buckets
                    )
                    _LOGGER.info(
                        "PREDICTION_START: %.1f°C → %.1f°C, raw=%.0f min, biased=%.0f min"
                        " (bias=%.3f, scalar=%.3f, buckets=%s, ambient=%.1f°C %.1f m/s)",
                        new_temp, new_target, raw_minutes, est_minutes,
                        self.prediction_bias, self._session_scalar, buckets_str,
                        self.ambient_temp or 0.0, self.ambient_wind or 0.0,
                    )

            # Finish tracking when near_target is newly reached.
            #
            # near_target is updated later in this same poll, so the value read
            # here is the PREVIOUS poll's.  That matters when the setpoint has
            # just been raised: a prediction created moments ago would be
            # "completed" against a stale near_target from before the change,
            # recording a zero-duration session (observed: 31.5 → 35.0 °C
            # started and finished in the same millisecond, actual 0 min,
            # error -5910280273.8%).  A real session cannot complete inside the
            # minimum sample window, so require at least that much elapsed
            # before a completion is believed.
            # A session that was abandoned did not finish, and must not be recorded
            # as though it had.
            #
            # near_target is measured against whatever the setpoint is *now*, so
            # stopping a heat-up by dropping the thermostat onto the water satisfies it
            # without a degree of progress. Observed 2026-08-20: a run aborted at 30.0 °C
            # by moving the setpoint from 39.5 to 20 was recorded as complete, giving
            # "estimated 669 min, actual 42 min | error -1505.3%" — a plan for a span the
            # spa covered less than a seventh of.
            #
            # The settle timer already cancels a plan whose target has moved, but it waits
            # a minute to survive a dial sweep, and the completion check runs first: the
            # abort is recorded a second after it happens and the cancellation arrives
            # fifty-nine seconds too late. So completion asks its own question, which is
            # not "is the water near the setpoint" but "is this still the setpoint the
            # plan was made for".
            #
            # The bias itself was never at risk — _bias_ratio rejects a ratio of 0.06
            # against BIAS_RATIO_MIN — but the record reaches _prediction_history and the
            # diagnostics dump, and a -1505% line in the log is an invitation to diagnose
            # a model that is working.
            abandoned = self._plan_abandoned(self._prediction, new_target)
            if self.near_target and abandoned:
                _LOGGER.debug(
                    "Prediction not recorded: planned for %s°C, setpoint is now %s°C "
                    "— the session was abandoned, not completed",
                    (self._prediction or {}).get("target_temp"), new_target,
                )
            if (self.near_target and self._prediction is not None
                    and not abandoned
                    and (datetime.now(timezone.utc)
                         - datetime.fromisoformat(self._prediction["start_time"])
                         ).total_seconds() / 3600 >= _MIN_RATE_SAMPLE_HOURS):
                start_iso = self._prediction["start_time"]
                start_dt = datetime.fromisoformat(start_iso)
                actual_minutes = (datetime.now(timezone.utc) - start_dt).total_seconds() / 60
                est = self._prediction["estimated_minutes"]
                error_minutes = actual_minutes - est
                # Guard the ratio too: a sub-minute duration would produce a
                # meaningless percentage even if it got this far.
                error_pct = (error_minutes / actual_minutes * 100) if actual_minutes >= 1 else 0
                est_biased = self._prediction.get("estimated_minutes_biased", est)
                error_minutes_biased = actual_minutes - est_biased
                result = {
                    **self._prediction,
                    "actual_minutes": round(actual_minutes, 1),
                    "error_minutes": round(error_minutes, 1),
                    "error_percent": round(error_pct, 1),
                    "error_minutes_biased": round(error_minutes_biased, 1),
                    "error_minutes_seed_only": _error_against(
                        self._prediction.get("estimated_minutes_seed_only"),
                        actual_minutes),
                    "error_minutes_learned": _error_against(
                        self._prediction.get("estimated_minutes_learned"),
                        actual_minutes),
                    "error_minutes_newton": _error_against(
                        self._prediction.get("estimated_minutes_newton"),
                        actual_minutes),
                    "end_time": datetime.now(timezone.utc).isoformat(),
                }
                self._prediction_history.append(result)
                self._prediction_history = self._prediction_history[-10:]  # keep last 10
                self._prediction = None
                self._shadow = None
                ratio = self._bias_ratio(result)
                if ratio is not None:
                    self._apply_bias_sample(
                        ratio,
                        span_c=(result.get("target_temp", 0)
                                - result.get("start_temp", 0)),
                    )
                _LOGGER.info(
                    "PREDICTION_RESULT: %.1f°C → %.1f°C | estimated %.0f min, actual %.0f min | error %+.0f min (%+.1f%%) | bias=%.3f",
                    result["start_temp"], result["target_temp"],
                    est, actual_minutes, error_minutes, error_pct, self.prediction_bias,
                )
            # Clear prediction if target changed mid-session — but only once the new
            # target has stopped moving.
            #
            # Cancelling throws away the ShadowPlan with everything it has measured so
            # far, and the spa is polled once a second after a user action, so turning
            # the dial from 38 to 39.5 reports every value on the way. Each one would
            # discard the session's accumulated measurement and start again. Waiting for
            # the setpoint to settle costs a minute of ETA and saves the measurement.
            if new_target is not None and new_target != self._settled_target:
                if new_target != self._pending_target:
                    self._pending_target = new_target
                    self._pending_target_at = datetime.now(timezone.utc)
                elif (datetime.now(timezone.utc) - self._pending_target_at).total_seconds() \
                        >= _TARGET_SETTLE_S:
                    _LOGGER.debug("Target settled at %.1f", new_target)
                    self._settled_target = new_target
                    self._pending_target = None
                    if (self._prediction is not None
                            and new_target != self._prediction.get("target_temp")):
                        _LOGGER.debug(
                            "Prediction cancelled — target changed from %.1f to %.1f",
                            self._prediction.get("target_temp", 0), new_target)
                        self._prediction = None
                        self._shadow = None

            self._update_near_target(new_temp, new_target)
            # else: no temp data — leave flags unchanged

            # Load persisted rates on the very first poll after startup/reload.
            if not self._rates_loaded:
                self._rates_loaded = True
                stored = await self._rates_store.async_load()
                if stored:
                    self.computed_heat_rate = stored.get("heat_rate")
                    self.computed_cool_rate = stored.get("cool_rate")
                    self.ambient_baseline = stored.get("ambient_baseline")
                    # Restore the temperature anchor so a restart mid-step doesn't
                    # discard the elapsed heating time and jump the ETA forward.
                    # A fresh "now" anchor was already set earlier this poll; only
                    # override it back to the persisted time when the water temp
                    # and target still match — otherwise the step advanced while
                    # HA was down and the fresh anchor is the correct one.
                    # Compare the stored *reading*, not the stored anchor: the anchor
                    # is a band midpoint and never equals a reading, so comparing it
                    # meant the persisted anchor was never actually restored.
                    anchor_iso = stored.get("temp_anchor_time")
                    stored_reading = stored.get("anchor_prev_reading",
                                                stored.get("temp_anchor_temp"))
                    if (anchor_iso is not None
                            and stored_reading == new_temp
                            and stored.get("temp_anchor_target") == new_target):
                        try:
                            self.temp_anchor_time   = datetime.fromisoformat(anchor_iso)
                            # Keep the persisted midpoint; overwriting it with the raw
                            # reading threw away the half-step correction it exists for.
                            self.temp_anchor_temp   = stored.get(
                                "temp_anchor_temp", new_temp)
                            self._anchor_prev_reading = new_temp
                            self.temp_anchor_rising = stored.get("temp_anchor_rising")
                            since = stored.get("heating_since")
                            if since:
                                try:
                                    self.heating_since = datetime.fromisoformat(since)
                                except (TypeError, ValueError):
                                    self.heating_since = None
                            self.temp_anchor_target = new_target
                            _LOGGER.debug(
                                "Restored temp anchor: %.1f°C → %.1f°C at %s",
                                new_temp or 0.0, new_target or 0.0, anchor_iso,
                            )
                        except (ValueError, TypeError):
                            pass
                    stored_buckets = stored.get("heat_rate_buckets")
                    self._prediction_history = stored.get("prediction_history", [])
                    self._band_observations = stored.get("band_observations", [])
                    self._band_stats = stored.get("band_stats", {})
                    # Restore the bias as a stored value — never recompute it here.
                    # Recomputing on load made it depend on the weather at startup,
                    # so a restart alone could change every subsequent prediction.
                    stored_bias = stored.get("prediction_bias")
                    if stored_bias is not None:
                        self.prediction_bias = max(
                            BIAS_CLAMP_MIN, min(BIAS_CLAMP_MAX, float(stored_bias))
                        )
                    else:
                        self._seed_prediction_bias_from_history()
                    # Restore in-progress prediction so a restart mid-heatup doesn't
                    # drop the session from the learning history.
                    self._prediction = stored.get("active_prediction")
                    # Restore the schedule trigger state so a restart during a
                    # scheduled heat-up resumes as "Heating" rather than
                    # re-planning a start for a heater that is already running.
                    # If the schedule turns out to be gone or expired, the
                    # datetime entity's restore path clears this again.
                    self._schedule_triggered = bool(
                        stored.get("schedule_triggered", False)
                    )
                    if self._prediction:
                        _LOGGER.info(
                            "Restored in-progress prediction: %.1f°C → %.1f°C (started %s)",
                            self._prediction.get("start_temp", 0),
                            self._prediction.get("target_temp", 0),
                            self._prediction.get("start_time", "unknown"),
                        )
                    if isinstance(stored_buckets, list) and len(stored_buckets) == 3:
                        save_ts = stored.get("bucket_save_ts")
                        if save_ts is not None:
                            days_elapsed = (time.time() - save_ts) / 86400
                            # Decay each bucket toward the global flat EMA over time so
                            # stale seasonal rates don't anchor predictions indefinitely.
                            # With a weather entity, ambient_rate_factor explains some
                            # of the seasonal variation directly, so the stored rates
                            # stay useful for longer and can decay more slowly.
                            # Without weather: 0.98/day → ~75% weight at 14 days.
                            # With weather:    0.994/day → ~92% weight at 14 days.
                            has_weather = bool(
                                self.config_entry.options.get(CONF_WEATHER_ENTITY)
                            )
                            decay_rate = 0.994 if has_weather else 0.98
                            decay = max(0.4, decay_rate ** days_elapsed)
                            global_rate = self.computed_heat_rate
                            self.heat_rate_buckets = [
                                (float(b) * decay + global_rate * (1 - decay))
                                if b is not None and global_rate is not None
                                else (float(b) * decay if b is not None else None)
                                for b in stored_buckets
                            ]
                            _LOGGER.debug(
                                "Loaded buckets: %.1f days old, decay=%.3f (weather=%s) → %s",
                                days_elapsed, decay, has_weather, self.heat_rate_buckets,
                            )
                        else:
                            self.heat_rate_buckets = [
                                float(b) if b is not None else None for b in stored_buckets
                            ]
                    _LOGGER.debug(
                        "Restored rates from storage: heat=%.3f cool=%.3f",
                        self.computed_heat_rate or 0,
                        self.computed_cool_rate or 0,
                    )

            # --- Observed heating rate tracking (see _track_heating_rate) ---
            curr_temp = transformed_data.get("water_temperature")
            heat_state = transformed_data.get("heat_state")
            now_mono = time.monotonic()

            # Detect new heating session.  Only reset the session scalar when
            # the heater newly engages AND delta-to-target is large (> 2°C).
            # This avoids false resets from thermostat cycling near setpoint.
            if self.circulating:
                if self.circulating_since is None:
                    self.circulating_since = datetime.now(timezone.utc)
            else:
                self.circulating_since = None

            heater_now_active = (heat_state == _HEAT_STATE_FULL)
            if heater_now_active and self.heating_since is None:
                self.heating_since = datetime.now(timezone.utc)
            elif not heater_now_active:
                self.heating_since = None
            if heater_now_active and not self._heat_was_active:
                delta_to_target = abs((new_target or 0) - (curr_temp or 0))
                if delta_to_target > _NEW_SESSION_DELTA:
                    self._session_scalar = 1.0
                    self._session_scalar_bucket = None
                    self._session_fresh_buckets = set()
                    _LOGGER.debug(
                        "MSpa: new heating session (delta %.1f°C) — session scalar reset",
                        delta_to_target,
                    )
            self._heat_was_active = heater_now_active

            # If the heater stops while we're still significantly below target, the
            # session has been interrupted (maintenance, power cycle, user manually
            # turning off the heater).  Cancel any active prediction so that when
            # circulation later mixes the water and near_target triggers, a spurious
            # short result isn't recorded in the history.
            if (not heater_now_active
                    and self._prediction is not None
                    and not self.near_target):
                _LOGGER.info(
                    "PREDICTION_CANCELLED: heater stopped at %.1f°C with %.1f°C to go "
                    "— session interrupted (maintenance / power cycle / manual off)",
                    curr_temp or 0,
                    abs((new_target or 0) - (curr_temp or 0)),
                )
                self._prediction = None

            self._track_heating_rate(curr_temp, heat_state, now_mono)

            # --- Observed cooling rate tracking (see _track_cooling_rate) ---
            self._track_cooling_rate(curr_temp, heat_state, now_mono)
            # --- end rate tracking ---

            # Persist updated rates so they survive reloads and HA restarts.
            # _prediction is included so an in-progress heatup is not lost on restart.
            await self._rates_store.async_save({
                "heat_rate": self.computed_heat_rate,
                "cool_rate": self.computed_cool_rate,
                "heat_rate_buckets": self.heat_rate_buckets,
                "bucket_save_ts": time.time(),
                "prediction_history": self._prediction_history,
                "band_observations": self._band_observations,
                "band_stats": self._band_stats,
                "prediction_bias": self.prediction_bias,
                "active_prediction": self._prediction,
                # Persisted so a restart mid-scheduled-heating resumes as
                # "Heating" instead of dropping back to pending and re-firing.
                "schedule_triggered": self._schedule_triggered,
                "ambient_baseline": self.ambient_baseline,
                # Written so the shadow is legible in the storage file itself, not only
                # through the sensors — the whole comparison should survive a restart and
                # be readable without Home Assistant running.
                "newton_ready_at": (
                    self.newton_ready_at.isoformat()
                    if self.newton_ready_at is not None else None
                ),
                "newton_start_at": (
                    self.newton_start_at.isoformat()
                    if self.newton_start_at is not None else None
                ),
                "newton_fit": self.newton_fit(),
                "newton_implied_tub": self.physical_constants(),
                "temp_anchor_time": (
                    self.temp_anchor_time.isoformat()
                    if self.temp_anchor_time is not None else None
                ),
                "heating_since": (
                    self.heating_since.isoformat()
                    if self.heating_since is not None else None
                ),
                "temp_anchor_rising": self.temp_anchor_rising,
                "anchor_prev_reading": self._anchor_prev_reading,
                "temp_anchor_temp": self.temp_anchor_temp,
                "temp_anchor_target": self.temp_anchor_target,
            })

            # The physical model's shadow of the same two decisions, recomputed from
            # the same inputs and driving nothing. Before the trigger so that a poll
            # which fires the schedule still leaves a shadow of the plan it fired on.
            self._update_newton_shadow(new_temp, new_target)

            # Trigger heating autonomously when the schedule window opens.
            # Must run BEFORE the auto-clear so the trigger fires even when the
            # schedule target is already reached and minutes_needed == 0 (the
            # start_at == target_utc edge-case that the auto-clear would otherwise
            # pre-empt by clearing scheduled_ready_at first).
            await self._check_schedule_trigger(new_temp, new_target)

            # Auto-clear the schedule once its time has passed.
            self._clear_schedule_if_expired(new_temp)

            # Check for power cycle and restore state if enabled
            await self._check_power_cycle(transformed_data)
            
            # Handle temperature unit enforcement (if always_enforce_unit is enabled)
            await self._enforce_temperature_unit(transformed_data)
            
            # Check if we need to adjust polling based on heat state or pending changes
            await self._check_adaptive_polling(transformed_data)
            
            return transformed_data

        except Exception as err:
            # DEBUG, not ERROR.  DataUpdateCoordinator already logs
            # "Error fetching mspa data: <this same message>" at ERROR when an
            # UpdateFailed reaches it, so this line duplicated it in the same
            # millisecond — visible on every network blip in the logs.
            #
            # It was also worse than redundant on a sustained outage: HA logs the first
            # failure and then stays quiet until the next success, while this fired on
            # every poll.  Kept at debug because it is raised from a different place
            # than HA reports, which is occasionally useful when tracing.
            _LOGGER.debug("Update failed: %s", err, exc_info=True)
            raise UpdateFailed(f"Update failed: {err}") from err

    def _update_near_target(self, new_temp, new_target) -> None:
        """Maintain the near-target flag and the ready latch.

        Extracted from `_async_update_data`, where it was inline and mirrored by hand
        in the tests. The copies drifted: when the 2026-08-14 overshoot bug was fixed
        in the coordinator, the test helper went on applying the old rule and three
        tests failed against a correct fix. A block this consequential should have one
        definition, and be reachable from a test directly.

        Two independent routes to "Ready", deliberately:

        * `near_target` — where the water sits relative to the setpoint right now,
          with hysteresis so thermostat cycling cannot flicker it.
        * `ready_latched` — set once the spa first arrives, so a session that has
          finished stays finished. Released by cooling off, by the setpoint being
          raised, or by the schedule being cleared.
        """
        _NEAR_TARGET_DEACTIVATE = 0.25
        _NEAR_TARGET_ACTIVATE = 0.5
        if new_temp is None or new_target is None:
            return                      # no reading — leave both flags as they were

        # The *shortfall*, signed, not the absolute gap.  Water above target is ready
        # and then some, but `abs()` treated overshoot exactly like undershoot: on
        # 2026-08-14 the spa reached 40.0 against a 39.5 target — ordinary thermostat
        # overshoot — which read as a 0.5 °C gap and cleared near_target.  The schedule
        # expired in the same window and released the latch, so both routes to "Ready"
        # went at once and the sensor fell to unknown with the spa at temperature.
        #
        # Only falling short un-readies the spa.  The hysteresis still works in that
        # direction, and near_target self-releases as the water cools.
        delta = new_target - new_temp

        # A setpoint moved below the water is not the spa becoming ready.
        #
        # The latch records "it heated to target and is still dip-warm", and holds until
        # the water gives up _LATCH_COOL_OFF from its peak. Turning the thermostat down
        # to 20 while the water sits at 32 closes the gap without a watt of heating, and
        # latched Ready on a tub that had never reached the target it was set to.
        target_moved = self._latch_target is not None and new_target != self._latch_target

        # The first reading after a restart is an observation, not an arrival.
        #
        # The latch is set on the False→True edge of near_target, and both guards on that
        # edge are vacuously true on a fresh coordinator: near_target starts False and
        # there is no previous setpoint for target_moved to compare against. So a restart
        # with the water anywhere at or above the setpoint latched Ready on the very first
        # poll — including the case the guards exist to reject, a thermostat turned down
        # onto warm water. Restarting mid-heat with the tub at 28.5 °C and the setpoint
        # parked at 20 read as Ready with ten degrees still to go.
        #
        # It cannot be answered by refusing to latch at all, because the after-a-soak
        # latch is meant to survive: water at the setpoint after a restart should still
        # say Ready. So the first sample is decided on the evidence in front of it, which
        # is how far above the setpoint the water sits — the same test the sensor applies.
        # Within ordinary overshoot the spa plausibly heated there and the latch stands;
        # several degrees above and the setpoint was plainly moved, so it does not.
        first_sample = self._latch_target is None
        self._latch_target = new_target

        if delta < _NEAR_TARGET_DEACTIVATE:
            plausible = not first_sample or -delta <= _NEW_SESSION_DELTA
            if not self.near_target and not target_moved and plausible:
                self.ready_latched = True
                # Remember how warm the water was when it latched, so the cool-off
                # release below can tell how much heat has since been given up.
                self.ready_latched_temp = new_temp
                _LOGGER.debug(
                    "ready_latched set (near_target True, shortfall=%.2f°C%s)",
                    delta, ", first sample after restart" if first_sample else "")
            elif first_sample and not plausible:
                _LOGGER.debug(
                    "ready_latched withheld on first sample: water %.1f°C is %.1f°C above "
                    "setpoint %.1f°C, so the spa did not heat there",
                    new_temp, -delta, new_target,
                )
            self.near_target = True
        elif delta >= _NEAR_TARGET_ACTIVATE:
            self.near_target = False

        # Track the warmest water seen while latched.  Thermostat cycling nudges the
        # temperature either side of the setpoint — including into the hysteresis dead
        # band, where neither branch above runs — so the cool-off must measure from the
        # peak reached, not from whatever the reading happened to be when it latched.
        if (self.ready_latched
                and (self.ready_latched_temp is None
                     or new_temp > self.ready_latched_temp)):
            self.ready_latched_temp = new_temp

        # Cool-off release: withdraw "Ready" once the water has given up
        # _LATCH_COOL_OFF degrees from where it latched.  Without this the latch
        # outlives the warmth it advertises — drop the thermostat to 20 °C with the
        # water at 40 °C and, two days later, the water is 24 °C, still "above target",
        # and the sensor would happily claim the tub is ready for a dip.  Checked
        # regardless of direction, because with a lowered thermostat the spa is
        # technically cooling yet still above setpoint.
        if (self.ready_latched
                and self.ready_latched_temp is not None
                and (self.ready_latched_temp - new_temp) >= _LATCH_COOL_OFF):
            self.ready_latched = False
            _LOGGER.info(
                "ready_latched released (water cooled %.1f°C from %.1f°C to %.1f°C — "
                "no longer warm enough to call ready)",
                self.ready_latched_temp - new_temp, self.ready_latched_temp, new_temp,
            )
            self.ready_latched_temp = None

        # Release the latch once a genuine heating gap opens.  The latch holds "Ready"
        # steady after the spa arrives, but raising the setpoint means there is real
        # heating to do and the sensor must follow the thermostat again — otherwise it
        # stays pinned on "Ready" with degrees to go.
        #
        # Heating direction only: water above the setpoint (the thermostat was lowered
        # while the spa is warm) is still legitimately "Ready".  Using the new-session
        # threshold rather than the hysteresis one means thermostat cycling cannot
        # flicker the latch off and on.
        if self.ready_latched and (new_target - new_temp) > _NEW_SESSION_DELTA:
            self.ready_latched = False
            self.ready_latched_temp = None
            _LOGGER.debug(
                "ready_latched released (setpoint %.1f°C is %.1f°C above water "
                "%.1f°C — new heating session)",
                new_target, new_target - new_temp, new_temp,
            )

    def _update_temp_anchor(self, new_temp, new_target) -> None:
        """Re-anchor when the reading or target changes, at the band centre.

        Sensors use (anchor_time, anchor_temp, anchor_target) to measure elapsed
        progress between reported temperature steps without keeping per-sensor state,
        and `scheduling_temp` extrapolates from it.  Both depend on the anchor moving
        *only* at a real change.

        A reading change is a band crossing, and at that instant the true temperature is
        the threshold *between* the two reported values — half a step from either.
        Recording the new reading verbatim carries a systematic error whose sign depends
        on direction: 0.25 °C high when warming, 0.25 °C low when cooling. At ~1 °C/h
        that is a quarter-hour of ETA each way, and a full half hour across a cool-down
        followed by a heat-up, which is what made Ready at and the scheduler disagree by
        ~20 min at a session start.

        **Change detection compares readings with readings.** `temp_anchor_temp` is a
        midpoint, so testing the reading against it never matches: the anchor re-fired
        on every poll, halving its way back toward the raw reading and resetting
        `temp_anchor_time` each time. That silently defeated three things at once — the
        half-step correction decayed away, elapsed-time measurement never accumulated,
        and `scheduling_temp`'s clamp was measured from a moving anchor so the estimate
        could leave the reading's band entirely. Extracted from `_async_update_data` and
        given tests precisely because none of that was visible from outside.

        With the midpoint correct, the clamp in `predictor.extrapolate_within_band`
        bounds the estimate to exactly the reported reading's own band.
        """
        if (new_temp == self._anchor_prev_reading
                and new_target == self.temp_anchor_target):
            return

        prev = self._anchor_prev_reading
        anchored = new_temp
        rising = None
        if (prev is not None and new_temp is not None
                and abs(new_temp - prev) <= _TEMP_BAND_C + 1e-9
                and new_temp != prev):
            anchored = (new_temp + prev) / 2.0
            rising = new_temp > prev
        now = datetime.now(timezone.utc)
        shadow = getattr(self, "_shadow", None)
        if (shadow is not None and prev is not None and new_temp is not None
                and new_temp != prev):
            # A reported-temperature change is a crossing: an exact position, which is
            # the only thing the shadow will measure between.
            if shadow.crossing(new_temp, now):
                _LOGGER.info(
                    "Ready at: re-anchored at %.1f °C (band edge or final approach) "
                    "— ready %s, revision %d",
                    new_temp, shadow.eta.isoformat(timespec="minutes"), shadow.revisions,
                )
        self.temp_anchor_time     = now
        self.temp_anchor_temp     = anchored
        self.temp_anchor_target   = new_target
        self.temp_anchor_rising   = rising
        self._anchor_prev_reading = new_temp

    def shadow_eta(self):
        """Ready time from the shadow curve, or None outside a session.

        Revised three times in a typical session rather than at every sample, and from
        the first revision — a little under halfway — it is within half an hour whether
        or not the stored rates it started from were right. See predictor.ShadowPlan.
        """
        shadow = getattr(self, "_shadow", None)
        return shadow.eta if shadow is not None else None

    def shadow_revisions(self):
        """How many times the shadow curve has revised itself this session.

        Part of the displayed ETA's replan identity: a revision is a deliberate,
        infrequent correction and the display should adopt it rather than crawl toward
        it.  None outside a session, which keeps the identity stable when there is no
        shadow at all.
        """
        shadow = getattr(self, "_shadow", None)
        return shadow.revisions if shadow is not None else None

    def session_plan(self) -> "HeatPredictor | None":
        """The rate curve frozen at session start, or None outside a session.

        Bias and scalar are deliberately 1.0: the frozen rates already carry the
        ambient correction, and the bias was applied once to the opening estimate.
        Re-applying either here would compound a correction the plan already contains.
        """
        rates = (self._prediction or {}).get("plan_rates")
        if not rates or not any(rates):
            return None
        return HeatPredictor(buckets=tuple(rates), prediction_bias=1.0)

    def session_settled(self, at_temp, now=None) -> bool:
        """Whether the session has run long and far enough to trust a replan."""
        pred = self._prediction
        if not pred or at_temp is None:
            return False
        try:
            started = datetime.fromisoformat(pred["start_time"])
            start_temp = float(pred["start_temp"])
        except (KeyError, TypeError, ValueError):
            return False
        elapsed = ((now or datetime.now(timezone.utc)) - started).total_seconds() / 60.0
        return (elapsed >= _PLAN_SETTLE_MINUTES
                and (at_temp - start_temp) >= _PLAN_SETTLE_DEGREES)

    def session_opening_eta(self):
        """Finish time implied by the opening estimate, or None outside a session."""
        pred = self._prediction
        if not pred:
            return None
        try:
            started = datetime.fromisoformat(pred["start_time"])
            return started + timedelta(minutes=float(pred["estimated_minutes_biased"]))
        except (KeyError, TypeError, ValueError):
            return None

    def session_progress_deviation(self, at_temp, now=None):
        """Minutes behind (positive) or ahead (negative) of the opening plan.

        Measured, not predicted: elapsed time against what the frozen plan allowed to
        reach the temperature now reached.  Separating this from the ETA is the point —
        a session can be running 20 minutes behind while the finish estimate stays
        stable, and conflating the two is what made the shipped ETA chase its own tail.

        None until the session settles.  The opening crossings measure position within
        the 0.5 °C band rather than heating — on 2026-08-12 the first degree "took"
        7.6 minutes, an implied 7.9 °C/h that no heater here can produce — so a
        deviation computed then reads tens of minutes ahead and means nothing.  The
        same contamination is why the ETA holds through the settle period.
        """
        plan = self.session_plan()
        pred = self._prediction
        if plan is None or not pred or at_temp is None:
            return None
        if not self.session_settled(at_temp, now):
            return None
        try:
            started = datetime.fromisoformat(pred["start_time"])
            start_temp = float(pred["start_temp"])
        except (KeyError, TypeError, ValueError):
            return None
        allowed = plan.heating_minutes(start_temp, at_temp)
        if allowed is None:
            return None
        elapsed = ((now or datetime.now(timezone.utc)) - started).total_seconds() / 60.0
        return round(elapsed - allowed, 1)

    @property
    def fault_code(self) -> str | None:
        """The spa's fault code, or None when healthy.

        F1 is the flow fault, raised when the pump starts with a physical problem in
        the way — blocked flow, debris, a clogged or worn pump, frozen pipes. It cannot
        be cleared remotely, so switching anything on while it stands is pointless at
        best; refusing and saying so is more useful than retrying into it.
        """
        fault = self._last_data.get("fault")
        return None if fault in (None, "", "OK") else str(fault)

    @property
    def circulating(self) -> bool:
        """Whether the circulation pump is running, so the probe reads tub water.

        The probe sits in the external pump housing.  With the pump off it measures a
        separate stagnant volume that runs at or below tub temperature, and housings
        differ between spa models, so there is no offset to calibrate.
        """
        return self._last_data.get("filter") == "on"

    def scheduling_temp(self) -> float | None:
        """Water temperature to plan from — extrapolated inside its band when sound.

        The reported value is quantised to 0.5 °C and the scheduler recomputes from it
        every poll, so a crossing moves the planned start by a whole band of heating in
        one step — 27-44 min across the bucket rates.  That lump is what makes the
        schedule "miss": a crossing landing shortly before the planned start turns the
        plan into one that should already have begun, and there is no way to start in
        the past.

        Extrapolating from the crossing that entered the band removes the lump, and it
        costs nothing: averaged over a dwell the estimate is neutral against holding the
        reading, because it runs half a band warm just after a crossing and half a band
        cold just before the next, and the reading is the mean of the two.

        Every guard falls back to the reported reading, which is exactly today's
        behaviour, so nothing here can do worse than not being here:

        * **Not circulating** — the probe is in stagnant housing water and is not
          measuring the tub at all.  This is the one guard that is about the hardware
          rather than the model, and it is why accurate scheduling wants the pump left
          running.
        * **Anchor predates circulation** — the crossing was recorded while the probe
          was reading housing water, so its threshold does not describe the tub.
        * **Heater state changed after the anchor** — the water may have reversed
          direction since, making the recorded direction stale.
        * **Direction unknown** — the anchor came from a restart or a jump of more than
          one band, not from an observed crossing.
        * **No learned rate** — nothing to extrapolate along.
        """
        try:
            reading = float(self._last_data.get("water_temperature"))
        except (TypeError, ValueError):
            return None

        if not self.circulating:
            return reading
        anc_t, anc_temp = self.temp_anchor_time, self.temp_anchor_temp
        rising = self.temp_anchor_rising
        if anc_t is None or anc_temp is None or rising is None:
            return reading
        if self.circulating_since is None or anc_t < self.circulating_since:
            return reading
        if self.heating_since is not None and self.heating_since > anc_t:
            return reading

        # Warming with the heater off has no rate model, so do not invent one.
        #
        # The rise is real — sun on the tub, or heat conducted into it — but its rate
        # is nothing like the heater's. Extrapolating at bucket_rate (0.79-1.10 °C/h)
        # against a solar gain of perhaps 0.2-0.5 °C/h saturates the clamp two to five
        # times too early and then sits pinned a quarter-band above the reading. That
        # is ~19 min of false optimism at 0.8 °C/h, and it starts the session *late*,
        # which is the unsafe direction.
        #
        # It also flips sign at a band boundary: alternating crossings would swing the
        # estimate ±0.25 °C on mismatched slopes — up at the heater's rate, down at the
        # cooling rate. Bounded by the clamp, so never a runaway, but erratic.
        #
        # Cool-rate *learning* is already safe here (it only samples a falling reading),
        # so nothing else needs guarding. Falling back to the reported reading is the
        # pre-extrapolation behaviour, and conservative: it under-states the water.

        if rising and self.heating_since is None:
            return reading

        rate = (self._predictor().bucket_rate(anc_temp) if rising
                else self.computed_cool_rate)
        elapsed_h = (datetime.now(timezone.utc) - anc_t).total_seconds() / 3600.0
        est = extrapolate_within_band(anc_temp, elapsed_h, rate, cooling=not rising)
        if est is None:
            return reading

        if abs(est - anc_temp) >= _TEMP_BAND_C - 1e-9:
            # Pinned at the far edge: the model expected a crossing that has not
            # arrived, so the rate is optimistic in this regime.
            _LOGGER.debug(
                "scheduling_temp: band saturated after %.0f min at %.3f °C/h "
                "(anchor %.2f, reading %.1f) — rate is optimistic",
                elapsed_h * 60, rate or 0.0, anc_temp, reading,
            )
        return est

    def _clear_schedule_if_expired(self, current_temp) -> None:
        """Retire the schedule once its ready time has passed.

        Clears the trigger state AND the readiness latch: the latch exists so
        "Ready" holds steady through the scheduled session, but once the
        session is over the Ready at sensor must be released to follow the
        thermostat again — otherwise it stays pinned on "Ready" while the
        water cools or the setpoint is lowered, until the next schedule or a
        reload happens to reset it.  If the spa genuinely is at the thermostat
        target after clearing, near_target keeps the sensor on "Ready" on its
        own merit.
        """
        if (self.scheduled_ready_at is not None
                and dt_util.utcnow() >= dt_util.as_utc(self.scheduled_ready_at)):
            self.clear_schedule("session complete", current_temp)

    def clear_schedule(self, reason: str, current_temp: float | None = None) -> None:
        """Retire the schedule and all state derived from it.

        Deliberately does NOT touch the heater or the setpoint: cancelling a plan
        for later says nothing about whether the spa should be heating now.  A
        user who wants it off turns it off.

        Shared by the expiry auto-clear and the Cancel heat schedule button so the
        two cannot drift — the latch release in particular is easy to omit, and
        forgetting it once already shipped as a bug where Ready at stayed pinned
        on "Ready" indefinitely after a session.
        """
        if self.scheduled_ready_at is None:
            return
        _LOGGER.info(
            "Heat schedule: %s — clearing  "
            "[water=%.1f°C  sched=%.1f°C  triggered=%s  latched=%s]",
            reason, current_temp or 0.0, self.schedule_target_temp or 0.0,
            self._schedule_triggered, self.ready_latched,
        )
        self.scheduled_ready_at = None
        self._schedule_triggered = False
        self._last_computed_start_at = None
        self.ready_latched = False
        self.ready_latched_temp = None

    # How many band traverses to keep. Three per full heat-up from cold, so roughly
    # sixty sessions — enough to span a year of weather, which is what a sensitivity
    # has to be fitted across. Bounded because this file is written on every save.
    _BAND_OBSERVATIONS_MAX = 200

    # How the running fit forgets, and why it is counted in observations rather than days.
    #
    # Decaying by elapsed time would erase a spa that sits unused over winter — and it
    # would erase exactly the cold observations that are scarcest and hardest to replace.
    # Plenty of owners do not use a spa from October to April, and any spa has gaps. Faded
    # per observation, a gap costs nothing: the evidence waits, and comes back weighted as
    # it was left.
    #
    # 0.999 per traverse is deliberately slower than it looks. Three traverses per heat-up
    # and three heat-ups a week is about 450 a year, so last winter still carries roughly
    # two thirds of its weight a year later, and half of it after about eighteen months.
    # Fading exists so a new cover or a different fill level eventually washes out, not so
    # the model chases the season.
    _BAND_FADE = 0.999
    # Nothing fades until there is something to lose. Below this the fit is short of
    # evidence already and diluting it would only slow it down.
    _BAND_FADE_AFTER = 30

    @staticmethod
    def _empty_band_stats() -> dict:
        return {"n": 0, "sum_amb": 0.0, "sum_amb2": 0.0,
                "sum_delta": 0.0, "sum_delta2": 0.0,
                "sum_rate": 0.0, "sum_amb_rate": 0.0, "sum_delta_rate": 0.0,
                "min_amb": None, "max_amb": None}

    def _accumulate_band_stats(self, band, rate, ambient, delta) -> None:
        """Fold one traverse into the running fit for its band.

        Also tracks the range of ambient seen, because that is what decides whether the
        fit means anything. A slope from thirty observations all taken between 12 and
        14 °C is noise wearing a number: the correction it implies is dominated by
        whatever else varied. Range is the gate, not count.
        """
        if rate is None or ambient is None:
            return
        st = self._band_stats.setdefault(str(int(band)), self._empty_band_stats())
        # Fade before adding, so the newest traverse always carries full weight.
        if st["n"] >= self._BAND_FADE_AFTER:
            f = self._BAND_FADE
            for k in ("n", "sum_amb", "sum_amb2", "sum_delta", "sum_delta2",
                      "sum_rate", "sum_amb_rate", "sum_delta_rate"):
                st[k] *= f
        st["n"] += 1
        st["sum_amb"] += ambient
        st["sum_amb2"] += ambient * ambient
        st["sum_rate"] += rate
        st["sum_amb_rate"] += ambient * rate
        if delta is not None:
            st["sum_delta"] += delta
            st["sum_delta2"] += delta * delta
            st["sum_delta_rate"] += delta * rate
        st["min_amb"] = ambient if st["min_amb"] is None else min(st["min_amb"], ambient)
        st["max_amb"] = ambient if st["max_amb"] is None else max(st["max_amb"], ambient)

    @property
    def weather_entity(self) -> str | None:
        """The configured weather entity, which is what decides whether estimates are
        corrected for the outdoor temperature. There is no separate switch: without a
        weather entity `ambient_temp` is None and every correction already returns 1.0."""
        entry = getattr(self, "config_entry", None)
        if entry is None:
            return None
        return (getattr(entry, "options", None) or {}).get(CONF_WEATHER_ENTITY)

    def band_fits(self, *, force: bool = False) -> dict:
        """Every band's fit, keyed by band index, for handing to a HeatPredictor.

        `force` is kept for the side-by-side comparison, which is recorded whether or not
        a weather entity is configured — a spa with no weather source still learns
        nothing useful here (its observations carry no ambient), but the argument is part
        of the seam and removing it would only move the decision somewhere less obvious.
        """
        return {i: self.band_rate_fit(i) for i in (0, 1, 2)
                if self.band_rate_fit(i) is not None}

    def band_rate_fit(self, band, against="amb"):
        """Least-squares slope and intercept of rate against ambient, over all history.

        Returns None where there is nothing worth fitting. `against="delta"` fits the
        water/air gap instead, for the physical form.
        """
        st = self._band_stats.get(str(int(band)))
        if not st or st["n"] < 2:
            return None
        n = st["n"]
        sx = st["sum_amb"] if against == "amb" else st["sum_delta"]
        sxx = st["sum_amb2"] if against == "amb" else st["sum_delta2"]
        sxy = st["sum_amb_rate"] if against == "amb" else st["sum_delta_rate"]
        sy = st["sum_rate"]
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-9:
            return None                      # every observation at the same temperature
        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n
        # Spread as a weighted standard deviation, not as min-to-max.
        #
        # min/max never fade, so a single cold night three winters ago would go on
        # claiming a wide range long after its weight had gone. It is also a poor measure
        # of evidence in its own right: two readings at the extremes and thirty in the
        # middle is not the same body of evidence as thirty spread evenly, and min/max
        # cannot tell them apart. The standard deviation decays with the sums it is
        # computed from, so the reported spread always describes the fit actually in hand.
        mean_amb = st["sum_amb"] / n
        var = max(0.0, st["sum_amb2"] / n - mean_amb * mean_amb)
        return {
            "slope": slope, "intercept": intercept, "n": n,
            # The centre of the evidence. A fit is trusted near it and handed back to the
            # seed away from it, so a caller needs to know where "near" is.
            "ambient_mean": mean_amb,
            "ambient_sd": var ** 0.5,
            # Kept for reading a log by eye. Deliberately not the gate: these do not fade.
            "ambient_seen": (st["min_amb"], st["max_amb"]),
        }

    # ── The physical model, measured alongside the one that ships ────────────
    #
    # Fitted from `_band_observations` rather than from running sums, deliberately.
    # The rows are already persisted with everything the fit needs — water mean, ambient
    # mean and rate per traverse — so this adds no state, no migration and no second
    # fading scheme, and a later analysis can refit them differently without having had
    # to decide the form in advance. Two hundred traverses is roughly a year.
    #
    # None of it drives a prediction. See predictor.py.

    def newton_fit(self) -> dict | None:
        """This spa's `τ` and asymptote, from every traverse recorded."""
        return newton_fit(self._band_observations)

    def newton_free_fit(self) -> dict | None:
        """Water and air regressed separately — the coefficients the law constrains."""
        return newton_free_fit(self._band_observations)

    @property
    def heater_power_heat_w(self) -> int:
        """Rated heater power in full-heat mode, as configured for the energy sensors.

        Unambiguously the mode-3 figure and not the pre-heat one, because rates are only
        ever learned while `heat_state == 3` — `_track_heating_rate` samples nothing else.
        """
        opts = getattr(getattr(self, "config_entry", None), "options", None) or {}
        try:
            value = int(opts.get("heater_power_heat", DEFAULT_HEATER_POWER_HEAT))
        except (TypeError, ValueError):
            return DEFAULT_HEATER_POWER_HEAT
        return value if value > 0 else DEFAULT_HEATER_POWER_HEAT

    def physical_constants(self) -> dict | None:
        """Thermal mass and loss coefficient implied by the fit and the rated power.

        The heater only. The circulation pump runs throughout but turns a pump rather
        than heating the water — see `physical_constants` for why it is not added in.
        """
        return physical_constants(self.newton_fit(), self.heater_power_heat_w)

    def newton_minutes(self, from_temp, to_temp, *, ambient=None) -> float | None:
        """What the physical model would predict for this span, or None if it cannot.

        Uses the fit as it stands *now*, so calling it when a session opens gives a
        genuinely out-of-sample prediction: the traverses of that session have not been
        recorded yet and cannot have informed it.
        """
        fit = self.newton_fit()
        if fit is None:
            return None
        amb = self.ambient_temp if ambient is None else ambient
        return newton_heating_minutes(
            from_temp, to_temp, amb, fit["tau_h"], fit["asymptote_lift_c"])

    def _update_newton_shadow(self, current_temp, current_target) -> None:
        """Recompute the physical model's shadow of Ready at and of the planned start.

        Deliberately raw. The shipping Ready at slews its display so corrections land as
        bounded ramps, and latches once the water arrives; neither is a property of the
        model, and reproducing them here would hide exactly the wandering this is meant
        to expose. What is compared is the estimate, not the presentation of it.

        Both go to None whenever the model declines — too few traverses to fit, or an
        asymptote at or below the target, which on a cold night is a real answer. A gap
        in the history is the finding; a fallback would erase it.
        """
        self.newton_ready_at = None
        self.newton_start_at = None
        plan_temp = self.scheduling_temp()
        if plan_temp is None:
            plan_temp = current_temp
        if plan_temp is None:
            return

        # Ready at: to whichever target is actually being heated towards — the schedule's
        # while one is pending, the thermostat's otherwise. Mirrors the shipping sensor's
        # choice of target so the two series are answering the same question.
        target = current_target
        if self.scheduled_ready_at is not None and self.schedule_target_temp is not None:
            target = self.schedule_target_temp
        if target is not None:
            minutes = self.newton_minutes(plan_temp, target)
            if minutes is not None:
                self.newton_ready_at = (
                    datetime.now(timezone.utc) + timedelta(minutes=minutes))

        # Planned start: the same subtraction `_check_schedule_trigger` makes, against
        # the same scheduled time, so the two start times are directly comparable.
        if (self.scheduled_ready_at is not None and not self._schedule_triggered
                and self.schedule_target_temp is not None):
            minutes = self.newton_minutes(plan_temp, self.schedule_target_temp)
            if minutes is not None:
                self.newton_start_at = (
                    dt_util.as_utc(self.scheduled_ready_at)
                    - timedelta(minutes=minutes))

    def _tub_is_disturbed(self) -> bool:
        """Whether the spa is in a state where its heating rate cannot be measured.

        Bubbles or jets mean the tub is in use or being cleaned — never part of a heating
        cycle. Either way the cover is off, air or water is being driven through, and
        what is being measured is the evening rather than the spa.

        The signal is one-way, and that is the whole of how it is used. A switch being
        *on* is conclusive: stop recording. A switch being *off* concludes nothing — the
        cover comes off before anyone reaches for a switch, and plenty of soaks happen
        with neither running. So this can only ever add a reason to discard; it can never
        be read as permission to trust a span.

        Everything that says a span *is* trustworthy comes from watching the water
        instead — see `_window_looks_unmeasurable`. Once either has fired the flag
        persists to the end of the band, because the pieces after a disturbance are
        measuring the recovery rather than the spa.
        """
        data = getattr(self, "_last_data", None) or {}
        return data.get("bubble") == "on" or data.get("jet") == "on"

    # A heating spa whose water is *falling* is not being measured — something is taking
    # heat out faster than a 2 kW heater puts it in, which in practice means the cover is
    # off in weather. Unambiguous, and needs no model to detect.
    #
    # Below this fraction of what the curve expects, a span is treated the same way. The
    # threshold is deliberately generous: the danger of a tighter one is circular, since a
    # model that is wrongly high would reject the very evidence that would correct it. It
    # is applied only once the band has enough observations to have a real expectation.
    _UNMEASURABLE_RATE_FRACTION = 0.4
    _EXPECTATION_NEEDS_N = 10.0

    def _window_looks_unmeasurable(self, band, from_temp, rate) -> str | None:
        """Why this span cannot be learned from, or None if it can.

        Watches the water rather than the accessories, which matters because the
        accessories lag: the cover is off for some minutes before anyone switches
        anything on. It also catches a cover simply left open, where no switch is touched
        at all.
        """
        if rate is None:
            return None
        if getattr(self, "ready_latched", False):
            # Already at temperature. Whatever the heater does now is maintenance against
            # losses, not a heat-up, and the tub is at its most likely to be in use — so
            # this is where the noisiest readings live and none of them are wanted.
            return "the spa was already up to temperature"
        if rate <= 0:
            return "the water was falling while the heater was on"
        if rate > _MAX_HEAT_RATE:
            # Faster than any heater can manage, so the water was changed rather than
            # heated — a top-up with hot water, most likely. Worth flagging rather than
            # merely discarding: a fill alters how much water there is, so the rest of
            # this band is measuring a different tub, and the mixing that follows is not
            # a heating rate at all. A cold fill shows up the other way, as a sudden fall
            # caught by the test above.
            return (f"{rate:.1f} °C/h is faster than the heater can manage — "
                    f"the water was probably topped up")
        st = self._band_stats.get(str(int(band)))
        if not st or st["n"] < self._EXPECTATION_NEEDS_N:
            return None                       # no expectation worth measuring against yet
        try:
            expected = self._predictor().bucket_rate(from_temp)
        except Exception:                     # a model that cannot answer is not evidence
            return None
        if expected and rate < expected * self._UNMEASURABLE_RATE_FRACTION:
            return (f"{rate:.2f} °C/h against about {expected:.2f} expected — "
                    f"the cover is probably off")
        return None


    def _seed_window_ambient(self, *, new_band: bool = True) -> None:
        """Start a fresh ambient window at the conditions the band is entered in.

        Zeroing instead of seeding leaves the mean covering only the polls *after* the
        anchor, which biases it toward the end of the traverse — measurably: a band
        crossed through 6, 10 and 14 °C reported 12.0 rather than 10.0, because the
        reading at the anchor was the one poll that never reached the accumulator. The
        anchor poll is the moment the band was entered and belongs in its mean.
        """
        self._window_amb_sum = float(self.ambient_temp) if self.ambient_temp is not None else 0.0
        self._window_amb_n = 1 if self.ambient_temp is not None else 0
        self._window_wind_sum = float(self.ambient_wind) if self.ambient_wind is not None else 0.0
        self._window_wind_n = 1 if self.ambient_wind is not None else 0
        # The disturbance survives a mid-band re-anchor.
        #
        # Rejecting a sample re-anchors the window, so a spell with the cover off splits
        # one traverse into pieces — and the pieces after it would otherwise look clean.
        # They are not: the water has just been churned, the cover has only this moment
        # gone back on, and what is being measured is the recovery rather than the spa.
        # The flag is cleared when the band is genuinely left, which is also when the
        # traverse it belonged to is over.
        if new_band:
            self._window_disturbed = self._tub_is_disturbed()
        else:
            self._window_disturbed = self._window_disturbed or self._tub_is_disturbed()

    def _record_band_observation(self, band, from_temp, to_temp, hours, rate,
                                 *, usable: bool = True,
                                 bucket_learnable: bool = True):
        """Record one full band traverse, with the weather that prevailed across it.

        Each of these is a single clean point for the question the model currently
        answers with a hard-coded guess: how much does the outdoor temperature move
        *this* band's heating rate. `AMBIENT_SENSITIVITY` is (0.0, 0.02, 0.06) — three
        constants chosen from measurement on a different spa — and nothing in the
        integration has ever checked them against this one.

        The mean matters more than it looks. Ambient was previously recorded once per
        session, at the moment heating started: on 2026-08-25 that was 10.8 °C at 08:41,
        while the mid band was crossed between 12:22 and 17:58 in a warming afternoon,
        and the correction the session ran on was 20% out by the end. A band's own mean
        is the number its rate should be regressed against.

        The water span is kept rather than only the band index, because the physical
        quantity is the gap between water and air: loss scales with it, which is why the
        hot band is the weather-sensitive one. Storing both means either form can be
        fitted later without re-recording.
        """
        if not hours or hours <= 0 or rate is None:
            return
        amb = (self._window_amb_sum / self._window_amb_n
               if self._window_amb_n else self.ambient_temp)
        wind = (self._window_wind_sum / self._window_wind_n
                if self._window_wind_n else self.ambient_wind)
        water_mean = (float(from_temp) + float(to_temp)) / 2.0
        self._band_observations.append({
            "at": datetime.now(timezone.utc).isoformat(),
            # Kept even when it cannot be learned from. A discarded traverse still says
            # what happened, and a run of them is how a fault shows itself — an empty
            # record would look like a spa that simply was not used.
            "usable": bool(usable),
            # Whether a bucket was allowed to learn from it. False for a span outside
            # 20-39 °C, which the physical model still uses — see the call site.
            "bucket_learnable": bool(bucket_learnable),
            "disturbed": bool(self._window_disturbed),
            "band": int(band),
            "from_temp": round(float(from_temp), 2),
            "to_temp": round(float(to_temp), 2),
            "hours": round(float(hours), 4),
            "rate": round(float(rate), 4),
            "water_mean": round(water_mean, 2),
            "ambient_mean": round(float(amb), 2) if amb is not None else None,
            "wind_mean": round(float(wind), 2) if wind is not None else None,
            # The regressor the physics actually wants, stored so a fit need not
            # reconstruct it and cannot reconstruct it differently.
            "delta_mean": (round(water_mean - float(amb), 2)
                           if amb is not None else None),
        })
        self._band_observations = (
            self._band_observations[-self._BAND_OBSERVATIONS_MAX:])
        # The per-band running fit is the *bucket* model's weather response, so it takes
        # only spans a bucket could have learned from. A 6→20 chord belongs in the record
        # and in the physical fit, and would misdescribe the cold bucket's sensitivity.
        if usable and bucket_learnable:
            self._accumulate_band_stats(
                band, float(rate), amb if amb is None else float(amb),
                None if amb is None else water_mean - float(amb))
        _LOGGER.debug(
            "Band observation: band %d, %.1f→%.1f °C in %.2f h = %.3f °C/h, "
            "ambient mean %s °C (water-air %s)",
            band, from_temp, to_temp, hours, rate,
            f"{amb:.1f}" if amb is not None else "n/a",
            f"{water_mean - amb:.1f}" if amb is not None else "n/a",
        )

    def _track_heating_rate(self, curr_temp, heat_state, now_mono: float) -> None:
        """Sample the observed heating rate over a growing per-bucket window.

        Only samples in full-heat mode (heat_state == 3) to avoid preheat and
        cooling skew.

        The first crossing after the heater engages is phase-uncertain: the
        temperature reports in 0.5 °C bands, so the anchor set at heater-on
        sits at an unknown position inside its band and the time to the first
        crossing measures that random phase, not the rate (~2x fast on
        average, arbitrarily fast when the water started near a boundary).
        That crossing re-anchors only — the anchor is then exactly on a band
        boundary — and learning starts from the second crossing.

        From there the anchor is *held*, so each sample measures from the first
        boundary reached in this bucket to the current one rather than from the
        previous crossing.  Every crossing lands exactly on a band boundary, so
        any boundary-to-boundary span is equally phase-exact — a wider span is
        no more biased, and much less noisy, because rate is delta/elapsed and a
        few minutes of report-timing jitter matters far less over hours than
        over one 30-minute step.

        Measured on the 2026-08-06 session (22 samples): per-step sampling gave
        18% noise in the cold bucket and dragged its stored rate to 0.98 °C/h
        against a realised 1.14; the growing window gives 7% and 1.08.  In the
        mid bucket, 3% becomes 1% (0.4% once the span reaches 2 °C), converging
        on 0.958 °C/h — exactly that bucket's realised rate.  It also absorbs
        paired reporting glitches that per-step sampling cannot: that session
        contained 27.5→28.0 at 0.51 °C/h immediately followed by 28.0→28.5 at
        29.5 °C/h — one late report then one early — where the out-of-bounds
        second is rejected and the spuriously slow first is learned.

        The window closes when the water leaves the anchor's bucket (each bucket
        must be measured on its own, since they model different loss regimes) or
        when a sample is rejected (a bad span is never re-used).  Heater-off
        resets it entirely via the branch below, so a window never spans an
        interruption.

        Successive estimates from a growing window share most of their data, so
        the per-crossing EMA double-counts the early samples and its effective
        time constant is shorter than nominal.  That is harmless here — the
        sequence it averages is smooth and converging rather than noisy — but it
        is why the EMA must not be re-tuned on the assumption that samples are
        independent.
        """
        if heat_state == _HEAT_STATE_FULL and curr_temp is not None:
            if self._rate_last_temp is None:
                # First poll in heat mode — set a phase-uncertain anchor.
                self._rate_last_temp = curr_temp
                self._rate_last_time = now_mono
                self._seed_window_ambient()
                self._rate_prev_temp = curr_temp
                self._rate_first_step = True
            elif curr_temp != self._rate_prev_temp:
                # A genuine change since the previous poll.  This must be tested
                # against the previous *reading*, not against the window anchor:
                # the anchor is deliberately held across several crossings, so
                # comparing to it stays true on every poll until the next change
                # and re-learns the same span once per poll with an ever-growing
                # elapsed time.  Observed 2026-08-07: one 36.5→37.0 crossing
                # re-learned every 30 s for twelve minutes, walking bucket[2]
                # from 1.040 down to 0.822.
                self._rate_prev_temp = curr_temp
                if self._rate_first_step:
                    # First crossing: position information only.  Keep the
                    # existing learned rates driving the estimates.
                    _LOGGER.info(
                        "Heat rate: first crossing %.1f→%.1f°C after heater-on "
                        "is phase-uncertain — anchored, not learned",
                        self._rate_last_temp, curr_temp,
                    )
                    self._rate_first_step = False
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
                    self._seed_window_ambient()
                    return
                # Temperature has changed — elapsed time since the window anchor
                # is the true duration of the whole boundary-to-boundary span.
                elapsed_hours = (now_mono - self._rate_last_time) / 3600
                if self._tub_is_disturbed():
                    self._window_disturbed = True
                if self.ambient_temp is not None:
                    self._window_amb_sum += float(self.ambient_temp)
                    self._window_amb_n += 1
                if self.ambient_wind is not None:
                    self._window_wind_sum += float(self.ambient_wind)
                    self._window_wind_n += 1
                anchor_bucket = _heat_bucket_index(self._rate_last_temp)
                accepted = False
                if elapsed_hours >= _MIN_RATE_SAMPLE_HOURS:
                    delta = curr_temp - self._rate_last_temp
                    rate = delta / elapsed_hours  # guarded: elapsed_hours > 0
                    # Before the branches: a span that cannot be learned from is still
                    # recorded, and the record needs to know whether it was in range.
                    _in_learn_range = in_learning_range(self._rate_last_temp, curr_temp)
                    # Bound here rather than in the accepted branch: a span that cannot
                    # be learned from still belongs to a band, and is still recorded.
                    _bi = anchor_bucket
                    _why = (self._window_looks_unmeasurable(
                                anchor_bucket, self._rate_last_temp, rate)
                            or ("the tub was in use — bubbles or jets ran"
                                if self._window_disturbed else None))
                    if _why:
                        self._window_disturbed = True
                        _LOGGER.info(
                            "Heat rate sample %.3f °C/h ignored: %s", rate, _why
                        )
                    elif _MIN_HEAT_RATE <= rate <= _MAX_HEAT_RATE:
                        accepted = True
                        if self.computed_heat_rate is None:
                            self.computed_heat_rate = rate
                        else:
                            self.computed_heat_rate = (
                                _EMA_ALPHA * rate
                                + (1 - _EMA_ALPHA) * self.computed_heat_rate
                            )
                        # The window anchor is the *start* temperature of the span.
                        # Weight the sample by how much of the bucket it measured, and
                        # recompute from the value the window started at.
                        #
                        # Samples from a growing window are nested — each shares most
                        # of its data with the last — so feeding every one into an EMA
                        # at full weight compounds the same evidence repeatedly, and
                        # weights the shortest, least representative span exactly as
                        # heavily as the longest. Measured on 2026-08-12: the first
                        # mid-bucket sample covered 0.5 °C of a 7 °C bucket and moved
                        # its rate 9.9% (0.93 → 1.02), while the tenth covered 5.0 °C,
                        # had converged on the truth, and moved it 1.3%. The bucket
                        # finished at 0.988 against a realised 0.947, and every nudge
                        # in between shifted the ETA — most of the residual wobble.
                        #
                        # Recomputing from `_bucket_base_value` makes the update
                        # idempotent with respect to nesting: ten nested samples land
                        # where the last one alone would.
                        if self._bucket_base_bucket != _bi:
                            self._bucket_base_bucket = _bi
                            self._bucket_base_value = self.heat_rate_buckets[_bi]
                        _bp = self._bucket_base_value
                        _span = abs(curr_temp - self._rate_last_temp)
                        _alpha = _EMA_ALPHA * min(1.0, _span / _BUCKET_SPAN_FULL_C)
                        # The outer buckets learn over a bounded span, like the
                        # middle one.
                        #
                        # Left open-ended the hot bucket absorbed the 39-40 tail, where
                        # a session with a 40 °C setpoint spends its slowest hour, and
                        # the chord it settled on described neither half. Bounded at 39
                        # the rate above is extrapolated instead, which the recordings
                        # say costs nothing: across five sessions the final half degree
                        # runs at 1.05x the degree below it, flat within the noise of a
                        # 0.5 °C span. Errors there also fall on the forgiving side —
                        # the water is heating faster than modelled, so the spa is ready
                        # sooner than promised rather than later.
                        #
                        # The cold bucket is bounded at 20 for symmetry. Nothing in the
                        # archive goes below 22, so this only refuses to learn from a
                        # span nobody has recorded.
                        if _in_learn_range:
                            self.heat_rate_buckets[_bi] = (
                                _alpha * rate + (1 - _alpha) * _bp
                            ) if _bp is not None else rate
                            self._session_fresh_buckets.add(_bi)
                        else:
                            _LOGGER.debug(
                                "Heat rate sample %.3f °C/h not learned: %.1f→%.1f °C "
                                "lies outside the %.0f-%.0f learning range",
                                rate, self._rate_last_temp, curr_temp,
                                HEAT_BUCKET_LEARN_MIN, HEAT_BUCKET_LEARN_MAX,
                            )
                        # Track the baseline outdoor temperature under which
                        # rates are being learned, so the ambient correction
                        # knows what "normal" looks like for this spa.  Slow
                        # EMA so it reflects the seasonal norm, not one night.
                        # Seed from a neutral default (not the first, possibly
                        # cold, reading) so a cold night registers as
                        # below-normal immediately rather than after the EMA
                        # slowly drifts up.
                        if self.ambient_temp is not None:
                            base = (
                                self.ambient_baseline
                                if self.ambient_baseline is not None
                                else AMBIENT_BASELINE_DEFAULT
                            )
                            self.ambient_baseline = (
                                AMBIENT_BASELINE_ALPHA * self.ambient_temp
                                + (1 - AMBIENT_BASELINE_ALPHA) * base
                            )
                        # The first bucket to receive data in this session becomes
                        # the scalar source.  Its observations are compared against
                        # the stored base to derive an ambient-condition factor that
                        # is applied to all *other* buckets that haven't been
                        # observed yet this session.
                        if self._session_scalar_bucket is None and _bp is not None:
                            self._session_scalar_bucket = _bi
                        if _bi == self._session_scalar_bucket and _bp is not None:
                            ratio = max(0.5, min(2.0, rate / _bp))
                            self._session_scalar = 0.4 * ratio + 0.6 * self._session_scalar
                            _LOGGER.debug(
                                "Session scalar → %.3f (bucket[%d] ratio actual/base=%.3f)",
                                self._session_scalar, _bi, ratio,
                            )
                        _LOGGER.info(
                            "Heat rate sample %.3f °C/h over %.1f °C / %.1f h "
                            "(EMA → %.3f °C/h, bucket[%d] → %.3f °C/h)",
                            rate, delta, elapsed_hours, self.computed_heat_rate,
                            _bi, self.heat_rate_buckets[_bi],
                        )
                    else:
                        _LOGGER.info(
                            "Heat rate sample %.3f °C/h rejected (out of bounds)", rate
                        )
                # Close the window only when it must be: the water has left the
                # anchor's bucket (the next bucket models a different loss regime
                # and is measured on its own), or the sample was rejected (a bad
                # span must never be re-used).  Otherwise hold the anchor so the
                # span keeps widening and the estimate keeps improving.
                #
                # The zone, not the bucket. Everything below HEAT_BUCKET_LEARN_MIN is one
                # bucket with the 20-30 range, so an anchor set at the start of a cold
                # session sat there until 30 — and in_learning_range tests the *from*
                # temperature, so every sample a session starting at 15 could offer was
                # 15→x and every one was refused, the complete 20→30 traverse included.
                # Refusing a full traverse of the cold bucket because the session began
                # below it throws away the one measurement that stretch exists to make.
                # Re-anchoring at 20 turns it back into an ordinary chord.
                _left_the_band = (learning_anchor_zone(curr_temp)
                                  != learning_anchor_zone(self._rate_last_temp))
                # Recorded whether or not a bucket may learn from it, and that split is
                # deliberate. The 20 °C floor is a *bucket* constraint: the cold bucket
                # is a flat chord over 20-30 and a span from well below it describes
                # something else, so letting one in would distort the rate other sessions
                # depend on. The physical model has no such problem — it regresses rate
                # against the water/air gap, and a span starting at 6 °C is simply a
                # measurement at a large gap, which is the observation it most wants and
                # is least likely to get.
                #
                # This costs one fresh fill to get wrong. Groundwater comes in around
                # 6 °C, so a refill climbs 14 °C below anything the archive contains, in
                # one run, at close to constant outdoor temperature — precisely the water
                # variation that pins `tau` and separates it from the air term. Discarding
                # it because a bucket could not use it would throw away the best evidence
                # this integration will ever see, and there is no second chance until the
                # next refill.
                if _left_the_band and elapsed_hours > 0:
                    # A whole band, entered and left at its edges, measured against the
                    # weather that prevailed while it was crossed. That is the observation
                    # a per-band ambient sensitivity has to be fitted from, and until now
                    # it was computed, used once for the EMA, and discarded: the stored
                    # history kept one ambient temperature per *session*, taken at its
                    # start, which describes a five-hour band not at all.
                    self._record_band_observation(
                        _bi, self._rate_last_temp, curr_temp, elapsed_hours, rate,
                        usable=bool(accepted) and not self._window_disturbed,
                        bucket_learnable=bool(_in_learn_range))
                if not accepted or _left_the_band:
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
                    self._seed_window_ambient(new_band=_left_the_band)
                    # A new window measures a new chord, so the value it recomputes from
                    # must be re-read rather than carried over from the window before it.
                    self._bucket_base_bucket = None
            # else: temperature unchanged — let elapsed time accumulate, don't touch anchor
        else:
            # Heater off/preheat — reset anchor so next on-cycle starts fresh
            # (and the next first crossing is treated as phase-uncertain again).
            self._rate_last_temp = None
            self._bucket_base_bucket = None      # heater off — next window re-bases
            self._rate_last_time = None
            self._rate_prev_temp = None
            self._rate_first_step = False

    def _track_cooling_rate(self, curr_temp, heat_state, now_mono: float) -> None:
        """Sample the passive cooling rate from temperature drops.

        Subject to the same phase-uncertainty rule as heating: the anchor set
        when the heater stops is a 0.5 °C band value at an unknown internal
        position, so the first crossing re-anchors only and learning starts
        from the second.  This matters more here than for heating, because
        thermostat cycling re-arms the anchor on every heater cycle — each
        cycle previously injected one phase-biased (fast) sample into the
        cooling EMA.
        """
        _MIN_COOL_RATE = 0.01  # °C/h — below this is sensor drift
        _MAX_COOL_RATE = 3.0   # °C/h — above this is unusual for an insulated spa

        if heat_state not in (2, _HEAT_STATE_FULL) and curr_temp is not None:
            if self._cool_last_temp is None:
                # First poll in cooling mode — set a phase-uncertain anchor.
                self._cool_last_temp = curr_temp
                self._cool_last_time = now_mono
                self._cool_first_step = True
            elif curr_temp != self._cool_last_temp and self._cool_first_step:
                # First crossing since the heater stopped: position only.
                _LOGGER.info(
                    "Cool rate: first crossing %.1f→%.1f°C after heater-off "
                    "is phase-uncertain — anchored, not learned",
                    self._cool_last_temp, curr_temp,
                )
                self._cool_first_step = False
                self._cool_last_temp = curr_temp
                self._cool_last_time = now_mono
            elif curr_temp != self._cool_last_temp:
                # Temperature has changed — compute rate only for drops.
                elapsed_hours = (now_mono - self._cool_last_time) / 3600
                if elapsed_hours >= _MIN_RATE_SAMPLE_HOURS:
                    delta = curr_temp - self._cool_last_temp
                    if delta < 0:  # temperature actually dropped
                        rate = -delta / elapsed_hours  # positive °C/h
                        if _MIN_COOL_RATE <= rate <= _MAX_COOL_RATE:
                            if self.computed_cool_rate is None:
                                self.computed_cool_rate = rate
                            else:
                                self.computed_cool_rate = (
                                    _EMA_ALPHA * rate
                                    + (1 - _EMA_ALPHA) * self.computed_cool_rate
                                )
                            _LOGGER.info(
                                "Cool rate sample %.3f °C/h (EMA → %.3f °C/h)",
                                rate, self.computed_cool_rate,
                            )
                        else:
                            _LOGGER.debug(
                                "Cool rate sample %.3f °C/h rejected (out of bounds)", rate
                            )
                # Always advance anchor on any temperature change (rise or drop).
                self._cool_last_temp = curr_temp
                self._cool_last_time = now_mono
            # else: temperature unchanged — let elapsed time accumulate
        else:
            # Actively heating — reset cooling anchor so next off-cycle starts
            # fresh (its first crossing will again be phase-uncertain).
            self._cool_last_temp = None
            self._cool_last_time = None
            self._cool_first_step = False

    async def _check_schedule_trigger(
        self, current_temp: float | None, current_target: float | None
    ) -> None:
        """Start the heater autonomously when the schedule window opens.

        Fires at most once per schedule (guarded by _schedule_triggered).
        Skips if the heater is already running at the right temperature.
        Also handles the case where the spa is at or above the schedule target
        (cooling direction): minutes_needed == 0, so start_at == target_utc and
        the trigger fires at the scheduled time to ensure the setpoint and heater
        state are correct for maintenance once the water reaches target.
        """
        if self.scheduled_ready_at is None or self._schedule_triggered:
            return
        if current_temp is None:
            return

        target_temp = self.schedule_target_temp
        target_utc  = dt_util.as_utc(self.scheduled_ready_at)
        now_utc     = dt_util.utcnow()

        # A target well in the past is a stale plan, not a window that just opened.
        # Firing on it starts the heater for a session the user no longer wants —
        # reported 2026-08-08, where editing the schedule's *date* to a past day
        # (the only way to clear it before the Cancel button existed) turned the
        # heater on.  Inside the grace period the existing behaviour stands: a
        # target a few minutes ago should still confirm its setpoint, since that
        # is a window opening rather than an abandoned plan.
        if now_utc - target_utc > _SCHEDULE_STALE_AFTER:
            _LOGGER.info(
                "Heat schedule: target %s is %.1f h in the past — abandoning "
                "without starting the heater",
                target_utc.isoformat(),
                (now_utc - target_utc).total_seconds() / 3600,
            )
            self.clear_schedule("stale target", current_temp)
            return

        # Plan from the in-band estimate, not the quantised reading, so the start
        # ramps instead of lurching a whole band at each crossing.  Falls back to the
        # reading whenever the estimate is not sound — see scheduling_temp.
        plan_temp = self.scheduling_temp()
        if plan_temp is None:
            plan_temp = current_temp
        minutes_needed = self._compute_heating_minutes(plan_temp, target_temp)
        if minutes_needed is None:
            # No learned rate data yet.  If the scheduled time has already arrived,
            # fire immediately with 0 lead time — raising the setpoint now is
            # preferable to missing the window entirely.  If the time hasn't arrived
            # yet we can't know when to start, so wait for rate data.
            if now_utc >= target_utc:
                minutes_needed = 0.0
            else:
                return  # Will retry on next poll

        start_at = target_utc - timedelta(minutes=minutes_needed)

        # Log a meaningful shift in the planned start time (e.g. as the outdoor
        # temperature changes while waiting), so the ambient-driven rescheduling
        # is visible.  Recomputed every poll; only shifts > 15 min are logged.
        prev_start = self._last_computed_start_at
        if prev_start is None or abs((start_at - prev_start).total_seconds()) > 900:
            if prev_start is not None:
                eff = (
                    (self.schedule_target_temp - plan_temp) / (minutes_needed / 60.0)
                    if minutes_needed and plan_temp is not None
                    and self.schedule_target_temp is not None else None
                )
                _LOGGER.info(
                    "Heat schedule: start time moved %s → %s (%.0f min heat @%s, "
                    "outdoor=%s°C, baseline=%s°C)",
                    prev_start.isoformat(timespec="minutes"),
                    start_at.isoformat(timespec="minutes"),
                    minutes_needed,
                    f"{eff:.2f}°C/h" if eff is not None else "n/a",
                    f"{self.ambient_temp:.1f}" if self.ambient_temp is not None else "n/a",
                    f"{self.ambient_baseline:.1f}" if self.ambient_baseline is not None else "n/a",
                )
            self._last_computed_start_at = start_at

        if now_utc < start_at:
            return  # Not yet time

        heater_on = self._last_data.get("heater") == "on"

        _LOGGER.info(
            "Heat schedule: confirming setpoint %.1f°C (water %.1f°C, %.0f min before %s)",
            target_temp, current_temp, minutes_needed, target_utc.isoformat(),
        )
        # Set the flag BEFORE the first await so that if the debounce commit for a
        # new schedule fires during the API call (resetting the flag to False and
        # updating scheduled_ready_at), the new schedule is not accidentally marked
        # as already triggered once the awaits return.
        self._schedule_triggered = True
        # A new heating session is starting — clear the latch so the Ready at
        # sensor counts down to the new target rather than showing "Ready"
        # immediately from a previous session.
        self.ready_latched = False
        self.ready_latched_temp = None
        self.near_target   = False
        try:
            # Always confirm the setpoint at the scheduled time regardless of current
            # state — the setpoint must match the schedule target even when no heating
            # was needed (spa already warm, or warm-day epsilon case).
            await self.api.set_temperature_setting(target_temp)
            if not heater_on:
                await self.set_feature_state("heater", "on")
        except Exception as err:
            self._schedule_triggered = False  # allow retry on next poll
            # A fault does not clear by retrying, so the same message would otherwise
            # land at ERROR on every poll for as long as it persists.  Report each
            # distinct reason once and drop repeats to debug.
            msg = str(err)
            if msg != getattr(self, "_last_schedule_failure", None):
                self._last_schedule_failure = msg
                _LOGGER.error("Heat schedule: failed to start conditioning: %s", err)
            else:
                _LOGGER.debug(
                    "Heat schedule: still failing to start conditioning: %s", err
                )
        else:
            self._last_schedule_failure = None

    def _predictor(self) -> HeatPredictor:
        """The shared prediction model, built from current learned state."""
        return HeatPredictor.from_coordinator(self)

    @property
    def prediction_model(self) -> str:
        """Which model answers the questions the user sees. See CONF_PREDICTION_MODEL."""
        entry = getattr(self, "config_entry", None)
        opts = (getattr(entry, "options", None) or {}) if entry is not None else {}
        return opts.get(CONF_PREDICTION_MODEL, DEFAULT_PREDICTION_MODEL)

    @property
    def uses_frozen_plan(self) -> bool:
        """Whether a session's rates are frozen at the start and revised at band edges.

        True for buckets, and the mechanism exists because a bucket rate is stale by
        construction: it was learned weeks ago under other weather, so the plan is held
        and corrected at the few points where a complete traverse has been measured.

        False for the physical model, which has nothing to freeze. It re-derives from the
        current water temperature and the current outdoor temperature on every poll, so a
        frozen plan would be holding it back rather than steadying it, and a revision
        mechanism would be revising towards what it already says.
        """
        return self.prediction_model != PREDICTION_MODEL_NEWTON

    def heating_minutes(self, from_temp: float, to_temp: float) -> float | None:
        """Minutes to heat between two temperatures, under the selected model.

        **The one production entry point.** Ready at, the Heat schedule sensor and the
        autonomous trigger all arrive here, which is what lets the model be switched
        without moving an entity: the sensors keep their ids and their meaning and only
        the arithmetic changes.

        Newton falls back to buckets whenever it declines — no fit yet, or an asymptote
        at or below the target. The diagnostic shadow sensors deliberately leave a gap in
        that case because the gap is the finding; this path must not, because a blank
        Ready at is a broken dashboard. The fallback is logged on the way in and on the
        way out, never per poll.
        """
        if self.prediction_model == PREDICTION_MODEL_NEWTON:
            minutes = self.newton_minutes(from_temp, to_temp)
            if minutes is not None:
                if self._newton_fallback_active:
                    self._newton_fallback_active = False
                    _LOGGER.info(
                        "Prediction model: the physical model can answer again "
                        "(%.1f→%.1f °C) — no longer falling back to buckets",
                        from_temp or 0.0, to_temp or 0.0)
                return minutes
            if not self._newton_fallback_active:
                self._newton_fallback_active = True
                fit = self.newton_fit()
                _LOGGER.info(
                    "Prediction model: the physical model declines %.1f→%.1f °C "
                    "(%s) — falling back to buckets",
                    from_temp or 0.0, to_temp or 0.0,
                    "no fit yet" if fit is None else
                    f"asymptote {(self.ambient_temp or 0.0) + fit['asymptote_lift_c']:.1f} °C "
                    f"at outdoor {self.ambient_temp}",
                )
        return self._predictor().heating_minutes(from_temp, to_temp)

    def _compute_heating_minutes(self, from_temp: float, to_temp: float) -> float | None:
        """Heating time (minutes) — delegates to the selected model.

        Was a second implementation of the sensor's calculation, kept in sync by
        hand and documented as mirroring it "to avoid a circular import" that did
        not exist.  They had drifted; see predictor.py.
        """
        return self.heating_minutes(from_temp, to_temp)

    def _heating_minutes_variant(self, from_temp, to_temp, *, use_fits: bool):
        """The same estimate, with the learned weather response forced on or off.

        Both are recorded for every session so the correction can be judged on finished
        sessions rather than on argument — and judged retroactively, without anyone
        having had to enable a diagnostic beforehand. The user's own setting decides
        which one is *shown*; this decides nothing.
        """
        p = HeatPredictor.from_coordinator(self)
        p.band_fits = self.band_fits(force=True) if use_fits else {}
        return p.heating_minutes(from_temp, to_temp)

    # Map of features to their respective API methods
    FEATURE_API_MAP = {
        "heater": "set_heater_state",
        "filter": "set_filter_state",
        "bubble": "set_bubble_state",
        "jet": "set_jet_state",
        "ozone": "set_ozone_state",
        "uvc": "set_uvc_state",
    }

    async def _confirm_feature(self, feature: str, want: str) -> bool:
        """Poll the spa until `feature` really reads `want`.

        A SUCCESS response says the command was accepted for delivery, not that the
        hardware acted on it.  That distinction is the whole point of the heater start
        sequence: the pump must actually be *running* before the heater is commanded,
        and we must *know* the heater came on rather than assume it and count down to a
        session that never began.

        Bounded by RAPID_POLL_TIMEOUT at RAPID_SCAN_INTERVAL — the same budget the
        background confirmation logic already allows for a change to appear, so a
        timeout here means the same thing it means there.

        Returns True on confirmation and False on timeout rather than raising, because
        the two callers want different things from a failure: a pump that never starts
        must stop the sequence, while an unconfirmed heater has to be reported but has
        already been commanded.
        """
        raw_key = _PENDING_TO_RAW_KEY.get(feature)
        if raw_key is None:
            return True                          # nothing to compare against
        want_num = 1 if want == "on" else 0
        deadline = time.monotonic() + RAPID_POLL_TIMEOUT
        while True:
            try:
                status = await self.api.get_hot_tub_status()
            except Exception as err:              # a dropped poll is not a verdict
                _LOGGER.debug("Confirm %s=%s: status read failed: %s",
                              feature, want, err)
                status = None
            if isinstance(status, dict):
                try:
                    if int(status.get(raw_key)) == want_num:
                        self._last_data[feature] = want
                        return True
                except (TypeError, ValueError):
                    pass
            if time.monotonic() >= deadline:
                _LOGGER.warning(
                    "Confirm %s=%s: spa did not report the change within %s s",
                    feature, want, RAPID_POLL_TIMEOUT,
                )
                return False
            await asyncio.sleep(RAPID_SCAN_INTERVAL)

    async def _start_pump_before_heater(self) -> bool:
        """Bring the circulation pump up before the heater is commanded.

        **Not** about avoiding an F1 fault — F1 comes from starting the pump into a
        physical problem, and this starts the pump, so it cannot help there. The reason
        is command acceptance:

        * A heater command issued while the pump is off can simply be **refused** by
          the spa. The integration would then believe heating had begun when it had
          not, and Ready at would count down to a session that never started.
        * Asserting `heater_state` and `filter_state` in a **single payload** is
          untested on this hardware. Two separate commands in a known order, with the
          first acknowledged before the second is sent, keeps us on ground we have
          actually observed.

        Hence the ordering and the settle delay, rather than one combined call.

        Returns True when a pump command was actually sent, so the caller can fold the
        filter into the rapid-poll expectations.  Note it returns False — no command,
        nothing to wait for — when the pump is already running, which is the normal
        case for anyone following the README's advice to leave it on.

        Raises if the pump will not start, deliberately, because commanding the heater
        is then the one thing we must not do.  Callers surface it: the scheduler clears
        its trigger flag and retries on the next poll, and the climate and switch paths
        raise to the UI.
        """
        if self._last_data.get("filter") == "on":
            return False        # already circulating — nothing to do

        _LOGGER.info("Heater requested: starting the circulation pump first")
        response = await self.api.set_filter_state(1)
        # send_device_command returns the parsed payload; anything other than
        # SUCCESS means the spa did not accept it.
        if not (isinstance(response, dict) and response.get("message") == "SUCCESS"):
            raise HomeAssistantError(
                "The spa refused to start the circulation pump, so the heater was "
                "not switched on. Check the pump and filter, then try again."
            )
        # SUCCESS is delivery, not action.  Wait until the spa itself reports the pump
        # running before the heater is commanded at all.
        if not await self._confirm_feature("filter", "on"):
            raise HomeAssistantError(
                f"The circulation pump did not start within {RAPID_POLL_TIMEOUT} "
                "seconds, so the heater was not switched on. Check the pump and "
                "filter, then try again."
            )
        self._last_data["filter"] = "on"
        # A short pause after confirmation, for flow to stabilise before the heater
        # loads it.  This used to stand in for confirmation; now it is only what it
        # says.
        if _PUMP_SETTLE_SECONDS:
            await asyncio.sleep(_PUMP_SETTLE_SECONDS)
        return True

    async def set_feature_state(self, feature: str, state: str) -> None:
        """Set a feature state using the API map."""
        # INFO, not DEBUG: these are rare, consequential, and externally triggered.
        # On 2026-08-12 a `filter: off` landed 2.5 min before a scheduled heat start
        # and the only evidence was the *retry* warning 15 s later — the command
        # itself was invisible, so there was nothing to correlate against.
        _LOGGER.info("Command: set %s → %s (currently %s)",
                     feature, state, self._last_data.get(feature))
        try:
            if state.lower() not in ["on", "off"]:
                raise ValueError("State must be 'on' or 'off'")
            # Normalise once and use it everywhere.  The expectation below used to keep
            # the caller's original casing while the poll reports lowercase, so
            # `mspa.set_filter` with `state: "OFF"` sent the command correctly and then
            # waited for a value that could never arrive — producing a "Spa did not
            # confirm" warning for a command the spa had actually accepted.
            state = state.lower()
            numerical_state = 1 if state == "on" else 0

            # Refuse to switch things *on* while the spa reports a fault, and say why
            # in the UI rather than only in the log.  Switching *off* stays allowed:
            # shutting a faulting spa down is exactly what a user needs to be able to
            # do, and blocking that would be the worse failure.
            fault = self.fault_code
            if fault and numerical_state == 1:
                raise ServiceValidationError(
                    f"The spa is reporting fault {fault}, so {feature} was not "
                    f"switched on. Clear the fault at the control box first."
                )
            api_method = getattr(self.api, self.FEATURE_API_MAP[feature])

            # Heating without flow is refused by the spa, so the pump leads.
            pump_started = False
            if feature == "heater" and numerical_state == 1:
                pump_started = await self._start_pump_before_heater()

            # Build raw command for potential retry
            if feature == "bubble":
                bubble_level = max(1, self._last_data.get("bubble_level") or 1)
                self._last_data["bubble_level"] = bubble_level
                await api_method(numerical_state, bubble_level)
                raw_command = {"bubble_state": numerical_state, "bubble_level": bubble_level}
            else:
                response = await api_method(numerical_state)
                # The response was previously discarded, so a command the spa *refused*
                # still armed the confirmation wait and surfaced 15 s later as "Spa did
                # not confirm" — indistinguishable from a command that was accepted and
                # simply slow. Say which it was. The retry still runs, since a refusal
                # can be transient.
                if not (isinstance(response, dict)
                        and response.get("message") == "SUCCESS"):
                    _LOGGER.warning(
                        "Command: spa did not accept %s → %s (response: %r) — the "
                        "confirmation wait that follows may never be satisfied",
                        feature, state, response,
                    )
                raw_key = _PENDING_TO_RAW_KEY.get(feature)
                raw_command = {raw_key: numerical_state} if raw_key else {}

            # Turning off the filter also forces the heater off (handled in the API
            # layer).  Register that implicit change here so _pending_changes stays
            # consistent and the retry payload doesn't accidentally resend a stale
            # heater-on command from an earlier user action.
            expected_changes = {feature: state}
            if feature == "filter" and numerical_state == 0:
                expected_changes["heater"] = "off"
                raw_command["heater_state"] = 0
            # A soft start changed the filter too, so register it to keep the rapid
            # poll's expectations honest.
            #
            # Deliberately NOT added to raw_command.  That payload is resent whole as a
            # single device command if the spa does not confirm, so including the pump
            # here would retry `{heater_state: 1, filter_state: 1}` together — the one
            # combination the soft start exists to avoid, and untested on this
            # hardware.  A bare heater-on is the right retry: the pump was already
            # commanded and acknowledged with SUCCESS before the heater was touched.
            if pump_started:
                expected_changes["filter"] = "on"

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(expected_changes, raw_command)
            await self.async_request_refresh()

            # Close the loop on a heater start: pump on, confirmed, heater on, and now
            # confirmed too.  Without this a refused heater command reads as success and
            # Ready at counts down to a session that never began.  Only the heater is
            # checked synchronously — the other features have no ordering requirement
            # and the background retry already covers them.
            if feature == "heater" and numerical_state == 1:
                if not await self._confirm_feature("heater", "on"):
                    raise HomeAssistantError(
                        "The spa accepted the heater command but has not reported the "
                        f"heater running within {RAPID_POLL_TIMEOUT} seconds. The pump "
                        "is on; check the spa and try again."
                    )
        except Exception as err:
            _LOGGER.error("Failed to set %s to %s: %s", feature, state, str(err))
            raise

    async def set_temperature(self, service: ServiceCall) -> None:
        """Set the target temperature."""
        try:
            temperature = service.data.get(ATTR_TEMPERATURE)
            _LOGGER.debug("Setting temperature to %s", temperature)
            await self.api.set_temperature_setting(temperature)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"target_temperature": temperature},
                {"temperature_setting": int(temperature * 2)},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set temperature: %s", str(err))
            raise

    async def set_bubble(self, service: ServiceCall) -> None:
        """Set the bubble state."""
        try:
            bubble_state = service.data.get(ATTR_STATE)
            _LOGGER.debug("Setting bubble state to %s", bubble_state)
            numerical_state = 1 if bubble_state.lower() == "on" else 0
            bubble_level = max(1, self._last_data.get("bubble_level") or 1)
            await self.api.set_bubble_state(numerical_state, bubble_level)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"bubble": bubble_state},
                {"bubble_state": numerical_state, "bubble_level": bubble_level},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set bubble state: %s", str(err))
            raise

    async def set_bubble_level(self, service: ServiceCall) -> None:
        try:
            bubble_level = service.data.get("level")
            _LOGGER.debug("Setting bubble level to %s", bubble_level)
            self._last_data["bubble_level"] = bubble_level
            # Setting a level activates the bubbles on the device — send state+level
            # together so HA's pending-change tracking stays consistent.
            await self.api.set_bubble_state(1, bubble_level)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"bubble_level": bubble_level, "bubble": "on"},
                {"bubble_state": 1, "bubble_level": bubble_level},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set bubble level: %s", str(err))
            raise

    async def set_temperature_unit(self, unit: int) -> None:
        """Set temperature unit (0=Celsius, 1=Fahrenheit)."""
        try:
            _LOGGER.debug("Setting temperature unit to %s", unit)
            await self.api.set_temperature_unit(unit)

            # Enable rapid polling to quickly detect the change
            self._enable_rapid_polling(
                {"temperature_unit": unit},
                {"temperature_unit": unit},
            )
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set temperature unit: %s", str(err))
            raise

    # Generic service handler for features
    async def handle_feature_service(self, service: ServiceCall) -> None:
        """Handle mspa.set_<feature>, and record who called it.

        This is the attribution hole the 2026-08-12 investigation fell into. A caller
        using `mspa.set_filter` references neither the switch entity nor the climate
        entity, so searching automations and scripts for those entities finds nothing —
        and the switch-level caller tracing never fires either, because no switch is
        involved. The ServiceCall carries the context directly, which is better
        evidence than the entity's copy of it.
        """
        feature = service.service.replace("set_", "")
        state = service.data.get(ATTR_STATE)
        ctx = getattr(service, "context", None)
        _LOGGER.info(
            "Service mspa.%s called: %s → %s (user_id=%s, parent_id=%s)",
            service.service, feature, state,
            getattr(ctx, "user_id", None), getattr(ctx, "parent_id", None),
        )
        await self.set_feature_state(feature, state)

    # Register these as service handlers in __init__.py:
    # set_heater, set_filter, set_bubble, set_jet, set_ozone, set_uvc
    set_heater = handle_feature_service
    set_filter = handle_feature_service
    # set_bubble = handle_feature_service
    set_jet = handle_feature_service
    set_ozone = handle_feature_service
    set_uvc = handle_feature_service

    async def _check_adaptive_polling(self, data: dict) -> None:
        """3-tier adaptive polling: idle → active → rapid.

        Tiers:
          RAPID (1s, 15s window)  — after sending a command, to confirm it took effect
          EXTERNAL (5s, 15s)     — after detecting an unexpected state change (panel/app)
          ACTIVE (30s)           — something is running (heater, filter, bubble, jet)
          IDLE (120s)            — nothing running and state stable for 10+ minutes

        Transitions between active/idle happen automatically based on device state.
        Rapid/external are time-boxed bursts that fall back to active/idle when done.
        """
        current_time = time.monotonic()

        # Keys confirmed on *this* poll.  Phase 1 removes them from _pending_changes,
        # so without this Phase 4 would see a change it can no longer attribute and
        # report every commanded change as external.
        just_confirmed: set[str] = set()

        # --- Phase 1: Handle pending command confirmations (rapid tier) ---
        if self._pending_changes:
            confirmed = []
            for key, expected_value in list(self._pending_changes.items()):
                if data.get(key) == expected_value:
                    _LOGGER.debug("Pending change confirmed: %s = %s", key, expected_value)
                    confirmed.append(key)

            just_confirmed.update(confirmed)
            for key in confirmed:
                del self._pending_changes[key]
                raw_key = _PENDING_TO_RAW_KEY.get(key)
                if raw_key:
                    self._pending_raw_command.pop(raw_key, None)

            if confirmed and not self._pending_changes:
                self._command_retry_count = 0

        # --- Phase 2: Check rapid poll timeout / retry ---
        if self._rapid_poll_until and time.time() > self._rapid_poll_until:
            if self._pending_changes:
                if self._command_retry_count < 1 and self._pending_raw_command:
                    self._command_retry_count += 1
                    _LOGGER.warning(
                        "Spa did not confirm %s — retrying command %s (attempt %d)",
                        self._pending_changes,
                        self._pending_raw_command,
                        self._command_retry_count,
                    )
                    try:
                        await self.api.send_device_command(self._pending_raw_command)
                    except Exception as err:
                        _LOGGER.error("Command retry failed: %s", err)
                    self._rapid_poll_until = time.time() + RAPID_POLL_TIMEOUT
                else:
                    _LOGGER.warning(
                        "Spa did not confirm changes after retry: %s — giving up",
                        self._pending_changes,
                    )
                    self._pending_changes.clear()
                    self._pending_raw_command.clear()
                    self._command_retry_count = 0
                    self._rapid_poll_until = None
            else:
                self._rapid_poll_until = None

        # --- Phase 3: Check external-change timeout ---
        if self._external_change_until and time.time() > self._external_change_until:
            self._external_change_until = None

        # --- Phase 4: Detect unexpected external state changes ---
        # Compare control keys against last snapshot; if something changed that
        # we didn't command, someone used the physical panel or MSpa Link app.
        _CONTROL_KEYS = ("heater", "filter", "bubble", "jet", "ozone", "uvc", "target_temperature")
        if self._last_snapshot:
            # Report *every* key that moved, and whether we asked for it.
            #
            # Two blind spots made the 2026-08-12 filter-off untraceable. This ran only
            # when nothing was pending, so any change arriving inside a command's
            # confirmation window was invisible — exactly when a cascade happens. And
            # it broke after the first key, so a heater and filter moving together
            # reported only the heater, which is precisely the cascade's signature.
            #
            # Changes we did ask for are logged too, at debug, so the sequence is
            # complete: reading "commanded" next to "not commanded by HA" is what
            # separates our own cascade from the unit acting on its own.
            unexpected = False
            for key in _CONTROL_KEYS:
                old_val = self._last_snapshot.get(key)
                new_val = data.get(key)
                if old_val is None or new_val is None or old_val == new_val:
                    continue
                if key in just_confirmed or self._pending_changes.get(key) == new_val:
                    _LOGGER.debug("Confirmed change: %s %s → %s (commanded)",
                                  key, old_val, new_val)
                    continue
                _LOGGER.info(
                    "External change detected: %s changed %s → %s (not commanded by HA)",
                    key, old_val, new_val,
                )
                unexpected = True
            if unexpected:
                self._external_change_until = time.time() + EXTERNAL_CHANGE_TIMEOUT
                self._last_state_change_time = current_time

        # --- Phase 5: Track heat state transitions for logging ---
        current_heat_state = data.get("heat_state")
        if self._last_heat_state != current_heat_state:
            # INFO: infrequent, and it is the spa telling us what it is doing.  These
            # transitions were the most informative lines in the 2026-08-12
            # investigation and were only available at debug, which was not on.
            _LOGGER.info(
                "Heat state: %s → %s",
                _HEAT_STATE_NAMES.get(self._last_heat_state, self._last_heat_state),
                _HEAT_STATE_NAMES.get(current_heat_state, current_heat_state),
            )
            self._last_heat_state = current_heat_state
            self._last_state_change_time = current_time

        # --- Phase 6: Determine appropriate tier and set interval ---
        if self._rapid_poll_until or self._pending_changes:
            # RAPID tier: confirming a command we sent
            target_interval = RAPID_SCAN_INTERVAL
            tier = "rapid"
        elif self._external_change_until:
            # EXTERNAL tier: someone used the panel/app, watch for follow-up changes
            target_interval = EXTERNAL_CHANGE_INTERVAL
            tier = "external"
        elif self._is_spa_active(data):
            # ACTIVE tier: heater, filter or bubble/jet running
            target_interval = ACTIVE_SCAN_INTERVAL
            tier = "active"
            self._last_state_change_time = current_time
        else:
            # IDLE tier: nothing running, check if stable long enough
            time_since_change = current_time - self._last_state_change_time if self._last_state_change_time else IDLE_STABLE_THRESHOLD + 1
            if time_since_change >= IDLE_STABLE_THRESHOLD:
                target_interval = IDLE_SCAN_INTERVAL
                tier = "idle"
            else:
                # Recently stopped — stay at active rate briefly
                target_interval = ACTIVE_SCAN_INTERVAL
                tier = "active"

        # Only log and update when the interval actually changes
        current_interval = self.update_interval.total_seconds()
        if current_interval != target_interval:
            self.update_interval = timedelta(seconds=target_interval)
            _LOGGER.info("Polling tier: %s (%ds interval)", tier, target_interval)

    @staticmethod
    def _is_spa_active(data: dict) -> bool:
        """Return True if any spa component is actively running."""
        return (
            data.get("heater") == "on"
            or data.get("filter") == "on"
            or data.get("bubble") == "on"
            or data.get("jet") == "on"
        )

    def _enable_rapid_polling(self, expected_changes: dict, raw_command: dict = None) -> None:
        """Enable rapid polling and track expected changes."""
        self._pending_changes.update(expected_changes)
        if raw_command:
            self._pending_raw_command.update(raw_command)
            self._command_retry_count = 0
        self._rapid_poll_until = time.time() + RAPID_POLL_TIMEOUT
        self.update_interval = timedelta(seconds=RAPID_SCAN_INTERVAL)
        _LOGGER.debug("Rapid polling enabled, waiting for changes: %s", expected_changes)

    async def _check_power_cycle(self, data: dict) -> None:
        """Check for power cycle and restore state if enabled.
        
        Uses multiple detection methods:
        1. is_online transition from False to True
        2. Multiple simultaneous parameter changes indicating a reset
        3. Temperature unit reverting to default (typically F/1)
        """
        current_is_online = data.get("is_online", True)
        power_cycle_detected = False
        detection_method = ""
        
        # Method 1: Track is_online transitions
        if self._last_is_online is not None:
            # Detect power off transition (True → False)
            if self._last_is_online and not current_is_online:
                _LOGGER.info("🔌 MSpa power OFF detected (is_online: True → False)")
                # Save current state before power off
                self._saved_state = {
                    "heater": data.get("heater"),
                    "target_temperature": data.get("target_temperature"),
                    "filter": data.get("filter"),
                    "temperature_unit": data.get("temperature_unit"),
                    "ozone": data.get("ozone"),
                    "uvc": data.get("uvc"),
                }
                _LOGGER.info(f"💾 Saved state for restoration: {self._saved_state}")
            
            # Detect power on transition (False → True)
            elif not self._last_is_online and current_is_online:
                power_cycle_detected = True
                detection_method = "is_online transition (False → True)"
                _LOGGER.info(f"⚡ MSpa power ON detected via {detection_method}")
        
        # Method 2: Detect multiple simultaneous changes suggesting a reset
        # This helps catch quick power cycles that we might miss with is_online
        if self._last_snapshot and not power_cycle_detected:
            changes_detected = []
            
            # Check for key parameters reverting to defaults
            if self._last_snapshot.get("temperature_unit") == 0 and data.get("temperature_unit") == 1:
                changes_detected.append("temp_unit_reset_to_F")
            
            if self._last_snapshot.get("heater") == "on" and data.get("heater") == "off":
                changes_detected.append("heater_off")
            
            if self._last_snapshot.get("filter") == "on" and data.get("filter") == "off":
                changes_detected.append("filter_off")
            
            if self._last_snapshot.get("ozone") == "on" and data.get("ozone") == "off":
                changes_detected.append("ozone_off")
            
            if self._last_snapshot.get("uvc") == "on" and data.get("uvc") == "off":
                changes_detected.append("uvc_off")
            
            # If multiple things turned off simultaneously, it's likely a power cycle
            if len(changes_detected) >= 2:
                power_cycle_detected = True
                detection_method = f"multiple simultaneous changes: {', '.join(changes_detected)}"
                _LOGGER.warning(f"⚡ Possible power cycle detected via {detection_method}")
                _LOGGER.info("💡 TIP: If this is a false positive, please report it with the changes detected")
        
        # Store current state as snapshot for next comparison
        self._last_snapshot = {
            "temperature_unit": data.get("temperature_unit"),
            "heater": data.get("heater"),
            "filter": data.get("filter"),
            "ozone": data.get("ozone"),
            "uvc": data.get("uvc"),
            "target_temperature": data.get("target_temperature"),
        }
        
        # Handle power cycle restoration
        if power_cycle_detected:
            self._power_cycle_detected = True
            
            # Check config options
            track_unit = self.config_entry.options.get(CONF_TRACK_TEMPERATURE_UNIT, False)
            restore_enabled = self.config_entry.options.get(CONF_RESTORE_STATE, False)
            
            _LOGGER.info(f"🔧 Config: track_temperature_unit={track_unit}, restore_state={restore_enabled}")
            
            # Handle temperature unit tracking (independent of restore_state)
            if track_unit:
                # Set temperature unit based on HA unit system
                ha_unit = self.hass.config.units.temperature_unit
                desired_unit = 1 if ha_unit == UnitOfTemperature.FAHRENHEIT else 0
                current_unit = data.get("temperature_unit", 0)
                
                if current_unit != desired_unit:
                    unit_name = "Fahrenheit" if desired_unit == 1 else "Celsius"
                    _LOGGER.info(f"🌡️ Setting MSpa temperature unit to {unit_name} to match HA system")
                    try:
                        await self.set_temperature_unit(desired_unit)
                    except Exception as err:
                        _LOGGER.error(f"❌ Failed to set temperature unit: {err}")
            
            # Handle state restoration (independent of track_unit)
            if restore_enabled:
                if self._saved_state:
                    _LOGGER.info("♻️ Starting state restoration after power cycle")
                    await self._restore_saved_state()
                else:
                    _LOGGER.warning("⚠️ No saved state available for restoration (device may have been off during HA restart)")
        
        # Update last is_online state
        self._last_is_online = current_is_online
    
    async def _restore_saved_state(self) -> None:
        """Restore saved state after power cycle."""
        try:
            restored_items = []
            failed_items = []
            
            # Small delay to allow temperature unit to be set first
            await asyncio.sleep(2)
            
            _LOGGER.info(f"♻️ Restoring state from: {self._saved_state}")
            
            # Restore target temperature if saved
            if "target_temperature" in self._saved_state:
                temp = self._saved_state["target_temperature"]
                try:
                    _LOGGER.info(f"🌡️ Restoring target temperature to {temp}°C")
                    await self.api.set_temperature_setting(temp)
                    restored_items.append(f"temperature={temp}°C")
                    await asyncio.sleep(0.5)  # Small delay between commands
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore temperature: {err}")
                    failed_items.append(f"temperature: {err}")
            
            # Restore heater state if saved and was on
            if self._saved_state.get("heater") == "on":
                try:
                    _LOGGER.info("♨️ Restoring heater to ON")
                    await self.set_feature_state("heater", "on")
                    restored_items.append("heater=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore heater: {err}")
                    failed_items.append(f"heater: {err}")
            
            # Restore filter state if saved and was on
            if self._saved_state.get("filter") == "on":
                try:
                    _LOGGER.info("💨 Restoring filter to ON")
                    await self.set_feature_state("filter", "on")
                    restored_items.append("filter=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore filter: {err}")
                    failed_items.append(f"filter: {err}")
            
            # Restore ozone if saved and was on
            if self._saved_state.get("ozone") == "on":
                try:
                    _LOGGER.info("🫧 Restoring ozone to ON")
                    await self.set_feature_state("ozone", "on")
                    restored_items.append("ozone=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore ozone: {err}")
                    failed_items.append(f"ozone: {err}")
            
            # Restore UVC if saved and was on
            if self._saved_state.get("uvc") == "on":
                try:
                    _LOGGER.info("💡 Restoring UVC to ON")
                    await self.set_feature_state("uvc", "on")
                    restored_items.append("uvc=ON")
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.error(f"❌ Failed to restore UVC: {err}")
                    failed_items.append(f"uvc: {err}")
            
            # Summary
            if restored_items:
                _LOGGER.info(f"✅ State restoration completed: {', '.join(restored_items)}")
            if failed_items:
                _LOGGER.error(f"❌ Failed to restore some items: {', '.join(failed_items)}")
            if not restored_items and not failed_items:
                _LOGGER.info("ℹ️ No items to restore (all were OFF)")
            
        except Exception as err:
            _LOGGER.error(f"❌ State restoration failed with error: {err}", exc_info=True)
    
    async def _enforce_temperature_unit(self, data: dict) -> None:
        """Enforce temperature unit to match HA system if always_enforce_unit is enabled.
        
        This is useful for devices that forget temperature unit setting even without a full power cycle.
        """
        always_enforce = self.config_entry.options.get(CONF_ALWAYS_ENFORCE_UNIT, False)
        
        if not always_enforce:
            return
        
        # Don't enforce immediately after a detected power cycle (already handled there)
        if self._power_cycle_detected:
            self._power_cycle_detected = False
            return
        
        # Get desired unit from HA system
        ha_unit = self.hass.config.units.temperature_unit
        desired_unit = 1 if ha_unit == UnitOfTemperature.FAHRENHEIT else 0
        current_unit = data.get("temperature_unit", 0)
        
        # Only enforce if there's a mismatch
        if current_unit != desired_unit:
            unit_name = "Fahrenheit" if desired_unit == 1 else "Celsius"
            _LOGGER.info(f"🌡️ Enforcing temperature unit: {unit_name} (always_enforce_unit enabled)")
            try:
                await self.set_temperature_unit(desired_unit)
            except Exception as err:
                _LOGGER.error(f"❌ Failed to enforce temperature unit: {err}")
    
    @property
    def last_data(self) -> dict:
        return self._last_data
