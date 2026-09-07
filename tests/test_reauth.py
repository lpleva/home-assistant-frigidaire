"""Reauthentication: a dead session prompts for the password instead of retrying forever."""

import json
from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from payloads import DEHUMIDIFIER, LEGACY_AC
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from vendor import frigidaire

DOMAIN = "frigidaire"

INVALID_CREDENTIALS = frigidaire.FrigidaireException(
    "Failed to authenticate: sessionInfo missing or incomplete",
    status_code=401,
    error_code="invalid_credentials",
)


def _reauth_flows(hass: HomeAssistant) -> list[dict]:
    return [flow for flow in hass.config_entries.flow.async_progress() if flow["context"]["source"] == SOURCE_REAUTH]


async def test_invalid_credentials_at_setup_start_a_reauth_flow(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    stub = frigidaire_stub([LEGACY_AC])
    stub.appliances_error = INVALID_CREDENTIALS
    entry = MockConfigEntry(domain=DOMAIN, data={"username": "user@example.com"}, unique_id="user@example.com")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Not SETUP_RETRY: retrying cannot fix a wrong password, and the UI must say so.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1


async def test_invalid_credentials_during_a_poll_start_a_reauth_flow(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([LEGACY_AC])
    stub.details_error = INVALID_CREDENTIALS

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_reauth_flows(hass)) == 1


async def test_a_transient_failure_during_a_poll_does_not_start_a_reauth_flow(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([LEGACY_AC])
    stub.details_error = frigidaire.FrigidaireException("Request failed", status_code=503)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _reauth_flows(hass) == []


async def test_reauth_accepts_a_new_password_and_reloads_without_storing_it(
    hass: HomeAssistant, setup_entry, tmp_path
) -> None:
    entry, stub = await setup_entry([DEHUMIDIFIER])
    result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == "user@example.com"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "a-new-password"})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
    # The password is verified and then dropped: only the username is persisted.
    assert dict(entry.data) == {"username": "user@example.com"}
    # The fresh session key lands in the entry's own file, which is what setup reads.
    stored = json.loads((tmp_path / ".storage" / f"frigidaire-{entry.entry_id}.json").read_text())
    assert stored["session_key"] == "stub-session-key"


async def test_reauth_reports_a_still_wrong_password(hass: HomeAssistant, setup_entry) -> None:
    entry, stub = await setup_entry([DEHUMIDIFIER])
    result = await entry.start_reauth_flow(hass)
    stub.appliances_error = INVALID_CREDENTIALS

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "still-wrong"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_reports_a_connectivity_failure_separately(hass: HomeAssistant, setup_entry) -> None:
    entry, stub = await setup_entry([DEHUMIDIFIER])
    result = await entry.start_reauth_flow(hass)
    stub.appliances_error = frigidaire.FrigidaireException("Request failed", status_code=503)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "a-new-password"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_setup_drops_a_password_left_in_the_config_entry(hass: HomeAssistant, setup_entry) -> None:
    """Migration: entries created before 0.2.0 hold the password in cleartext."""
    entry, _stub = await setup_entry([DEHUMIDIFIER])

    assert entry.state is ConfigEntryState.LOADED
    assert "password" not in entry.data
    assert entry.data["username"] == "user@example.com"


async def test_the_session_cap_does_not_prompt_for_a_password(hass: HomeAssistant, setup_entry) -> None:
    """cas_3403 means too many sessions, not wrong credentials; re-auth would make it worse."""
    _entry, stub = await setup_entry([LEGACY_AC])
    stub.details_error = frigidaire.FrigidaireException("Request failed", status_code=403, error_code="cas_3403")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _reauth_flows(hass) == []


async def test_the_password_migration_runs_once_and_bumps_the_entry_version(hass: HomeAssistant, setup_entry) -> None:
    """A v1 entry is rewritten once by async_migrate_entry, not on every start."""
    entry, _stub = await setup_entry([DEHUMIDIFIER])

    assert entry.version == 2
    assert "password" not in entry.data

    with patch.object(hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry) as up:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert not [call for call in up.call_args_list if "data" in call.kwargs]


async def test_an_entry_from_a_newer_release_is_not_downgraded(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    frigidaire_stub([DEHUMIDIFIER])
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "user@example.com"}, unique_id="user@example.com", version=3
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
