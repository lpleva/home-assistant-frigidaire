"""A command Electrolux refuses reaches the user as a sentence, not a traceback."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from payloads import DEHUMIDIFIER

from custom_components.frigidaire.vendor import frigidaire


def dehumidifier_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("humidifier", "frigidaire", "DH-1")
    assert entity_id is not None
    return entity_id


async def test_refused_command_is_a_home_assistant_error(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])
    stub.command_error = frigidaire.FrigidaireException(
        "Request failed with status 400 (error=ocp_020300, 74 bytes)", status_code=400, error_code="ocp_020300"
    )

    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            "humidifier", "set_humidity", {"entity_id": dehumidifier_id(hass), "humidity": 50}, blocking=True
        )

    assert excinfo.value.translation_key == "command_refused"
    assert excinfo.value.translation_placeholders["device"]
    assert "ocp_020300" in excinfo.value.translation_placeholders["error"]
    assert stub.commands == []


async def test_accepted_command_still_goes_through(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await hass.services.async_call(
        "humidifier", "set_humidity", {"entity_id": dehumidifier_id(hass), "humidity": 50}, blocking=True
    )

    assert ("targetHumidity", 50) in stub.commands
