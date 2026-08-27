"""Constants for the MSpa Hot Tub integration."""
from homeassistant.const import  UnitOfTemperature

DOMAIN = "mspa"
DEFAULT_SCAN_INTERVAL = 60
IDLE_SCAN_INTERVAL = 120  # Polling interval when spa is idle (nothing running, stable)
ACTIVE_SCAN_INTERVAL = 30  # Polling interval when spa is active (heater/filter on)
RAPID_SCAN_INTERVAL = 1  # Polling interval in seconds when waiting for changes
RAPID_POLL_TIMEOUT = 15  # Maximum time in seconds to poll rapidly
EXTERNAL_CHANGE_INTERVAL = 5  # Polling interval after detecting an external change
EXTERNAL_CHANGE_TIMEOUT = 15  # How long to rapid-poll after an external change
IDLE_STABLE_THRESHOLD = 600  # Seconds of unchanged state before entering idle tier

# Configuration
CONF_PRODUCT_ID = "product_id"
CONF_REGION = "region"
CONF_TRACK_TEMPERATURE_UNIT = "track_temperature_unit"
CONF_RESTORE_STATE = "restore_state"
CONF_ALWAYS_ENFORCE_UNIT = "always_enforce_unit"

# Region configuration
# ROW = Rest of World (Europe, Africa, Middle East, Oceania, etc.)
# US = United States and Canada
# CH = China mainland
DEFAULT_REGION = "ROW"  # Safe default for European operation

REGIONS = {
    "ROW": "Automatic (Europe/Rest of World)",
    "US": "United States",
    "CH": "China"
}

# API Base URLs by region
API_ENDPOINTS = {
    "ROW": "https://api.iot.the-mspa.com",
    "US": "https://api.usiot.the-mspa.com",
    "CH": "https://api.mspa.mxchip.com.cn"
}

# Country code to region mapping for automatic detection
# This ensures proper regional routing based on user's country
COUNTRY_TO_REGION = {
    # North America - US Servers
    "US": "US",  # United States
    "CA": "US",  # Canada
    "MX": "US",  # Mexico (often routes through US)
    
    # China - China Servers
    "CN": "CH",  # China
    "HK": "CH",  # Hong Kong
    "MO": "CH",  # Macau
    
    # All other countries default to ROW (Europe-based servers)
    # This includes:
    # - Europe: AT, BE, BG, HR, CY, CZ, DK, EE, FI, FR, DE, GR, HU, IE, IT, 
    #   LV, LT, LU, MT, NL, PL, PT, RO, SK, SI, ES, SE, GB, NO, CH, IS, etc.
    # - Middle East: AE, SA, IL, etc.
    # - Africa: ZA, NG, KE, etc.
    # - Oceania: AU, NZ, etc.
    # - Asia (non-China): JP, KR, SG, TH, MY, IN, etc.
    # - South America: BR, AR, CL, etc.
    #
    # Note: ROW region provides the best compatibility and is the default
    # fallback for any country not explicitly listed above.
}

# Services
SERVICE_SET_TEMPERATURE = "set_temperature"
SERVICE_SET_HEATER = "set_heater"
SERVICE_SET_BUBBLE = "set_bubble"
SERVICE_SET_JET = "set_jet"
SERVICE_SET_FILTER = "set_filter"

# Default values
TEMP_UNIT = UnitOfTemperature.CELSIUS
MAX_TEMP = 40
MIN_TEMP = 20

# Power consumption defaults (Watts) based on MSpa Comfort model specifications
# These are configurable per-device in the integration options
DEFAULT_PUMP_POWER = 60  # Filter pump: 2000l/t, 60W, 12V
DEFAULT_BUBBLE_POWER = 900  # Bubble generator: 900W (1.2HP)
DEFAULT_HEATER_POWER_PREHEAT = 1500  # Heating element: 1500W (preheat mode)
DEFAULT_HEATER_POWER_HEAT = 2000  # Heating element in active heating (estimated)

# Optional weather entity for ambient-condition bias correction.
# Reads temperature and wind_speed from a weather entity (e.g. Met.no, OpenWeatherMap).
CONF_WEATHER_ENTITY = "weather_entity"

# There is deliberately no switch for "apply the learned response to outdoor
# temperature". Configuring a weather entity *is* the switch: without one `ambient_temp`
# is None, and both `ambient_rate_factor` and `learned_ambient_factor` already return no
# correction. A separate boolean only added a second way to express the same intent, and
# a state — weather entity set, correction off — that nothing wanted.

