"""DataUpdateCoordinator for MSpa integration."""
import logging
from datetime import timedelta, datetime, timezone
from .mspa_api import MSpaApiClient
from .predictor import (
    HeatPredictor,
    ShadowPlan,
    ambient_rate_factor,
    bucket_index,
    extrapolate_within_band,
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

# The live ETA holds the opening estimate until BOTH of these are met, then replans
# at every 0.5 °C crossing against rates frozen at session start.  Simulated over the
# four recorded sessions in analysis/settle_time.py: replanning converges to 0 min at
# the finish where holding carries its opening error all the way in (10 min mean, 35
# on the worst session).  Before the settle point the opposite is true — a recompute
# from a partial span is badly wrong, and the opening crossings measure band position
# rather than heating — so each is used where it wins.
_PLAN_SETTLE_MINUTES = 90.0
_PLAN_SETTLE_DEGREES = 1.5

# How far the water may cool below the temperature at which it latched before
# "Ready" is withdrawn.  The latch advertises "still warm enough to use
# without waiting"; once the water has given up this much heat that claim is
# no longer credible, however the thermostat happens to be set.  Larger than
# _NEW_SESSION_DELTA so a lowered thermostat followed by ordinary cycling
# doesn't withdraw Ready prematurely.
_LATCH_COOL_OFF = 3.0             # °C

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
        self._bucket_base_bucket: int | None = None
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
            if (self.near_target and self._prediction is not None
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
            # Clear prediction if target changed mid-session.
            if (self._prediction is not None
                    and new_target is not None
                    and new_target != self._prediction.get("target_temp")):
                _LOGGER.debug("Prediction cancelled — target changed from %.1f to %.1f",
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
                "prediction_bias": self.prediction_bias,
                "active_prediction": self._prediction,
                # Persisted so a restart mid-scheduled-heating resumes as
                # "Heating" instead of dropping back to pending and re-firing.
                "schedule_triggered": self._schedule_triggered,
                "ambient_baseline": self.ambient_baseline,
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

        if delta < _NEAR_TARGET_DEACTIVATE:
            if not self.near_target:        # latch on the False→True transition only
                self.ready_latched = True
                # Remember how warm the water was when it latched, so the cool-off
                # release below can tell how much heat has since been given up.
                self.ready_latched_temp = new_temp
                _LOGGER.debug(
                    "ready_latched set (near_target True, shortfall=%.2f°C)", delta)
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
                    "Ready at: plan revised at %.1f °C — rates now %s, ready %s",
                    new_temp,
                    "/".join(f"{r:.2f}" for r in shadow.rates),
                    shadow.eta.isoformat(timespec="minutes"),
                )
        self.temp_anchor_time     = now
        self.temp_anchor_temp     = anchored
        self.temp_anchor_target   = new_target
        self.temp_anchor_rising   = rising
        self._anchor_prev_reading = new_temp

    def shadow_eta(self):
        """Ready time from the shadow curve, or None outside a session.

        Revised twice in a typical session rather than eighteen times, and within about
        ten minutes from a quarter of the way in — see predictor.ShadowPlan.
        """
        shadow = getattr(self, "_shadow", None)
        return shadow.eta if shadow is not None else None

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
                    return
                # Temperature has changed — elapsed time since the window anchor
                # is the true duration of the whole boundary-to-boundary span.
                elapsed_hours = (now_mono - self._rate_last_time) / 3600
                anchor_bucket = _heat_bucket_index(self._rate_last_temp)
                accepted = False
                if elapsed_hours >= _MIN_RATE_SAMPLE_HOURS:
                    delta = curr_temp - self._rate_last_temp
                    rate = delta / elapsed_hours  # guarded: elapsed_hours > 0
                    if _MIN_HEAT_RATE <= rate <= _MAX_HEAT_RATE:
                        accepted = True
                        if self.computed_heat_rate is None:
                            self.computed_heat_rate = rate
                        else:
                            self.computed_heat_rate = (
                                _EMA_ALPHA * rate
                                + (1 - _EMA_ALPHA) * self.computed_heat_rate
                            )
                        # Also update the temperature bucket for this observation.
                        # The window anchor is the *start* temperature of the span.
                        _bi = anchor_bucket
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
                        self.heat_rate_buckets[_bi] = (
                            _alpha * rate + (1 - _alpha) * _bp
                        ) if _bp is not None else rate
                        self._session_fresh_buckets.add(_bi)
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
                if not accepted or _heat_bucket_index(curr_temp) != anchor_bucket:
                    self._rate_last_temp = curr_temp
                    self._rate_last_time = now_mono
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

    def _compute_heating_minutes(self, from_temp: float, to_temp: float) -> float | None:
        """Heating time (minutes) — delegates to the shared model.

        Was a second implementation of the sensor's calculation, kept in sync by
        hand and documented as mirroring it "to avoid a circular import" that did
        not exist.  They had drifted; see predictor.py.
        """
        return self._predictor().heating_minutes(from_temp, to_temp)

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
