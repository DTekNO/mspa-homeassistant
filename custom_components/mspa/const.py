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

# Optional heat-schedule: calendar-driven automatic preheat scheduling.
CONF_SCHEDULE_DATETIME = "schedule_datetime"
CONF_SCHEDULE_TARGET_TEMP = "schedule_target_temp"
CONF_SCHEDULE_LOOKAHEAD_DAYS = "schedule_lookahead_days"
DEFAULT_SCHEDULE_TARGET_TEMP = 40.0
DEFAULT_SCHEDULE_LOOKAHEAD_DAYS = 5