# Which model computes the times the user actually sees and the scheduler acts on:
# Ready at, Heat schedule, and when a scheduled heat-up starts.
#
# The point of the option is that switching does not move any entity. The same sensors
# keep the same entity ids and the same meaning, so a dashboard or automation built on
# them needs no change — only the arithmetic behind them differs.
#
# "buckets" is the shipped model: three learned rates plus a correction for the weather.
# "newton" is the physical model, `dT/dt = P/C - (T_water - T_air)/tau`, fitted from the
# same recorded traverses. Newton falls back to buckets whenever it cannot answer, so
# selecting it can leave a time missing but never leaves one blank.
# **Deliberately absent from the config flow.** The physical model is being evaluated,
# not offered: the diagnostic shadow sensors are how it is watched, and putting a switch
# in the options dialog would invite people to adopt a model that has not yet been shown
# to work on a single spa. The seam exists so that switching is a one-line change when
# the evidence is in — and so it can be flipped now, by hand, on an installation whose
# owner knows what they are testing:
#
#     .storage/core.config_entries → the mspa entry → "options": {"prediction_model":
#     "newton"}, then restart.
#
# Nothing else moves when it is flipped. That is the property worth protecting: the same
# sensors keep the same entity ids and the same meaning, so no dashboard or automation
# has to change to try the other model, or to go back.
CONF_PREDICTION_MODEL = "prediction_model"
PREDICTION_MODEL_BUCKETS = "buckets"
PREDICTION_MODEL_NEWTON = "newton"
DEFAULT_PREDICTION_MODEL = PREDICTION_MODEL_BUCKETS

# Optional heat-schedule: calendar-driven automatic preheat scheduling.
CONF_SCHEDULE_TARGET_TEMP = "schedule_target_temp"
CONF_SCHEDULE_LOOKAHEAD_DAYS = "schedule_lookahead_days"
DEFAULT_SCHEDULE_TARGET_TEMP = 40.0
DEFAULT_SCHEDULE_LOOKAHEAD_DAYS = 5

# --- Prediction bias -----------------------------------------------------------
# The bias is a residual correction: the mean ratio of actual to estimated
# heating time, folded in once per completed session as an EMA.  It must only
# change when a session finishes — recomputing it from history on every load
# made it drift with whatever the weather happened to be at startup, and let it
# move *away* from recent performance.  Weather belongs in the rate model
# (see ambient_rate_factor), not here.
BIAS_EMA_ALPHA = 0.3     # newest session weight; last ~3 sessions ≈ 2/3 of total
BIAS_CLAMP_MIN = 0.9     # the segmented rate model does the real work — cap the
BIAS_CLAMP_MAX = 1.1     # residual tightly so it cannot double-correct
# The bias step scales with the span a session actually measured, because that is
# what sets the ratio's precision: ±0.25 °C of quantisation at each end is a few
# percent over a full cold start and ~50% over a 0.5 °C top-up.  A full-span run
# therefore corrects the bias in one session instead of five.
BIAS_ALPHA_MAX = 0.8     # weight given to a session spanning BIAS_SPAN_FULL_C
BIAS_SPAN_FULL_C = 8.0   # °C of heating that counts as a fully determined ratio
BIAS_MIN_DELTA_C = 3.0   # short runs carry too much variance to learn from
BIAS_RATIO_MIN = 0.3     # reject outlier ratios (e.g. water added mid-session)
BIAS_RATIO_MAX = 3.0


# --- Ambient-condition heating-rate correction ---------------------------------
# Colder-than-baseline outdoor air slows heating, most strongly near the setpoint
# (the "hot" bucket) where the water-to-air temperature difference — and thus the
# convective/evaporative heat loss — is greatest.  This is a linear model:
#
#   factor_i = clamp(1 + sensitivity_i * (ambient_now - ambient_baseline))
#   rate_adjusted_i = rate_learned_i * factor_i
#
# Per-bucket sensitivity is a fraction of the bucket's learned rate lost per °C
# below the learned baseline.  Cold water is nearly insensitive; the near-setpoint
# bucket is the most affected — matching observed cold-night behaviour where cold
# and mid buckets track their learned rates but the hot bucket collapses.
# (cold: T < 30°C, mid: 30-37°C, hot: T ≥ 37°C)
AMBIENT_SENSITIVITY = (0.0, 0.02, 0.06)
AMBIENT_FACTOR_MIN = 0.3   # never slow a bucket below 30% of its learned rate
AMBIENT_FACTOR_MAX = 1.5   # never speed a bucket beyond 150% of its learned rate
AMBIENT_BASELINE_ALPHA = 0.05    # slow EMA so the baseline tracks the seasonal norm
AMBIENT_BASELINE_DEFAULT = 15.0  # °C, used until enough samples have been learned


def ambient_rate_factor(bucket_idx, ambient_now, ambient_baseline):
    """Multiplicative heating-rate correction for current outdoor conditions.

    Returns 1.0 (no change) when ambient data is unavailable, so the estimate
    degrades gracefully to the plain learned rate.  Otherwise scales the bucket
    rate linearly with how far the current outdoor temperature is from the
    learned baseline, using a per-bucket sensitivity that is strongest near the
    setpoint.  The result is clamped to keep predictions stable.
    """
    if ambient_now is None or ambient_baseline is None:
        return 1.0
    if bucket_idx < 0 or bucket_idx > 2:
        return 1.0
    factor = 1.0 + AMBIENT_SENSITIVITY[bucket_idx] * (ambient_now - ambient_baseline)
    return max(AMBIENT_FACTOR_MIN, min(AMBIENT_FACTOR_MAX, factor))