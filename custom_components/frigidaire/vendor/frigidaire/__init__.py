"""Frigidaire 2.0 API client"""

import gzip
import hashlib
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import NoReturn, Optional, TypeVar, cast
from urllib.parse import urlencode, urlparse

import requests
from requests import Response

from .rate_limit import RateLimiter, wrap_session_request
from .signature_generator import get_signature

T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)

GLOBAL_API_URL = "https://api.ocp.electrolux.one"

# Hosts this client may talk to. Two of the three hosts it uses are not constants:
# the Gigya identity domain and the regional API base URL both arrive inside a cloud
# response, and the credential POST goes to the identity domain. Without an allowlist,
# anyone able to tamper with that response could point the POST carrying the account
# email and password at a host of their choosing.
_ALLOWED_IDENTITY_DOMAIN_SUFFIXES = (".gigya.com", ".electrolux.one", ".electrolux.com")
_ALLOWED_API_HOST_SUFFIXES = (".electrolux.one", ".electrolux.com")

# A DNS hostname and nothing else: no scheme, no port, no path, no credentials. Checking
# the shape matters as much as the suffix, because these values are interpolated straight
# into a URL — "evil.example/x.gigya.com" ends with an allowed suffix but resolves to
# evil.example.
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def _is_allowed_host(host: str, allowed_suffixes: tuple[str, ...]) -> bool:
    """Whether ``host`` is a well-formed hostname inside one of the allowed domains."""
    if not isinstance(host, str):
        return False
    candidate = host.strip().rstrip(".").lower()
    if not _HOSTNAME_RE.match(candidate):
        return False
    return any(candidate == suffix.lstrip(".") or candidate.endswith(suffix) for suffix in allowed_suffixes)


def _validate_identity_domain(domain: str) -> str:
    """Return the Gigya identity domain, or refuse to send credentials to it."""
    if not _is_allowed_host(domain, _ALLOWED_IDENTITY_DOMAIN_SUFFIXES):
        raise FrigidaireException(
            f"Refusing to send credentials to unexpected identity domain {domain!r}; "
            f"expected a host under {', '.join(_ALLOWED_IDENTITY_DOMAIN_SUFFIXES)}"
        )
    return domain


def _validate_api_base_url(base_url: str) -> str:
    """Return the regional API base URL, or reject it as a place to send the bearer token."""
    parsed = urlparse(base_url or "")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.path.strip("/"):
        raise FrigidaireException(f"Unexpected Frigidaire API base URL {base_url!r}")
    if parsed.port is not None and parsed.port != 443:
        raise FrigidaireException(f"Unexpected Frigidaire API base URL {base_url!r}")
    if not _is_allowed_host(parsed.hostname or "", _ALLOWED_API_HOST_SUFFIXES):
        raise FrigidaireException(
            f"Refusing to use unexpected Frigidaire API host {parsed.hostname!r}; "
            f"expected a host under {', '.join(_ALLOWED_API_HOST_SUFFIXES)}"
        )
    return base_url.rstrip("/")


def _validate_request_url(url: str) -> str:
    """Last check before a request leaves: https, on a host this client is allowed to use.

    The base URL is validated where it is read, but it also comes back from the caller
    (Home Assistant persists it between restarts), so every request is re-checked rather
    than trusting that path.
    """
    parsed = urlparse(url)
    allowed = _ALLOWED_IDENTITY_DOMAIN_SUFFIXES + _ALLOWED_API_HOST_SUFFIXES
    if parsed.scheme != "https" or not _is_allowed_host(parsed.hostname or "", allowed):
        raise FrigidaireException(f"Refusing to make a request to unexpected URL {url!r}")
    return url


FRIGIDAIRE_API_KEY = "3BAfxFtCTdGbJ74udWvSe6ZdPugP8GcKz3nSJVfg"
CLIENT_SECRET = (
    "26SGRupOJaxv4Y1npjBsScjJPuj7f8YTdGxJak3nhAnowCStsBAEzKtrEHsgbqUyh90"
    "KFsoty7xXwMNuLYiSEcLqhGQryBM26i435hncaLqj5AuSvWaGNRTACi7ba5yu"
)
CLIENT_ID = "FrigidaireOneApp"
FRIGIDAIRE_USER_AGENT = "Ktor client"
AUTH_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 12; sdk_gphone64_x86_64 Build/SE1A.220826.008)"

# Limiters are keyed by account so multiple Frigidaire instances for the same
# account share spacing — without this, a config-flow re-validation that runs
# alongside a live entry would compete and trip cas_3403.
_SCOPED_LIMITERS: dict[str, RateLimiter] = {}

# Re-authentication is serialised per account for the same reason the limiter is
# shared: when several threads (one per appliance in Home Assistant) fail at once,
# only the first should mint a new session; the rest wait and reuse it.
_SCOPED_REAUTH_LOCKS: dict[str, threading.Lock] = {}

# Header names whose values are credentials; matched case-insensitively.
_REDACT_HEADERS = frozenset({"authorization", "x-api-key"})

# Top-level JSON keys whose values are credentials in auth-flow request bodies.
_REDACT_PAYLOAD_KEYS = frozenset(
    {"password", "clientSecret", "apiKey", "oauth_token", "idToken", "id_token", "sig", "accessToken"}
)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _REDACT_HEADERS else v) for k, v in headers.items()}


def _redact_payload(payload: str) -> str:
    if not payload:
        return payload
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload
    if not isinstance(data, dict):
        return payload
    return json.dumps({k: ("<redacted>" if k in _REDACT_PAYLOAD_KEYS else v) for k, v in data.items()})


