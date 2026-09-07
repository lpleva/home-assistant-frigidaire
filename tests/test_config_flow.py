import pytest
"""The user config flow: credentials are checked, and only the username is kept."""

import json
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from payloads import DEHUMIDIFIER
from vendor import frigidaire

DOMAIN = "frigidaire"

USER_INPUT = {"username": "user@example.com", "password": "hunter2"}

INVALID_CREDENTIALS = frigidaire.FrigidaireException(
    "Failed to authenticate: sessionInfo missing or incomplete",
    status_code=401,
    error_code="invalid_credentials",
)


def _stage_a_session_key(tmp_path) -> None:
    """Write the file the config flow used to trust: a valid key for some other account."""
    storage = tmp_path / ".storage"
    storage.mkdir(exist_ok=True)
    (storage / "frigidaire.json").write_text(
        json.dumps({"session_key": "someone-elses-session", "regional_base_url": "https://api.us.ocp.electrolux.one"})
    )


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_a_successful_flow_stores_only_the_username(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    frigidaire_stub([DEHUMIDIFIER])

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    # One per-device step for the dehumidifier, then the entry.
    assert result["step_id"] == "device"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"username": "user@example.com"}
    assert "password" not in result["data"]


async def test_the_user_step_never_reuses_a_staged_session_key(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    """authenticate() returns early on a working session, so reuse would skip the password."""
    hass.config.config_dir = str(tmp_path)
    _stage_a_session_key(tmp_path)
    frigidaire_stub([DEHUMIDIFIER])

    result = await _start(hass)
    with patch.object(frigidaire, "Frigidaire", wraps=frigidaire.Frigidaire) as client_cls:
        await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert client_cls.call_args.kwargs["session_key"] is None
    assert client_cls.call_args.kwargs["regional_base_url"] is None


async def test_a_wrong_password_is_rejected_even_with_a_staged_session_key(
    hass: HomeAssistant, frigidaire_stub, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _stage_a_session_key(tmp_path)
    frigidaire_stub([DEHUMIDIFIER])

    result = await _start(hass)
    with patch.object(frigidaire, "Frigidaire", side_effect=INVALID_CREDENTIALS):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "user@example.com", "password": "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_connectivity_failure_is_reported_separately(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    frigidaire_stub([DEHUMIDIFIER])

    result = await _start(hass)
    with patch.object(
        frigidaire, "Frigidaire", side_effect=frigidaire.FrigidaireException("Request failed", status_code=503)
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_account_with_no_appliances_says_so(hass: HomeAssistant, frigidaire_stub, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    frigidaire_stub([])

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "no_appliances"}


async def test_a_malformed_appliance_list_is_reported_without_a_traceback(
    hass: HomeAssistant, frigidaire_stub, tmp_path, caplog
) -> None:
    hass.config.config_dir = str(tmp_path)
    stub = frigidaire_stub([DEHUMIDIFIER])
    stub.appliances_error = KeyError("applianceData")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "unknown"}
    assert "Could not parse the Frigidaire appliance list" in caplog.text


def test_user_schema_requires_both_fields() -> None:
    """An empty password must be rejected by the schema, not crash validate_input."""
    import voluptuous as vol
    from custom_components.frigidaire.config_flow import STEP_USER_DATA_SCHEMA

    with pytest.raises(vol.Invalid):
        STEP_USER_DATA_SCHEMA({"username": "a@b.c"})
    assert STEP_USER_DATA_SCHEMA({"username": "a@b.c", "password": "x"}) == {"username": "a@b.c", "password": "x"}
