"""Config flow for frigidaire integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .auth_store import per_entry_auth_path, save_auth, shared_auth_path
from .const import (
    BINARY_SENSOR_OPTIONS,
    CONF_COMPRESSOR_ESTIMATE,
    CONF_COMPRESSOR_OFF_DELAY,
    CONF_COOL_HYSTERESIS,
    CONFIG_ENTRY_VERSION,
    DEFAULT_COMPRESSOR_OFF_DELAY,
    DEFAULT_COOL_HYSTERESIS,
    DOMAIN,
    SENSOR_OPTIONS,
    SWITCH_OPTIONS,
)
from .helpers import is_auth_failure
from .vendor import frigidaire

_LOGGER = logging.getLogger(__name__)

# vol.Required: a plain-key Schema makes its keys optional, so an empty password field
# reached validate_input as a missing key and surfaced as "unknown" (KeyError) instead of
# the form refusing to submit (seen 2026-09-07 on first install).
STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})

ALL_OPTIONS = {**SWITCH_OPTIONS, **BINARY_SENSOR_OPTIONS, **SENSOR_OPTIONS}


def _device_schema(current: dict, appliance: frigidaire.Appliance | None = None) -> vol.Schema:
    fields: dict = {vol.Optional(key, default=current.get(key, False)): bool for key in ALL_OPTIONS}

    if appliance is not None and appliance.destination == frigidaire.Destination.AIR_CONDITIONER:
        fields[vol.Optional(CONF_COMPRESSOR_ESTIMATE, default=current.get(CONF_COMPRESSOR_ESTIMATE, False))] = bool
        fields[
            vol.Optional(
                CONF_COOL_HYSTERESIS,
                default=float(current.get(CONF_COOL_HYSTERESIS, DEFAULT_COOL_HYSTERESIS)),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=10))
        fields[
            vol.Optional(
                CONF_COMPRESSOR_OFF_DELAY,
                default=int(current.get(CONF_COMPRESSOR_OFF_DELAY, DEFAULT_COMPRESSOR_OFF_DELAY)),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=0, max=3600))

    return vol.Schema(fields)


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any], entry_id: str | None = None
) -> list[frigidaire.Appliance]:
    """Validate credentials and return list of appliances.

    ``entry_id`` is passed when re-authenticating an existing entry: the fresh session key
    then lands in that entry's own file, which is the one setup reads.
    """

    def setup(username: str, password: str) -> list[frigidaire.Appliance]:
        # Staged under .storage until the entry exists and gets its own file.
        auth_path = (
            shared_auth_path(hass.config.path())
            if entry_id is None
            else per_entry_auth_path(hass.config.path(), entry_id)
        )

        try:
            # No cached session key, ever, on either path. authenticate() returns early
            # when the session it is handed still works, so reusing one here would mean
            # the password the user just typed is never checked — a wrong password would
            # be accepted, and adding a second account would silently reuse the first
            # account's session. The cost is one freshly minted session per validation.
            client = frigidaire.Frigidaire(
                username=username,
                password=password,
                timeout=30,
                session_key=None,
                regional_base_url=None,
                session_max_retries=1,
            )
            save_auth(auth_path, client.session_key, client.regional_base_url, getattr(client, "refresh_token", None))

            return client.get_appliances()
        except frigidaire.FrigidaireException as err:
            # Structural first; the library's wording is not a stable contract.
            if is_auth_failure(err):
                raise InvalidAuth from err

            raise CannotConnect from err

    appliances = await hass.async_add_executor_job(setup, data[CONF_USERNAME], data[CONF_PASSWORD])

    if len(appliances) == 0:
        raise NoAppliances

    return appliances


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for frigidaire."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}
        self._appliances: list[frigidaire.Appliance] = []
        self._pending_appliances: list[frigidaire.Appliance] = []
        self._options: dict[str, dict[str, Any]] = {}
        self._reauth_username: str = ""

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

        errors = {}

        try:
            appliances = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except NoAppliances:
            errors["base"] = "no_appliances"
        except (KeyError, TypeError, ValueError) as err:
            # A malformed appliance record from the cloud. The message is safe to log; the
            # exception chain is not, since a wrapped response body can carry a token.
            _LOGGER.error("Could not parse the Frigidaire appliance list: %s", err)
            errors["base"] = "unknown"
        except Exception:  # noqa: BLE001 - last resort, must not leave the flow hanging
            _LOGGER.exception("Unexpected error validating Frigidaire credentials")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(user_input["username"].lower())
            self._abort_if_unique_id_configured()
            self._user_input = user_input
            self._appliances = appliances
            self._pending_appliances = list(appliances)
            return await self._async_next_device_step()

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    async def _async_next_device_step(self) -> FlowResult:
        if not self._pending_appliances:
            # Only the username is persisted. validate_input has already stored the
            # session key, which is the credential the integration actually runs on; the
            # password would otherwise sit in cleartext in .storage/core.config_entries
            # and in every backup. If the session ever dies, async_step_reauth asks again.
            return self.async_create_entry(
                title="Frigidaire",
                data={CONF_USERNAME: self._user_input[CONF_USERNAME]},
                options=self._options,
            )
        return await self.async_step_device()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Start reauth when the stored session cannot be refreshed and no password is held."""
        self._reauth_username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask for the current password and swap in a fresh session key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            candidate = {CONF_USERNAME: self._reauth_username, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await validate_input(self.hass, candidate, entry_id=entry.entry_id)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (CannotConnect, NoAppliances):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - last resort, must not leave the flow hanging
                _LOGGER.exception("Unexpected error re-authenticating with Frigidaire")
                errors["base"] = "unknown"
            else:
                # data, not data_updates: this also drops the plaintext password an
                # older version of the integration left in the entry.
                return self.async_update_reload_and_abort(entry, data={CONF_USERNAME: self._reauth_username})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": self._reauth_username},
            errors=errors,
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show switch checkboxes for the current appliance in the queue."""
        appliance = self._pending_appliances[0]

        if user_input is not None:
            self._options[appliance.appliance_id] = user_input
            self._pending_appliances.pop(0)
            return await self._async_next_device_step()

        schema = _device_schema({}, appliance)
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            description_placeholders={"device_name": appliance.nickname},
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for the frigidaire integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry_id = config_entry.entry_id
        self._appliances: list[frigidaire.Appliance] = []
        self._pending_appliances: list[frigidaire.Appliance] = []
        self._options: dict[str, dict[str, Any]] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Load appliances then start per-device steps."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
        if entry is None or runtime is None:
            # The entry is not loaded (bad credentials, cloud outage). The options are
            # per appliance and the appliance list comes from the running integration, so
            # there is nothing to show — say that rather than raising KeyError at the user.
            return self.async_abort(reason="entry_not_loaded")
        self._appliances = runtime["appliances"]
        self._pending_appliances = list(self._appliances)
        self._options = dict(entry.options)
        return await self._async_next_device_step()

    async def _async_next_device_step(self) -> FlowResult:
        if not self._pending_appliances:
            return self.async_create_entry(title="", data=self._options)
        return await self.async_step_device()

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show switch checkboxes for the current appliance in the queue."""
        appliance = self._pending_appliances[0]
        current = self._options.get(appliance.appliance_id, {})

        if user_input is not None:
            self._options[appliance.appliance_id] = {**current, **user_input}
            self._pending_appliances.pop(0)
            return await self._async_next_device_step()

        schema = _device_schema(current, appliance)
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            description_placeholders={"device_name": appliance.nickname},
        )


class NoAppliances(HomeAssistantError):
    """Error to indicate there are no appliances."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