def _require_field(response: dict, key: str, what: str) -> str:
    """Read a required field from an auth response, naming the keys rather than the body.

    These responses carry session tokens, so a failure reports which keys arrived and
    nothing else. Raising FrigidaireException (not KeyError) also keeps the failure inside
    the class callers already handle.
    """
    value = response.get(key)
    if not value:
        raise FrigidaireException(f"Failed to authenticate: {key} missing from {what} (keys: {sorted(response)})")
    return value


class FrigidaireException(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


# Maps known Electrolux internal platform codenames to destination types.
# These codenames appear in applianceData.modelName for newer devices instead
# of the legacy "AC"/"DH" values. Add new entries here as they are confirmed.
_MODEL_MAPPINGS: dict[str, "Destination"]


class Destination(str, Enum):
    AIR_CONDITIONER = "AC"
    DEHUMIDIFIER = "DH"

    @classmethod
    def from_appliance_type(cls, appliance_type: str) -> "Destination":
        """
        Maps known model names to their corresponding destination types.
        Falls back to direct enum lookup for backward compatibility.

        :param appliance_type: The model name from the appliance data
        :return: The appropriate Destination enum value
        :raises ValueError: If the model name is not recognized
        """
        if appliance_type in _MODEL_MAPPINGS:
            return _MODEL_MAPPINGS[appliance_type]
        try:
            return cls(appliance_type)
        except ValueError as e:
            raise ValueError(
                f"'{appliance_type}' is not a recognized model name or destination type. "
                f"Known destinations: {list(cls)}, "
                f"Known models: {list(_MODEL_MAPPINGS.keys())}"
            ) from e


_MODEL_MAPPINGS = {
    "Husky": Destination.DEHUMIDIFIER,  # e.g. FHDD5033W1 (50-pint WiFi dehumidifier)
    "Eagle": Destination.DEHUMIDIFIER,  # e.g. GHDD5035W1 (50-pint Gallery WiFi dehumidifier)
    "Panther": Destination.AIR_CONDITIONER,  # e.g. FHWW105WE1 (window inverter AC)
    "Telica": Destination.AIR_CONDITIONER,  # e.g. GHPH142AA1 (portable inverter AC/heat)
}

# Reported property keys that are unique to each destination type.
# Used to infer destination when the codename is not in _MODEL_MAPPINGS.
_AC_PROPERTY_KEYS = {
    "targetTemperatureC",
    "targetTemperatureF",
    "ambientTemperatureC",
    "ambientTemperatureF",
    "temperatureRepresentation",
}
# Note: "sensorHumidity" is deliberately absent. It is not DH-exclusive — Telica portable
# ACs report a room humidity reading, so treating it as a DH marker misidentifies them.
_DH_PROPERTY_KEYS = {"targetHumidity", "waterBucketLevel", "waterTankFull"}


class Setting(str, Enum):
    """
    Writeable settings that are known valid names of Components.
    These can be passed to the execute_action() API together with a target value
    to change settings.
    """

    # Common
    FAN_SPEED = "fanSpeedSetting"
    EXECUTE_COMMAND = "executeCommand"
    MODE = "mode"
    SLEEP_MODE = "sleepMode"
    UI_LOCK_MODE = "uiLockMode"
    VERTICAL_SWING = "verticalSwing"

    # AC
    TARGET_TEMPERATURE_C = "targetTemperatureC"
    TARGET_TEMPERATURE_F = "targetTemperatureF"
    TEMPERATURE_REPRESENTATION = "temperatureRepresentation"

    # Humidifier
    CLEAN_AIR_MODE = "cleanAirMode"
    DISPLAY_LIGHT = "displayLight"
    START_TIME = "startTime"
    STOP_TIME = "stopTime"
    TARGET_HUMIDITY = "targetHumidity"


class Detail(str, Enum):
    """
    Readable details that are known to be present in some products.
    """

    # Common
    ALERTS = "alerts"
    APPLIANCE_STATE = "applianceState"
    APPLIANCE_UI_SW_VERSION = "applianceUiSwVersion"
    FAN_SPEED = "fanSpeedSetting"
    FAN_SPEED_STATE = "fanSpeedState"
    FILTER_STATE = "filterState"
    MODE = "mode"
    # The mode the appliance reports it is *actually* running, which can differ from the
    # requested MODE — e.g. an ECO/AUTO unit reports "cool" or "fanOnly" as it cycles.
    MODE_STATE = "modeState"
    NETWORK_INTERFACE = "networkInterface"
    # Room humidity reading. Reported by dehumidifiers and by some ACs (e.g. Telica).
    SENSOR_HUMIDITY = "sensorHumidity"
    UI_LOCK_MODE = "uiLockMode"
    SLEEP_MODE = "sleepMode"
    VERTICAL_SWING = "verticalSwing"

    # AC
    AMBIENT_TEMPERATURE_C = "ambientTemperatureC"
    AMBIENT_TEMPERATURE_F = "ambientTemperatureF"
    TARGET_TEMPERATURE_C = "targetTemperatureC"
    TARGET_TEMPERATURE_F = "targetTemperatureF"
    TEMPERATURE_REPRESENTATION = "temperatureRepresentation"

    # Air quality, on models with a particulate sensor. Units are µg/m³.
    PM1 = "pm1"
    PM10 = "pm10"
    PM25 = "pm25"

    # Humidifier
    DISPLAY_LIGHT = "displayLight"
    CLEAN_AIR_MODE = "cleanAirMode"
    START_TIME = "startTime"
    STOP_TIME = "stopTime"
    TARGET_HUMIDITY = "targetHumidity"
    WATER_BUCKET_LEVEL = "waterBucketLevel"
    WATER_TANK_FULL = "waterTankFull"


class Appliance:
    def __init__(self, args: dict):
        # Every field except the id is optional: the cloud has shipped records without
        # applianceData, and one such record used to raise KeyError out of the list
        # comprehension in get_appliances(), taking every other appliance with it.
        appliance_data = args.get("applianceData") or {}
        appliance_id = args.get("applianceId")
        if not appliance_id:
            raise ValueError(f"Appliance record has no applianceId; keys: {sorted(args)}")
        self.appliance_id: str = appliance_id
        self.appliance_type: str = appliance_data.get("modelName") or ""
        self.nickname: str = appliance_data.get("applianceName") or appliance_id
        self.destination = self._resolve_destination(args)

    def _resolve_destination(self, args: dict) -> Optional["Destination"]:
        try:
            return Destination.from_appliance_type(self.appliance_type)
        except ValueError:
            pass

        # Check DH first: target-humidity/water-bucket keys are DH-exclusive, while the "AC"
        # keys (ambient temperature, temperature representation) are also reported by
        # dehumidifiers that display room temp. Note that a humidity *reading*
        # ("sensorHumidity") is not a DH marker — some ACs report one too.
        reported_keys = set(((args.get("properties") or {}).get("reported") or {}).keys())
        if reported_keys & _DH_PROPERTY_KEYS:
            _LOGGER.warning(
                f"Unknown appliance type '{self.appliance_type}' for '{self.nickname}' "
                f"({self.appliance_id}) — inferred DEHUMIDIFIER from reported properties. "
                f"Please report this at https://github.com/bm1549/frigidaire/issues"
            )
            return Destination.DEHUMIDIFIER
        if reported_keys & _AC_PROPERTY_KEYS:
            _LOGGER.warning(
                f"Unknown appliance type '{self.appliance_type}' for '{self.nickname}' "
                f"({self.appliance_id}) — inferred AIR_CONDITIONER from reported properties. "
                f"Please report this at https://github.com/bm1549/frigidaire/issues"
            )
            return Destination.AIR_CONDITIONER

        _LOGGER.warning(
            f"Unrecognized appliance type '{self.appliance_type}' for '{self.nickname}' "
            f"({self.appliance_id}) — skipping. Reported keys: {sorted(reported_keys)}. "
            f"Please report this at https://github.com/bm1549/frigidaire/issues"
        )
        return None


class Component:
    def __init__(self, name: str | Setting, value: int | str):
        """
        Create a new Component to specify a setting with a name and value.
        Note: String names are discouraged but allowed since not all settings are known at this time.

        :param name: Name of the setting (Setting or a string).
        :param value: Value of the setting (string or int)
        """
        if isinstance(name, Setting):
            name = name.value
        self.name = name
        self.value = value


class DisplayLight(str, Enum):
    # Unlike most other on/off settings, the API rejects plain "ON"/"OFF" for displayLight.
    ON = "DISPLAY_LIGHT_1"
    OFF = "DISPLAY_LIGHT_0"


class Unit(str, Enum):
    FAHRENHEIT = "FAHRENHEIT"
    CELSIUS = "CELSIUS"


class ApplianceState(str, Enum):
    OFF = "OFF"
    RUNNING = "RUNNING"
    DELAYED_START = "DELAYED_START"


class FilterState(str, Enum):
    BUY = "BUY"
    CHANGE = "CHANGE"
    CLEAN = "CLEAN"
    GOOD = "GOOD"


class Power(str, Enum):
    ON = "ON"
    OFF = "OFF"


class SleepMode(str, Enum):
    ON = "ON"
    OFF = "OFF"


class VerticalSwing(str, Enum):
    ON = "ON"
    OFF = "OFF"


class Alert(str, Enum):
    BUCKET_FULL = "BUCKET_FULL"
    BUS_HIGH_VOLTAGE = "BUS_HIGH_VOLTAGE"
    COMMUNICATION_FAULT = "COMMUNICATION_FAULT"
    DC_MOTOR_FAULT = "DC_MOTOR_FAULT"
    DC_MOTOR_LOST_SPEED = "DC_MOTOR_LOST_SPEED"
    DRAIN_PAN_FULL = "DRAIN_PAN_FULL"
    INDOOR_DEFROST_THERMISTOR_FAULT = "INDOOR_DEFROST_THERMISTOR_FAULT"
    PM25_SENSOR_FAULT = "PM25_SENSOR_FAULT"
    TUBE_HIGH_TEMPERATURE = "TUBE_HIGH_TEMPERATURE"
    UNKNOWN_STATE_ERROR = "UNKNOWN_STATE_ERROR"


class Mode(str, Enum):
    # Air Conditioner
    OFF = "OFF"
    COOL = "COOL"
    FAN = "FANONLY"
    ECO = "ECO"
    # Dehumidifier
    DRY = "DRY"
    AUTO = "AUTO"
    CONTINUOUS = "CONTINUOUS"
    QUIET = "QUIET"
    SMART = "SMART"


class FanSpeed(str, Enum):
    # Common
    LOW = "LOW"
    MEDIUM = "MIDDLE"
    HIGH = "HIGH"
    # Air Conditioner
    AUTO = "AUTO"


class Action:
    @classmethod
    def set_display_light(cls, display_light: DisplayLight) -> list[Component]:
        return [Component(Setting.DISPLAY_LIGHT, display_light)]

    @classmethod
    def set_power(cls, power: Power) -> list[Component]:
        return [Component(Setting.EXECUTE_COMMAND, power)]

    @classmethod
    def set_mode(cls, mode: Mode) -> list[Component]:
        return [Component(Setting.MODE, mode)]

    @classmethod
    def set_fan_speed(cls, fan_speed: FanSpeed) -> list[Component]:
        return [Component(Setting.FAN_SPEED, fan_speed)]

    @classmethod
    def set_ui_lock_mode(cls, ui_lock_mode: bool) -> list[Component]:
        return [Component(Setting.UI_LOCK_MODE, ui_lock_mode)]

    @classmethod
    def set_vertical_swing(cls, vertical_swing: VerticalSwing) -> list[Component]:
        return [Component(Setting.VERTICAL_SWING, vertical_swing)]

    @classmethod
    def set_sleep_mode(cls, sleep_mode: SleepMode) -> list[Component]:
        return [Component(Setting.SLEEP_MODE, sleep_mode)]

    @classmethod
    def set_stop_time(cls, stop_time: int) -> list[Component]:
        """Stop time in seconds; device snaps to ~30-min increments (min ~1800s, use 0 to clear)."""
        if stop_time < 0:
            raise FrigidaireException("StopTime must be greater than or equal to 0")

        return [Component(Setting.STOP_TIME, stop_time)]

    @classmethod
    def set_start_time(cls, start_time: int) -> list[Component]:
        """Start time in seconds; device snaps to ~30-min increments (min ~1800s, use 0 to clear)."""
        if start_time < 0:
            raise FrigidaireException("StartTime must be greater than or equal to 0")

        return [Component(Setting.START_TIME, start_time)]

    @classmethod
    def set_humidity(cls, humidity: int) -> list[Component]:
        if humidity < 35 or humidity > 85:
            raise FrigidaireException("Humidity must be between 35 and 85 percent, inclusive")

        return [Component(Setting.TARGET_HUMIDITY, humidity)]

    @classmethod
    def set_temperature(cls, temperature: int, temperature_unit: Unit = Unit.FAHRENHEIT) -> list[Component]:
        # Note: Frigidaire sets limits for temperature which could cause this action to fail
        # Temperature ranges are below, inclusive of the endpoints
        #   Fahrenheit: 60-90
        #   Celsius: 16-32
        _LOGGER.debug(f"Client setting target to {temperature} {temperature_unit}")
        temperature_unit_setting = (
            Setting.TARGET_TEMPERATURE_F if temperature_unit == Unit.FAHRENHEIT else Setting.TARGET_TEMPERATURE_C
        )

        return [
            Component(Setting.TEMPERATURE_REPRESENTATION, temperature_unit),
            Component(temperature_unit_setting, temperature),
        ]


def _scope_key(raw: str) -> str:
    """Stable, non-identifying key for the shared limiter and re-auth lock."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _generate_nonce() -> str:
    """
    Generate a one-off random token to preserve the security of encrypted communication
    """
    return f"{str(int(time.time()))}_-{str(random.getrandbits(32))}"


class Frigidaire:
    """
    An API for interfacing with Frigidaire Air Conditioners
    This was reverse-engineered from the Frigidaire 2.0 App
    """

    def __init__(
        self,
        username: str,
        password: str | None,
        session_key: str | None = None,
        timeout: float | None = 15.0,
        regional_base_url: str | None = None,
        country_code: str = "US",
        *,
        refresh_token: str | None = None,
        rate_limit_min_interval: float = 1.25,
        rate_limit_jitter: float = 0.25,
        rate_limit_methods: frozenset[str] | set[str] | None = None,
        rate_limit_scope_key: str | None = None,
        max_retries_on_429: int = 4,
        max_retry_after: float = 60.0,
        session_max_retries: int = 2,
        session_retry_backoff: float = 0.0,
        on_session_key_update: Callable[[str, str | None, str | None], None] | None = None,
    ):
        """
        Initializes a new instance of the Frigidaire API and authenticates against it
        :param username: The username to log in to Frigidaire. Generally, this is an email
        :param password: The password to log in to Frigidaire. None means session-key only:
                            the client will use the session_key it is given, and once that
                            session is gone it raises FrigidaireException with
                            error_code="reauth_required" rather than attempting a login.
        :param session_key: The previously authenticated session key to connect to Frigidaire. If not specified,
                            authentication is required
        :param refresh_token: The refresh token Electrolux issued with that session key. Session keys
                            live 12 hours; with the refresh token the client mints the next one itself,
                            without the password. Without it, a dead session means reauth_required.
        :param timeout: Per-request HTTP timeout in seconds (default 15.0). None falls back to
                            rate_limit.FALLBACK_TIMEOUT; requests are never made without a timeout.
        :param regional_base_url: Regional base URL for the API user account
                            (e.g., https://api.us.ocp.electrolux.one for U.S. accounts). If not specified,
                            authentication is required
        :param country_code: Country code from which to derive regional base URL. Defaults to "US".
        :param rate_limit_min_interval: Minimum seconds between mutating requests (default 1.25).
        :param rate_limit_jitter: Random jitter added to spacing to smooth bursts (default 0.25).
        :param rate_limit_methods: HTTP methods to throttle (default POST/PUT/PATCH/DELETE).
        :param rate_limit_scope_key: Key for sharing a limiter across instances (default: username).
        :param max_retries_on_429: Max retries on 429/423 before giving up (default 4).
        :param max_retry_after: Cap on the server's Retry-After header in seconds (default 60.0).
        :param session_max_retries: How many times to re-run a failed operation before giving up
                            (default 2): the first extra attempt retries on the existing session,
                            the last re-authenticates first. The session cap (cas_3403) is never
                            retried. 0 disables session retries entirely.
        :param session_retry_backoff: Seconds to sleep before each session retry, scaled by attempt
                            number (default 0.0 = no delay).
        :param on_session_key_update: Optional callback invoked with (session_key, regional_base_url,
                            refresh_token) whenever a new session key is minted or refreshed. Lets callers persist the key so it
                            survives restarts instead of abandoning a still-valid token and minting a
                            new server-side session (which Electrolux caps via cas_3403).
        """
        self.username = username
        self.password = password
        self.session_key: str | None = session_key
        self.refresh_token: str | None = refresh_token
        # A cached base URL comes back from the caller's own storage, so it gets the same
        # check as one read from a response. A bad one is dropped rather than raised on:
        # re-authenticating recovers, and refusing to start would strand the caller with a
        # corrupt cache file it cannot see.
        if regional_base_url:
            try:
                regional_base_url = _validate_api_base_url(regional_base_url)
            except FrigidaireException:
                _LOGGER.warning("Ignoring cached Frigidaire base URL that is not a known Electrolux host")
                regional_base_url = None
                session_key = None
                self.session_key = None
        self.regional_base_url = regional_base_url
        self.country_code = country_code
        self._session_max_retries = session_max_retries
        self._session_retry_backoff = session_retry_backoff
        self._on_session_key_update = on_session_key_update

        self._session = requests.Session()
        # Hashed, because these dicts live for the life of the process and are never
        # cleaned up: keying them by the raw address would leave the account's email in a
        # module global (and in any heap dump) for every typo ever entered.
        scope = _scope_key(rate_limit_scope_key or username)
        self._scope = scope
        limiter = _SCOPED_LIMITERS.setdefault(scope, RateLimiter(rate_limit_min_interval, rate_limit_jitter))
        self._reauth_lock = _SCOPED_REAUTH_LOCKS.setdefault(scope, threading.Lock())
        self._session.request = wrap_session_request(  # type: ignore[method-assign]
            self._session.request,
            limiter,
            rate_limit_methods,
            max_retry_after,
            max_retries_on_429,
            default_timeout=timeout,
        )

        self.authenticate()

    def get_headers_frigidaire(self, method: str, include_bearer_token: bool) -> dict[str, str]:
        to_return = {
            "x-api-key": FRIGIDAIRE_API_KEY,
            "Authorization": "Bearer"
            if not (self.session_key and include_bearer_token)
            else f"Bearer {self.session_key}",
            "Accept": "application/json",
            "Accept-Charset": "UTF-8",
            "User-Agent": FRIGIDAIRE_USER_AGENT,
        }
        if method.upper() != "GET":
            to_return["Content-Type"] = "application/json"
        return to_return

    @staticmethod
    def get_headers_auth(method: str) -> dict[str, str]:
        to_return = {"User-Agent": AUTH_USER_AGENT, "Accept-Encoding": "gzip", "connection": "close"}
        if method.upper() != "GET":
            to_return["Content-Type"] = "application/x-www-form-urlencoded"
        return to_return

    def test_connection(self) -> None:
        """
        Tests for successful connectivity to the Frigidaire server
        :return:
        """
        self.get_request(
            self.regional_base_url,
            "/one-account-user/api/v1/users/current?countryDetails=true",
            self.get_headers_frigidaire("GET", include_bearer_token=True),
        )

    def authenticate(self) -> None:
        """
        Authenticates with the Frigidaire API

        This will re-authenticate if the session key is deemed invalid

        Will throw an exception if the authentication request fails or returns an unexpected response
        :return:
        """

        if not self.regional_base_url:
            self.session_key = None

        # Remember to include "Context-Brand: frigidaire" in the headers for
        # the "/api/v1/identity-providers" and "/api/v1/users/current" calls
        if self.session_key:
            _LOGGER.debug("Authentication requested but session key is present, testing session key")
            try:
                self.test_connection()
                _LOGGER.debug("Session key is still valid, doing nothing")
                return None
            except (FrigidaireException, ConnectionError):
                _LOGGER.debug("Session key is invalid, re-authenticating")
                self.session_key = None

        if self.refresh_token and self.regional_base_url:
            # Session keys last 12 hours. The refresh token Electrolux issued with the last
            # one mints the next without the password, which Home Assistant does not keep.
            if self._refresh_session():
                return None

        if not self.password:
            # The caller holds no password (Home Assistant keeps it out of the config
            # entry once a session key exists) and no working refresh token, so a full
            # login is impossible. Say so structurally instead of POSTing an empty
            # password to Gigya.
            raise FrigidaireException(
                "Session expired and no password is held; the account must be re-authenticated",
                status_code=401,
                error_code="reauth_required",
            )

        data = {"grantType": "client_credentials", "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET, "scope": ""}
        session_key_response = self._post_dict(
            GLOBAL_API_URL,
            "/one-account-authorization/api/v1/token",
            self.get_headers_frigidaire("POST", include_bearer_token=False),
            data,
        )
        self.session_key = _require_field(session_key_response, "accessToken", "client-credentials response")

        identity_providers_response = self._get_list_of_dicts(
            GLOBAL_API_URL,
            f"/one-account-user/api/v1/identity-providers?brand=frigidaire&countryCode={self.country_code}",
            self.get_headers_frigidaire("GET", include_bearer_token=True),
        )
        # These three values come out of a cloud response and decide where the account
        # password is sent, so they are validated before they are used, not after.
        if not identity_providers_response or not isinstance(identity_providers_response[0], dict):
            raise FrigidaireException(
                f"Failed to authenticate: no identity provider returned for country {self.country_code}"
            )
        provider = identity_providers_response[0]
        identity_domain = _validate_identity_domain(provider.get("domain"))
        identity_api_key = provider.get("apiKey")
        if not isinstance(identity_api_key, str) or not identity_api_key:
            raise FrigidaireException("Failed to authenticate: identity provider returned no apiKey")
        self.regional_base_url = _validate_api_base_url(provider.get("httpRegionalBaseUrl"))

        data = {
            "apiKey": identity_api_key,
            "format": "json",
            "httpStatusCodes": "false",
            "nonce": _generate_nonce(),
            "sdk": "Android_6.2.1",
            "targetEnv": "mobile",
        }
        get_ids_response = self._post_dict(
            f"https://socialize.{identity_domain}",
            "/socialize.getIDs",
            self.get_headers_auth("POST"),
            data,
            form_encoding=True,
        )

        auth_gmid = _require_field(get_ids_response, "gmid", "socialize.getIDs response")
        auth_ucid = _require_field(get_ids_response, "ucid", "socialize.getIDs response")

        data = {
            "apiKey": identity_api_key,
            "format": "json",
            "gmid": auth_gmid,
            "httpStatusCodes": "false",
            "loginID": self.username,
            "nonce": _generate_nonce(),
            "password": self.password,
            "sdk": "Android_6.2.1",
            "targetEnv": "mobile",
            "ucid": auth_ucid,
        }
        login_response = self._post_dict(
            f"https://accounts.{identity_domain}",
            "/accounts.login",
            self.get_headers_auth("POST"),
            data,
            form_encoding=True,
        )

        session_info = login_response.get("sessionInfo")
        if (
            session_info is None
            or session_info.get("sessionToken") is None
            or session_info.get("sessionSecret") is None
        ):
            # Report the shape, never the body: this response carries session tokens, and
            # this message ends up in the caller's log. Gigya reports a wrong password as
            # errorCode 403042 (and other 4030xx codes), so say so structurally rather
            # than leaving the caller to match on the wording.
            error_code = login_response.get("errorCode")
            invalid_credentials = str(error_code).startswith("4030")
            raise FrigidaireException(
                "Failed to authenticate: sessionInfo missing or incomplete "
                f"(response keys: {sorted(login_response)}, errorCode: {error_code})",
                status_code=401 if invalid_credentials else None,
                error_code="invalid_credentials" if invalid_credentials else None,
            )

        auth_session_token = session_info["sessionToken"]
        auth_session_secret = session_info["sessionSecret"]

        data = {
            "apiKey": identity_api_key,
            "fields": "country",
            "format": "json",
            "gmid": auth_gmid,
            "httpStatusCodes": "false",
            "nonce": _generate_nonce(),
            "oauth_token": auth_session_token,
            "sdk": "Android_6.2.1",
            "targetEnv": "mobile",
            "timestamp": str(int(time.time())),
            "ucid": auth_ucid,
        }
        sig = get_signature(auth_session_secret, "POST", f"https://accounts.{identity_domain}/accounts.getJWT", data)
        if sig is None:
            raise FrigidaireException("Failed to compute request signature for accounts.getJWT")
        data["sig"] = sig
        jwt_response = self._post_dict(
            f"https://accounts.{identity_domain}",
            "/accounts.getJWT",
            self.get_headers_auth("POST"),
            data,
            form_encoding=True,
        )

        auth_jwt = _require_field(jwt_response, "id_token", "accounts.getJWT response")

        data = {
            "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
            "clientId": CLIENT_ID,
            "idToken": auth_jwt,
            "scope": "",
        }
        frigidaire_auth_response = self._post_dict(
            self.regional_base_url,
            "/one-account-authorization/api/v1/token",
            self.get_headers_frigidaire("POST", include_bearer_token=False),
            data,
        )

        access_token = frigidaire_auth_response.get("accessToken")
        if access_token is None:
            # Same reasoning as above: the token-exchange body can carry a refresh token.
            raise FrigidaireException(
                "Failed to authenticate: accessToken missing from token response "
                f"(response keys: {sorted(frigidaire_auth_response)})"
            )

        _LOGGER.debug("Authentication successful, storing new session key")
        self.session_key = access_token
        self.refresh_token = frigidaire_auth_response.get("refreshToken") or None
        self._emit_session_key_update()

    def _refresh_session(self) -> bool:
        """Mint a new session key from the refresh token, without the password.

        Same endpoint as the login's final token exchange, grantType refresh_token
        (the shape Electrolux's own app uses). Electrolux rotates the refresh token on
        each use, so the new one is stored and persisted with the new session key.
        Returns False, and drops the refresh token, when Electrolux refuses it: the
        caller then falls back to a password login or to asking for one.
        """
        assert self.refresh_token is not None
        data = {"grantType": "refresh_token", "clientId": CLIENT_ID, "refreshToken": self.refresh_token, "scope": ""}
        try:
            response = self._post_dict(
                self.regional_base_url,
                "/one-account-authorization/api/v1/token",
                self.get_headers_frigidaire("POST", include_bearer_token=False),
                data,
            )
        except (FrigidaireException, ConnectionError) as err:
            _LOGGER.debug("Refresh token rejected (%s); a full login is needed", getattr(err, "status_code", "?"))
            self.refresh_token = None
            return False
        access_token = response.get("accessToken")
        if not access_token:
            _LOGGER.debug("Refresh response carried no accessToken (keys: %s)", sorted(response))
            self.refresh_token = None
            return False
        _LOGGER.debug("Session refreshed, storing new session key")
        self.session_key = access_token
        self.refresh_token = response.get("refreshToken") or self.refresh_token
        self._emit_session_key_update()
        return True

    def _emit_session_key_update(self) -> None:
        """Notify the caller of a freshly minted session key so it can be persisted.

        Best-effort: a failing callback must never sink authentication.
        """
        if self._on_session_key_update is None or self.session_key is None:
            return
        try:
            self._on_session_key_update(self.session_key, self.regional_base_url, self.refresh_token)
        except Exception:
            _LOGGER.exception("on_session_key_update callback failed")

    def close(self) -> None:
        """Release the HTTP session and its connection pool.

        The scoped limiter and re-auth lock are deliberately left in place: another live
        client for the same account may still be spacing its requests against them.
        """
        self._session.close()

    def re_authenticate(self) -> None:
        """
        Removes the session_key and tries to authenticate again
        :return:
        """
        self.session_key = None
        self.authenticate()

    def _post_dict(
        self, url: str | None, path: str, headers: dict[str, str], data: dict, form_encoding: bool = False
    ) -> dict:
        return cast(dict, self.post_request(url, path, headers, data, form_encoding))

    def _get_list_of_dicts(self, url: str | None, path: str, headers: dict[str, str]) -> list[dict]:
        return cast(list[dict], self.get_request(url, path, headers))

    @staticmethod
    def _is_session_cap(e: FrigidaireException) -> bool:
        """Whether an exception is the Electrolux active-session cap (cas_3403)."""
        return e.error_code == "cas_3403"

    def _with_reauth(self, fn: Callable[[], T]) -> T:
        """Run fn(), retrying on the existing session before falling back to re-authentication.

        Re-authenticating mints a new server-side session, and Electrolux caps active
        sessions (cas_3403). Because tokens stay valid for a long time, an abandoned
        session lingers and these accumulate until the account is locked out. A transient
        failure (timeout, 5xx) does not mean our session is invalid, so we first retry the
        same request on the existing session and only re-authenticate if that also fails.
        cas_3403 is never retried or re-authenticated — that only makes things worse.

        Re-authentication is serialised by a per-account lock: a thread that failed while
        another was minting a new session reuses that session instead of minting its own.

        The number of retries and any delay between them are configurable via
        ``session_max_retries`` and ``session_retry_backoff``.
        """
        last_attempt = self._session_max_retries
        for attempt in range(last_attempt + 1):
            key_before = self.session_key
            try:
                return fn()
            except FrigidaireException as e:
                if self._is_session_cap(e):
                    _LOGGER.debug("Rate limited - try again later")
                    raise
                if attempt == last_attempt:
                    raise
                if attempt == last_attempt - 1:
                    with self._reauth_lock:
                        if self.session_key == key_before:
                            _LOGGER.debug("Retry failed - attempting to re-authenticate")
                            self.re_authenticate()
                        else:
                            _LOGGER.debug("Another request already re-authenticated - retrying with the new session")
                else:
                    _LOGGER.debug("Request failed - retrying on the existing session")
                if self._session_retry_backoff:
                    time.sleep(self._session_retry_backoff * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _fetch_raw_appliances(self) -> list[dict]:
        return self._get_list_of_dicts(
            self.regional_base_url,
            "/appliance/api/v2/appliances?includeMetadata=true",
            self.get_headers_frigidaire("GET", include_bearer_token=True),
        )

    def get_appliances(self) -> list[Appliance]:
        """
        Uses the Frigidaire API to fetch the list of appliances
        Will authenticate if the request fails
        :return: The appliances that are associated with the Frigidaire account
        """
        _LOGGER.debug("Listing appliances")

        def fetch() -> list[Appliance]:
            appliances = []
            for raw in self._fetch_raw_appliances():
                try:
                    appliance = Appliance(raw)
                except (ValueError, KeyError, TypeError):
                    # One unparseable record must not cost the caller every other
                    # appliance on the account.
                    _LOGGER.warning(
                        "Skipping unparseable appliance record with keys %s",
                        sorted(raw) if isinstance(raw, dict) else type(raw).__name__,
                    )
                    continue
                if appliance.destination is not None:
                    appliances.append(appliance)
            return appliances

        return self._with_reauth(fetch)

    def get_appliances_raw(self) -> list[dict]:
        """
        Fetch the complete raw record of every appliance on the account in one request.
        Will authenticate if the request fails.

        Integrations that poll several appliances should call this once per cycle and
        pick records out by "applianceId" instead of calling get_appliance_raw() or
        get_appliance_details() per appliance, which each repeat the same request.
        :return: The full raw appliance records
        """
        _LOGGER.debug("Getting raw records for every appliance")
        return self._with_reauth(self._fetch_raw_appliances)

    def get_appliance_raw(self, appliance: Appliance) -> dict:
        """
        Uses the Frigidaire API to fetch the complete raw record for a given appliance.
        Will authenticate if the request fails

        Unlike get_appliance_details(), this keeps the keys that live alongside
        "properties" — notably "connectionState" and "status" — which callers need in order
        to tell a genuinely offline appliance from stale reported values.

        :param appliance: The appliance to request from the API
        :return: The full raw appliance record
        """
        _LOGGER.debug(f"Getting raw appliance record for appliance {appliance.nickname}")
        for raw_appliance in self.get_appliances_raw():
            if raw_appliance["applianceId"] == appliance.appliance_id:
                return raw_appliance
        raise FrigidaireException(f"Appliance {appliance.nickname} not found in list of appliances")

    def get_appliance_details(self, appliance: Appliance) -> dict:
        """
        Uses the Frigidaire API to fetch details for a given appliance
        Will authenticate if the request fails
        :param appliance: The appliance to request from the API
        :return: The details for the passed in appliance
        """
        _LOGGER.debug(f"Getting appliance details for appliance {appliance.nickname}")
        return self.get_appliance_raw(appliance)["properties"]["reported"]

    def execute_action(self, appliance: Appliance, action: list[Component]) -> None:
        """
        Executes any defined action on a given appliance
        Will authenticate if the request fails
        :param appliance: The appliance to perform the action on
        :param action: The action to be performed
        :return:
        """
        path = f"/appliance/api/v2/appliances/{appliance.appliance_id}/command"
        for component in action:
            data = {component.name: component.value}

            def send(data: dict = data) -> None:
                # Headers are built per attempt, not captured: _with_reauth retries this
                # closure after minting a new session key, and headers built once outside
                # would keep sending the dead bearer token on every retry.
                headers = self.get_headers_frigidaire("PUT", include_bearer_token=True)
                self.put_request(self.regional_base_url, path, headers, data)

            self._with_reauth(send)

    @staticmethod
    def parse_response(response: Response) -> dict:
        """
        Parses a response from the Frigidaire API
        :param response: The raw response from the requests lib
        :return: The data in the response, if the response was successful and there is data present
        """
        if response.status_code != 200:
            # Extract the platform error code (e.g. cas_3403) so callers can classify the
            # failure structurally instead of scanning the traceback string.
            error_code: str | None = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    error_code = body.get("error")
            except Exception:
                pass
            # The body is deliberately not in the message. It reaches the caller's log
            # through __cause__ chains, and an auth endpoint's error body can carry a
            # regToken. The status and the platform error code are what callers act on.
            raise FrigidaireException(
                f"Request failed with status {response.status_code} "
                f"(error={error_code}, {len(response.content or b'')} bytes)",
                status_code=response.status_code,
                error_code=error_code,
            )

        try:
            if response.headers.get("Content-Encoding") == "gzip":
                # Hack: Often, the server indicates "Content-Encoding: gzip" but does not send gzipped data
                try:
                    data = gzip.decompress(response.content)
                    response_dict = json.loads(data.decode("utf-8"))
                except gzip.BadGzipFile:
                    response_dict = response.json()
            elif response.content == b"":
                # The server says it was JSON, but it was not
                response_dict = {}
            else:
                response_dict = response.json()
        except Exception as e:
            # Same reasoning: report the shape, not the body.
            _LOGGER.debug("Could not decode a Frigidaire response: %s", e)
            raise FrigidaireException(
                f"Received an unexpected response: {type(e).__name__} decoding "
                f"{len(response.content or b'')} bytes of "
                f"{response.headers.get('Content-Type', 'an unknown content type')}"
            ) from e

        return response_dict

    @staticmethod
    def handle_request_exception(
        e: Exception, method: str, fullpath: str, headers: dict[str, str], payload: str
    ) -> NoReturn:
        # Don't log `e` directly: parse_response wraps response bodies into the
        # exception message, and auth-endpoint bodies contain tokens. Callers
        # who need it can inspect __cause__ on the raised exception.
        safe_headers = _redact_headers(headers)
        safe_payload = _redact_payload(payload)
        error_str = (
            f"Error processing request ({type(e).__name__}):\n"
            f"{method} {fullpath}\nheaders={safe_headers}\npayload={safe_payload}\n"
        )
        _LOGGER.warning(error_str)
        # Preserve structured error info from the wrapped exception so re-auth logic
        # downstream can still recognise the failure class (e.g. the cas_3403 session cap).
        raise FrigidaireException(
            error_str,
            status_code=getattr(e, "status_code", None),
            error_code=getattr(e, "error_code", None),
        ) from e

    def get_request(self, url: str | None, path: str, headers: dict[str, str]) -> dict | list:
        """
        Makes a get request to the Frigidaire API and parses the result
        :param url: Base URL for the request (no slashes)
        :param path: The path to the resource, including query params
        :param headers: Headers to include in the request
        :return: The contents of 'data' in the resulting json
        """
        # Validated outside the try so the refusal reaches the caller as itself rather
        # than as a generic request failure.
        full_url = _validate_request_url(f"{url}{path}")
        try:
            # No verify= argument anywhere in this class: requests validates the
            # certificate chain and hostname against the system trust store.
            # allow_redirects=False because the allowlist checks the URL this client
            # builds, not where a 3xx would send it: requests replays the body of a
            # 307/308 to the redirect target, which for /accounts.login is the account
            # password. Electrolux does not redirect in this flow, so a 3xx is an error
            # and parse_response reports it as one.
            response = self._session.get(full_url, headers=headers, allow_redirects=False)
            return self.parse_response(response)
        except Exception as e:
            self.handle_request_exception(e, "GET", f"{url}{path}", headers, "")

    def post_request(
        self, url: str | None, path: str, headers: dict[str, str], data: dict, form_encoding: bool = False
    ) -> dict | list:
        """
        Makes a post request to the Frigidaire API and parses the result
        :param url: Base URL for the request (no slashes)
        :param path: The path to the resource, including query params
        :param headers: Headers to include in the request
        :param data: The data to include in the body of the request
        :param form_encoding: Whether to form-encode data. If false, encodes as json
        :return: The contents of 'data' in the resulting json
        """
        full_url = _validate_request_url(f"{url}{path}")
        try:
            encoded_data = urlencode(data) if form_encoding else json.dumps(data)
            # allow_redirects=False: see get_request. This is the call that carries the
            # password, so a redirect must never be followed.
            response = self._session.post(full_url, data=encoded_data, headers=headers, allow_redirects=False)
            return self.parse_response(response)
        except Exception as e:
            self.handle_request_exception(e, "POST", f"{url}{path}", headers, json.dumps(data))

    def put_request(self, url: str | None, path: str, headers: dict[str, str], data: dict) -> dict | list:
        """
        Makes a put request to the Frigidaire API and parses the result
        :param url: Base URL for the request (no slashes)
        :param headers: Headers to include in the request
        :param path: The path to the resource, including query params
        :param data: The data to include in the body of the request
        :return: The contents of 'data' in the resulting json
        """
        encoded_data = json.dumps(data)
        full_url = _validate_request_url(f"{url}{path}")
        try:
            # allow_redirects=False: see get_request.
            response = self._session.put(full_url, data=encoded_data, headers=headers, allow_redirects=False)
            return self.parse_response(response)
        except Exception as e:
            self.handle_request_exception(e, "PUT", f"{url}{path}", headers, encoded_data)
