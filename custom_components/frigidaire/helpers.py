"""Shared helpers for the frigidaire integration."""

from __future__ import annotations

import socket
from typing import Any

import requests

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


def _network_failure_in_chain(err: BaseException) -> bool:
    """Whether err, or anything it was raised from, is a transport failure.

    The vendored client wraps a requests ConnectionError or Timeout in a
    FrigidaireException (``raise ... from e``), and when that happens inside the
    re-authentication step the wording can look like an authentication failure. A
    transport failure never means the stored credentials are wrong.
    """
    seen: set[int] = set()
    e: BaseException | None = err
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        if isinstance(e, (ConnectionError, TimeoutError, socket.timeout)):
            return True
        e = e.__cause__ or e.__context__
    return False


def is_auth_failure(err: Exception) -> bool:
    """Whether a library error means the stored credentials no longer work.

    Structural only: the vendored client sets status_code and error_code on
    FrigidaireException for every answer Electrolux or Gigya gives about credentials
    (a wrong password is errorCode 4030xx -> ``invalid_credentials`` with status 401;
    an expired session is a 401/403 from the API). The messages that merely say
    "Failed to authenticate" without a code are malformed or missing responses (no
    identity provider, sessionInfo absent with no error code, accessToken missing),
    which is exactly what an internet outage produces. Until 0.2.5 those words were
    matched as a fallback and a three-minute outage on 2026-09-16 asked for the
    password; a transport failure anywhere in the exception chain now settles it as
    "cannot connect" before anything else is looked at.
    """
    if _network_failure_in_chain(err):
        return False
    error_code = getattr(err, "error_code", None)
    if error_code == "cas_3403":
        # The active-session cap. It can arrive with a 4xx status, but the credentials are
        # fine — prompting for the password would be wrong, and re-authenticating to
        # "fix" it mints yet another session and makes it worse.
        return False
    if error_code in ("invalid_credentials", "reauth_required"):
        return True
    return getattr(err, "status_code", None) in (401, 403)


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
