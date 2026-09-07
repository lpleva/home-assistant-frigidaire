"""Config-entry setup and teardown against stubbed appliances."""

import json
import os
import stat
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from payloads import DEHUMIDIFIER, LEGACY_AC, with_reported
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from vendor import frigidaire

DOMAIN = "frigidaire"


def entity_id_for(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


async def test_setup_creates_expected_entities_for_each_appliance(hass: HomeAssistant, setup_entry) -> None:
    entry, _stub = await setup_entry([LEGACY_AC, DEHUMIDIFIER])

    assert entry.state is ConfigEntryState.LOADED
    assert entity_id_for(hass, "climate", "AC-LEGACY-1") is not None
    assert entity_id_for(hass, "number", "AC-LEGACY-1_timer_on") is not None
    assert entity_id_for(hass, "number", "AC-LEGACY-1_timer_off") is not None
    assert entity_id_for(hass, "humidifier", "DH-1") is not None
    # Connectivity is created for every appliance that reports connectionState, and the
    # dehumidifier's reported sensorHumidity gets a humidity sensor; with no options enabled
    # and no temperature on the dehumidifier, nothing else appears.
    assert entity_id_for(hass, "binary_sensor", "AC-LEGACY-1_connectivity") is not None
    assert entity_id_for(hass, "binary_sensor", "DH-1_connectivity") is not None
    assert entity_id_for(hass, "sensor", "DH-1_humidity") is not None
    registry = er.async_get(hass)
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 7


async def test_dehumidifier_reporting_temperature_gets_temperature_sensor(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([with_reported(DEHUMIDIFIER, ambientTemperatureF=68, temperatureRepresentation="FAHRENHEIT")])

    sensor_id = entity_id_for(hass, "sensor", "DH-1_temperature")
    assert sensor_id is not None
    assert hass.states.get(sensor_id).state == "68"


async def test_unload_entry_cleans_up(hass: HomeAssistant, setup_entry) -> None:
    entry, _stub = await setup_entry([LEGACY_AC])
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_session_cap_during_setup_reports_rate_limit(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    stub = frigidaire_stub([LEGACY_AC])
    stub.appliances_error = frigidaire.FrigidaireException("Request failed", status_code=429, error_code="cas_3403")
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "secret"}, unique_id="user@example.com"
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason == "Rate limited by Frigidaire. Will retry automatically."


async def test_other_api_failure_during_setup_reports_status(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    stub = frigidaire_stub([LEGACY_AC])
    stub.appliances_error = frigidaire.FrigidaireException("Request failed", status_code=503)
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "secret"}, unique_id="user@example.com"
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason == "Frigidaire error during setup (status=503): Request failed"


async def test_record_without_properties_fails_the_poll(
    hass: HomeAssistant, setup_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed record must fail the poll, not succeed with empty data.

    Empty data leaves the climate entity's temperature_unit with nothing to map, and Home
    Assistant reads capability attributes before it checks availability, so the state write
    would raise and freeze the entity at its last value on every poll.
    """
    _entry, stub = await setup_entry([LEGACY_AC])
    del stub.records["AC-LEGACY-1"]["properties"]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(entity_id_for(hass, "climate", "AC-LEGACY-1")).state == "unavailable"
    assert "no reported properties" in caplog.text


async def test_session_key_is_written_owner_only_under_storage(hass: HomeAssistant, setup_entry, tmp_path) -> None:
    """The token alone drives the appliance, so it must not sit world-readable in the config root."""
    entry, _stub = await setup_entry([LEGACY_AC])

    path = tmp_path / ".storage" / f"frigidaire-{entry.entry_id}.json"
    assert path.is_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["session_key"] == "stub-session-key"
    assert not (tmp_path / f"frigidaire-{entry.entry_id}.json").exists()


async def test_a_pre_020_session_file_is_migrated_and_removed(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    """An existing user keeps their cached session instead of minting a new one (cas_3403)."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "frigidaire.json").write_text(
        json.dumps({"session_key": "old-key", "regional_base_url": "https://api.us.ocp.electrolux.one"})
    )
    stub = frigidaire_stub([LEGACY_AC])
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com", "password": "secret"}, unique_id="user@example.com"
    )
    entry.add_to_hass(hass)

    with patch.object(frigidaire, "Frigidaire", return_value=stub) as client_cls:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert client_cls.call_args.kwargs["session_key"] == "old-key"
    assert not (tmp_path / "frigidaire.json").exists()
    assert (tmp_path / ".storage" / f"frigidaire-{entry.entry_id}.json").is_file()


async def test_entity_ids_and_names_carry_the_device(hass: HomeAssistant, setup_entry) -> None:
    """Two appliances on one account must not produce binary_sensor.connectivity and _2."""
    entry, _stub = await setup_entry([LEGACY_AC, DEHUMIDIFIER])

    registry = er.async_get(hass)
    entity_ids = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}

    assert "binary_sensor.bedroom_ac_connectivity" in entity_ids
    assert "binary_sensor.basement_dehumidifier_connectivity" in entity_ids
    assert not any(entity_id.endswith("_2") for entity_id in entity_ids)
    # The primary entity keeps the device's own name rather than repeating it.
    assert hass.states.get("humidifier.basement_dehumidifier").attributes["friendly_name"] == ("Basement Dehumidifier")
    assert hass.states.get("binary_sensor.bedroom_ac_connectivity").attributes["friendly_name"] == (
        "Bedroom AC Connectivity"
    )


async def test_an_unexpected_setup_failure_is_logged_with_its_traceback(
    hass: HomeAssistant, frigidaire_stub, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Retrying forever with nothing in the log is how an integration bug stays invisible."""
    hass.config.config_dir = str(tmp_path)
    stub = frigidaire_stub([LEGACY_AC])
    stub.appliances_error = TypeError("something in here is broken")
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "user@example.com"}, unique_id="user@example.com")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "Unexpected error setting up Frigidaire" in caplog.text
    assert "TypeError: something in here is broken" in caplog.text
