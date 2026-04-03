import asyncio
import hashlib
import time
import random
import string
import requests
import json

import logging
import functools


_LOGGER = logging.getLogger(__name__)


class _MSpaThrottle:
    """Proactive spike-arrest rate limiter shared across all API clients for one account.

    The MSpa API enforces a limit of 3 requests per second. By spacing every
    outbound HTTP call at least MIN_INTERVAL seconds apart we stay well below
    that ceiling regardless of how many coordinators / devices are running.
    """
    MIN_INTERVAL = 0.4  # seconds → max ~2.5 req/s, safely under the 3/s API limit

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_time: float = 0.0

    async def acquire(self) -> None:
        """Block until it is safe to fire the next API request."""
        async with self._lock:
            now = time.monotonic()
            wait = self.MIN_INTERVAL - (now - self._last_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_time = time.monotonic()


# Relevant ids and secrets:
# The App ID is sent in each HTTP request header from the Mspa app.
app_id = "e1c8e068f9ca11eba4dc0242ac120002"
# The App Secret can be determined by decompiling the Mspa app.
app_secret = "87025c9ecd18906d27225fe79cb68349"


class MSpaApiClient:
    def __init__(self, hass, account_email, password, coordinator, region="ROW", token=None, device_id=None):
        self.account_email = account_email
        self.password = password
        self.app_id = app_id
        self.app_secret = app_secret
        self.hass = hass
        self.coordinator = coordinator
        self.region = region if region in ["ROW", "US", "CH"] else "ROW"  # Safe fallback

        # Shared auth state keyed by credentials — all API clients for the same account
        # share one token and one lock so they never race to authenticate simultaneously.
        self._creds_key = hashlib.md5(f"{account_email}:{password}".encode()).hexdigest()
        auth_store = hass.data.setdefault("mspa_auth", {})
        if self._creds_key not in auth_store:
            auth_store[self._creds_key] = {
                "token": token,
                "lock": asyncio.Lock(),      # Serializes authentication across coordinators
                "api_lock": asyncio.Lock(),  # Serializes write commands across coordinators
                "throttle": _MSpaThrottle(), # Proactive rate limiter for all HTTP calls
            }

        self._target_device_id = device_id  # If set, select this specific device on init
        self.product_id = None
        self.device_id = None
        
        # Regional API endpoints with rock-solid fallback to ROW (Europe)
        self._api_endpoints = {
            "ROW": "https://api.iot.the-mspa.com",
            "US": "https://api.usiot.the-mspa.com",
            "CH": "https://api.mspa.mxchip.com.cn"
        }
        
        # Initialize device attributes
        self.series = None
        self.model = None
        self.software_version = None
        self.product_pic_url = None
        self._last_status = None
        
        _LOGGER.info("DIAGNOSTIC: MSpa API initialized for region: %s, endpoint: %s", 
                     self.region, self.base_url)
    
    @property
    def base_url(self):
        """Get the base URL for the current region with fallback to ROW."""
        return self._api_endpoints.get(self.region, self._api_endpoints["ROW"])

    @property
    def _throttle(self) -> _MSpaThrottle:
        """Per-account rate limiter shared by all API clients for this account."""
        return self.hass.data["mspa_auth"][self._creds_key]["throttle"]

    async def async_init(self):
        await self._do_async_init()

    async def _do_async_init(self):
        _LOGGER.info("DIAGNOSTIC: Starting MSpaApiClient initialization")
        device_list = await self.get_device_list()
        _LOGGER.info("DIAGNOSTIC: device_list result: %s", device_list)

        if not device_list:
            _LOGGER.error("DIAGNOSTIC: device_list is None or empty!")
            _LOGGER.error("No devices found. Please check your credentials and network connection.")
            raise RuntimeError("MSpaApiClient initialization failed: No devices found.")

        if not isinstance(device_list, dict):
            _LOGGER.error("DIAGNOSTIC: device_list is not a dict! Type: %s, Value: %s", type(device_list), device_list)
            raise RuntimeError("MSpaApiClient initialization failed: Unexpected device_list format.")

        if "list" not in device_list:
            _LOGGER.error("DIAGNOSTIC: 'list' key not found in device_list! Keys: %s", device_list.keys())
            raise RuntimeError("MSpaApiClient initialization failed: No 'list' in device_list.")

        devices = device_list["list"]

        if not devices or len(devices) == 0:
            _LOGGER.error("DIAGNOSTIC: Device list is empty! Full device_list: %s", device_list)
            raise RuntimeError("MSpaApiClient initialization failed: Device list is empty.")

        # Select device: use stored target device_id if available, otherwise fall back to first
        if self._target_device_id:
            device = next(
                (d for d in devices if d.get("device_id") == self._target_device_id),
                None,
            )
            if device is None:
                _LOGGER.warning(
                    "DIAGNOSTIC: Target device_id '%s' not found in device list; falling back to first device",
                    self._target_device_id,
                )
                device = devices[0]
            else:
                _LOGGER.info("DIAGNOSTIC: Found target device_id '%s' in device list", self._target_device_id)
        else:
            device = devices[0]

        _LOGGER.info("DIAGNOSTIC: Found %d device(s). Selected device: %s", len(devices), device)
        self.product_id = device.get("product_id")
        self.device_id = device.get("device_id")
        self.series = device.get("product_series")
        self.model = device.get("product_model")
        self.software_version = device.get("software_version")
        self.product_pic_url = device.get("url")

        self.device_alias = device.get("device_alias")
        self.coordinator.model = self.model
        self.coordinator.series = self.series
        self.coordinator.software_version = self.software_version
        self.coordinator.product_pic_url = self.product_pic_url
        self.coordinator.device_id = self.device_id
        self.coordinator.product_id = self.product_id
        self.coordinator.device_alias = self.device_alias

    @staticmethod
    def generate_nonce(length=32):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    @staticmethod
    def current_ts():
        return str(int(time.time()))

    def build_signature(self, nonce, ts):
        raw = self.app_id + "," + self.app_secret + "," + nonce + "," + ts
        return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

    def get_former_token(self):
        return self.hass.data.get("mspa_auth", {}).get(self._creds_key, {}).get("token") or ""

    def set_token_in_hass(self, token):
        self.hass.data.setdefault("mspa_auth", {}).setdefault(
            self._creds_key, {"token": None, "lock": asyncio.Lock()}
        )["token"] = token

    def _build_headers(self, auth_token=None):
        """Build standard API request headers."""
        nonce = self.generate_nonce()
        ts = self.current_ts()
        sign = self.build_signature(nonce, ts)
        authorization = f"token {auth_token}" if auth_token else "token"
        return {
            "push_type": "Android",
            "authorization": authorization,
            "appid": self.app_id,
            "nonce": nonce,
            "ts": ts,
            "lan_code": "de",
            "sign": sign,
            "content-type": "application/json; charset=UTF-8",
            "accept-encoding": "gzip",
            "user-agent": "okhttp/4.9.0"
        }

    def _obfuscate_email(self):
        """Return an obfuscated version of the account email for logging."""
        if not self.account_email:
            return "***"
        parts = self.account_email.split("@")
        if len(parts) == 2 and parts[0]:
            return f"{parts[0][:3]}***@{parts[1]}"
        return "***"

    def _obfuscate_response(self, response_data):
        """Obfuscate email addresses in response data for logging."""
        if not self.account_email:
            return response_data

        obfuscated = self._obfuscate_email()

        # Convert to JSON string, replace email, then parse back
        import copy
        safe_data = copy.deepcopy(response_data)
        data_str = json.dumps(safe_data)
        data_str = data_str.replace(self.account_email, obfuscated)
        return json.loads(data_str)


    async def authenticate(self):
        auth_state = self.hass.data["mspa_auth"][self._creds_key]
        lock = auth_state["lock"]
        token_before_wait = auth_state.get("token")

        async with lock:
            # If another coordinator authenticated while we were waiting for the lock,
            # reuse their fresh token instead of logging in again (which would invalidate it).
            if auth_state.get("token") and auth_state["token"] != token_before_wait:
                _LOGGER.info(
                    "DIAGNOSTIC: Token refreshed by another coordinator while waiting — reusing it"
                )
                return auth_state["token"]

            return await self._do_authenticate()

    async def _do_authenticate(self):
        headers = self._build_headers()
        payload = {
            "account": self.account_email,
            "app_id": self.app_id,
            "password": self.password,
            "brand": "",
            "registration_id": "",
            "push_type": "android",
            "lan_code": "EN",
            "country": ""  # Country is not required for authentication
        }
        token_request_url = f"{self.base_url}/api/enduser/get_token/"

        await self._throttle.acquire()
        _LOGGER.info("DIAGNOSTIC: Attempting authentication to %s", token_request_url)
        obfuscated_email = self._obfuscate_email()
        _LOGGER.info("DIAGNOSTIC: Account email: %s", obfuscated_email)
        _LOGGER.info("DIAGNOSTIC: Password hash length: %d, first 6 chars: %s", len(self.password), self.password[:6] if self.password else "None")

        try:
            response = await self.hass.async_add_executor_job(
                functools.partial(requests.post, token_request_url, headers=headers, json=payload, timeout=30)
            )
            _LOGGER.info("DIAGNOSTIC: Authentication HTTP status code: %s", response.status_code)

            response_json = response.json()

            # Obfuscate sensitive data in response for logging
            safe_response = self._obfuscate_response(response_json)
            _LOGGER.info("DIAGNOSTIC: Authentication raw response: %s", response.text.replace(self.account_email, obfuscated_email) if self.account_email in response.text else response.text)
            _LOGGER.info("DIAGNOSTIC: Authentication parsed response: %s", safe_response)

            token = response_json.get("data", {}).get("token")
            if token is not None:
                _LOGGER.info("DIAGNOSTIC: Token successfully received (length: %d)", len(token))
                self.set_token_in_hass(token)
                return token
            else:
                # Check for specific error codes
                error_code = response_json.get("code")
                error_message = response_json.get("message", "")

                if error_code == 16019 or "password" in error_message.lower():
                    _LOGGER.error("DIAGNOSTIC: Password authentication failed!")
                    _LOGGER.error("DIAGNOSTIC: API error code: %s, message: %s", error_code, error_message)
                    _LOGGER.error("DIAGNOSTIC: Please verify:")
                    _LOGGER.error("DIAGNOSTIC:   1. You are using the SAME password you use in the MSpa mobile app")
                    _LOGGER.error("DIAGNOSTIC:   2. Your password does not contain special characters that might cause encoding issues")
                    _LOGGER.error("DIAGNOSTIC:   3. Try resetting your password in the MSpa app and using a simple password (letters and numbers only)")
                    raise RuntimeError(f"Authentication failed: {error_message}. Please check your password in the MSpa mobile app.")

                _LOGGER.error("DIAGNOSTIC: No token in response. Response data: %s", response_json.get("data"))
                _LOGGER.error("DIAGNOSTIC: Full response: %s", response_json)
                raise RuntimeError(
                    f"Authentication failed: no token in response (code={error_code}, message={error_message})"
                )
        except requests.exceptions.Timeout:
            _LOGGER.error("DIAGNOSTIC: Authentication request timed out after 30 seconds")
            raise
        except requests.exceptions.ConnectionError as e:
            _LOGGER.error("DIAGNOSTIC: Connection error during authentication: %s", str(e))
            raise
        except Exception as e:
            _LOGGER.error("DIAGNOSTIC: Unexpected error during authentication: %s", str(e), exc_info=True)
            raise

    async def send_device_command(self, desired_dict, retry=False):
        _LOGGER.debug("send_device_command: %s, retry %s", desired_dict, retry)
        auth_state = self.hass.data["mspa_auth"][self._creds_key]
        async with auth_state["api_lock"]:
            return await self._send_device_command_locked(desired_dict, retry)

    async def _send_device_command_locked(self, desired_dict, retry=False):
        """Inner implementation — called only while the api_lock is held."""
        token = self.get_former_token()
        headers = self._build_headers(token)
        payload = {
            "device_id": self.device_id,
            "product_id": self.product_id,
            "desired": json.dumps({"state": {"desired": desired_dict}})
        }

        url = f"{self.base_url}/api/device/command"
        _LOGGER.debug("send_device_command: %s, url: %s", desired_dict, url)
        await self._throttle.acquire()
        response = await self.hass.async_add_executor_job(
            functools.partial(requests.post, url, headers=headers, json=payload)
        )
        response = response.json()
        if (response.get('message') != 'SUCCESS') and (not retry):
            token = await self.authenticate()
            return await self._send_device_command_locked(desired_dict, True)

        # Poll for expected state if provided
        for _ in range(5):
            await asyncio.sleep(3)
            status = await self.get_hot_tub_status()
            if all(status.get(k) == v for k, v in desired_dict.items()):
                self._last_status = status  # Cache latest status
                break

        if (desired_dict.get("filter_state")) == 0:
            await self.set_heater_state(0)

        # Trigger coordinator refresh after command completes
        await self.coordinator.async_request_refresh()

        return response



    async def set_heater_state(self, state: int):
        return await self.send_device_command({"heater_state": state})

    async def set_bubble_state(self, state: int, level: int):
        return await self.send_device_command({"bubble_state": state, "bubble_level": level})

    async def set_bubble_level(self, level: int):
        return await self.send_device_command({"bubble_level": level})

    async def set_jet_state(self, state: int):
        return await self.send_device_command({"jet_state": state})

    async def set_filter_state(self, state: int):
        return await self.send_device_command({"filter_state": state})

    async def set_ozone_state(self, state: int):
        return await self.send_device_command({"ozone_state": state})

    async def set_uvc_state(self, state: int):
        return await self.send_device_command({"uvc_state": state})

    async def set_temperature_setting(self, temp: int):
        return await self.send_device_command({"temperature_setting": temp*2})

    async def set_temperature_unit(self, unit: int):
        """Set temperature unit (0=Celsius, 1=Fahrenheit)."""
        return await self.send_device_command({"temperature_unit": unit})

    async def get_hot_tub_status(self, retry=False):
        headers = self._build_headers(self.get_former_token())
        payload = {
            "device_id": self.device_id,
            "product_id": self.product_id
        }
        url = f"{self.base_url}/api/device/thing_shadow/"
        await self._throttle.acquire()
        response = await self.hass.async_add_executor_job(
            functools.partial(requests.post, url, headers=headers, json=payload)
        )
        response = response.json()
        if not response.get("data") and not retry:
            await self.authenticate()
            return await self.get_hot_tub_status(True)
        data = response["data"]
        _LOGGER.debug("get_hot_tub_status %s", data)
        return data

    async def get_device_list(self, retry=False):
        headers = self._build_headers(self.get_former_token())
        url = f"{self.base_url}/api/enduser/devices/"

        _LOGGER.info("DIAGNOSTIC: Attempting to get device list from %s (retry=%s)", url, retry)
        _LOGGER.info("DIAGNOSTIC: Using token (first 20 chars): %s...", self.get_former_token()[:20] if self.get_former_token() else "None")

        try:
            await self._throttle.acquire()
            response = await self.hass.async_add_executor_job(
                functools.partial(requests.get, url, headers=headers, timeout=30)
            )
            _LOGGER.info("DIAGNOSTIC: Device list HTTP status code: %s", response.status_code)
            _LOGGER.info("DIAGNOSTIC: Device list raw response: %s", response.text)

            response_json = response.json()
            _LOGGER.info("DIAGNOSTIC: Device list parsed response: %s", response_json)

            code = response_json.get("code")
            data = response_json.get("data", {})
            has_list = isinstance(data, dict) and "list" in data

            if code == 11000 and not retry:
                # Rate-limited — wait and retry once
                _LOGGER.warning(
                    "DIAGNOSTIC: Device list rate-limited (code 11000). Waiting 2s before retry..."
                )
                await asyncio.sleep(2)
                return await self.get_device_list(True)

            if not has_list and not retry:
                _LOGGER.warning("DIAGNOSTIC: No device list in response (code=%s), re-authenticating...", code)
                await self.authenticate()
                return await self.get_device_list(True)

            if not has_list:
                _LOGGER.error("DIAGNOSTIC: No device list after retry. Response: %s", response_json)
                return data

            device_count = len(data["list"])
            _LOGGER.info("DIAGNOSTIC: Device list returned successfully. Number of devices: %d", device_count)

            if device_count == 0:
                _LOGGER.error("DIAGNOSTIC: Device list is empty! Full data structure: %s", data)
            else:
                _LOGGER.info("DIAGNOSTIC: First device info: %s", data.get("list", [])[0] if data.get("list") else "No list key")

            return data
        except requests.exceptions.Timeout:
            _LOGGER.error("DIAGNOSTIC: Device list request timed out after 30 seconds")
            raise
        except requests.exceptions.ConnectionError as e:
            _LOGGER.error("DIAGNOSTIC: Connection error getting device list: %s", str(e))
            raise
        except Exception as e:
            _LOGGER.error("DIAGNOSTIC: Unexpected error getting device list: %s", str(e), exc_info=True)
            raise
