"""The diagnostics platform: enough detail to debug with, no account identifiers."""

import json

from homeassistant.core import HomeAssistant
from payloads import DEHUMIDIFIER, LEGACY_AC
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.frigidaire.diagnostics import async_get_config_entry_diagnostics


async def _diagnostics(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return await async_get_config_entry_diagnostics(hass, entry)


async def test_diagnostics_describe_every_appliance(hass: HomeAssistant, setup_entry) -> None:
    entry, _stub = await setup_entry([LEGACY_AC, DEHUMIDIFIER])

    result = await _diagnostics(hass, entry)

    assert [a["nickname"] for a in result["appliances"]] == ["Bedroom AC", "Basement Dehumidifier"]
    assert [a["destination"] for a in result["appliances"]] == ["AC", "DH"]
    assert result["appliances"][0]["connection_state"] == "CONNECTED"
    assert result["appliances"][0]["reported"]["mode"] == "COOL"
    assert result["account"]["last_update_success"] is True


async def test_diagnostics_redact_the_account_and_the_hardware_ids(hass: HomeAssistant, setup_entry) -> None:
    entry, _stub = await setup_entry([LEGACY_AC])

    dumped = json.dumps(await _diagnostics(hass, entry))

    assert "user@example.com" not in dumped
    assert "AC-LEGACY-1" not in dumped
    assert "secret" not in dumped


async def test_diagnostics_redact_identifiers_nested_in_reported_properties(hass: HomeAssistant, setup_entry) -> None:
    record = json.loads(json.dumps(LEGACY_AC))
    record["properties"]["reported"]["serialNumber"] = "SERIAL-12345"
    entry, _stub = await setup_entry([record])

    result = await _diagnostics(hass, entry)

    assert result["appliances"][0]["reported"]["serialNumber"] == "**REDACTED**"


async def test_diagnostics_do_not_leak_the_id_through_a_missing_nickname(hass: HomeAssistant, setup_entry) -> None:
    """An appliance with no applianceName is nicknamed after its id by the client."""
    record = json.loads(json.dumps(LEGACY_AC))
    del record["applianceData"]["applianceName"]
    entry, _stub = await setup_entry([record])

    dumped = json.dumps(await _diagnostics(hass, entry))

    assert "AC-LEGACY-1" not in dumped
