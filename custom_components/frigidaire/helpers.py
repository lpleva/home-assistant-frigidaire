"""Shared helpers for the frigidaire integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar

from .const import DOMAIN
from .vendor import frigidaire


def normalize_enum_value(value: Any) -> Any:
    """Upper-case API strings so they compare equal to the library's str enums.

    Newer appliance firmware reports "running" where older units report "RUNNING".
    """
    if isinstance(value, str):
        return value.upper()
    return value


def is_auth_failure(err: Exception) -> bool:
    """Whether a library error means the stored credentials no longer work.

    Structural first: the client sets status_code and error_code on FrigidaireException.
    The message match is a fallback for wording this fork does not control, not the
    contract — classifying on the English text alone silently turns every wrong password
    into "cannot connect" the moment that wording changes.
    """
    error_code = getattr(err, "error_code", None)
    if error_code == "cas_3403":
        # The active-session cap. It can arrive with a 4xx status, but the credentials are
        # fine — prompting for the password would be wrong, and re-authenticating to
        # "fix" it mints yet another session and makes it worse.
        return False
    if error_code in ("invalid_credentials", "reauth_required"):
        return True
    if getattr(err, "status_code", None) in (401, 403):
        return True
    return "Failed to authenticate" in str(err)


def suggest_area(hass: HomeAssistant, nickname: str) -> str | None:
    """Return the longest area name that appears in the appliance nickname, or None."""
    registry = ar.async_get(hass)
    nickname_lower = nickname.lower()

    match = max(
        (area.name for area in registry.areas.values() if area.name.lower() in nickname_lower),
        key=len,
        default=None,
    )
    return match


def execute_or_raise(client: Any, appliance: Any, action: Any, device_name: str | None) -> None:
    """Send a command, turning a refused one into a sentence the dashboard can show.

    The client raises FrigidaireException for any non-2xx answer (Electrolux refuses a
    target humidity in Auto mode with a 400, for one). Left alone, that reaches the
    frontend as "Unexpected exception" plus a traceback in the log; wrapped, it reads
    "Dehumidifier refused the command: ...".
    """
    try:
        client.execute_action(appliance, action)
    except frigidaire.FrigidaireException as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_refused",
            translation_placeholders={"device": device_name or "The appliance", "error": str(err)},
        ) from err
