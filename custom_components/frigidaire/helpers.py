"""Shared helpers for the frigidaire integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar


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
