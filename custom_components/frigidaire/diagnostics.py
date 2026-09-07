"""Diagnostics support for the frigidaire integration.

This filename is a reserved platform name: Home Assistant imports
``custom_components.frigidaire.diagnostics`` and calls
``async_get_config_entry_diagnostics`` from it. It used to hold value parsers instead,
which meant the download-diagnostics button did nothing and a reader looking for it found
the wrong thing. The parsers now live in ``parsers.py``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Account identifiers and anything that names the hardware. The reported properties are
# telemetry, but they are dumped wholesale here, so the redaction covers keys that could
# appear in them as well as the ones in the config entry.
TO_REDACT = {
    "username",
    "password",
    "session_key",
    "applianceId",
    "serialNumber",
    "pnc",
    "elc",
    "mac",
    "macAddress",
    "ssid",
    "deviceId",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators = data["coordinators"]

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": {
                # Keyed by appliance id, which is redacted above; index the devices the
                # same way they appear in the list below.
                f"appliance_{index}": dict(options)
                for index, options in enumerate(entry.options.values())
            },
        },
        "account": {
            "last_update_success": data["account"].last_update_success,
            "update_interval_seconds": (
                data["account"].update_interval.total_seconds() if data["account"].update_interval else None
            ),
        },
        "appliances": [
            {
                "nickname": appliance.nickname,
                "appliance_type": appliance.appliance_type,
                "destination": getattr(appliance.destination, "value", None),
                "connection_state": coordinators[appliance.appliance_id].connection_state,
                "reported": async_redact_data(coordinators[appliance.appliance_id].data or {}, TO_REDACT),
            }
            for appliance in data["appliances"]
        ],
    }
