"""Every reported-property entity goes unavailable while the cloud reports the appliance disconnected.

A disconnected appliance (unplugged, or off the Wi-Fi) keeps returning its last-known
reported properties, so until this check the dehumidifier read "on" with a frozen humidity
and temperature for as long as it stayed unplugged. The connectivity sensor is the one
entity that stays available, reading "off".
"""

import copy
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from payloads import DEHUMIDIFIER, LEGACY_AC, with_reported
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from vendor import frigidaire

# A dehumidifier that reports everything an entity can be built from, with every opt-in on.
FULL_DEHUMIDIFIER = with_reported(DEHUMIDIFIER, ambientTemperatureF=71.6, airFilterLifeTime=3600)
DEHUMIDIFIER_OPTIONS = {
    "DH-1": {
        "bucket_status": True,
        "check_filter": True,
        "filter_runtime": True,
        "display_light": True,
        "clean_air_mode": True,
        "ui_lock": True,
    }
}
DEHUMIDIFIER_ENTITIES = [
    ("humidifier", "DH-1"),
    ("sensor", "DH-1_temperature"),
    ("sensor", "DH-1_humidity"),
    ("sensor", "DH-1_filter_runtime"),
    ("binary_sensor", "DH-1_bucket_status"),
    ("binary_sensor", "DH-1_check_filter"),
    ("switch", "DH-1_display_light"),
    ("switch", "DH-1_clean_air_mode"),
    ("switch", "DH-1_ui_lock"),
]

AC_OPTIONS = {"AC-LEGACY-1": {"compressor": True, "cool_hysteresis": 0.0, "compressor_off_delay": 0, "ui_lock": True}}
AC_ENTITIES = [
    ("climate", "AC-LEGACY-1"),
    ("number", "AC-LEGACY-1_timer_on"),
    ("number", "AC-LEGACY-1_timer_off"),
    ("binary_sensor", "AC-LEGACY-1_compressor"),
    ("switch", "AC-LEGACY-1_ui_lock"),
]


def disconnected(record: dict) -> dict:
    updated = copy.deepcopy(record)
    updated["connectionState"] = "Disconnected"
    return updated


def state_of(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, "frigidaire", unique_id)
    assert entity_id is not None, f"{platform} {unique_id} was not created"
    return hass.states.get(entity_id).state


def states_of(hass: HomeAssistant, entities: list[tuple[str, str]]) -> dict[str, str]:
    return {unique_id: state_of(hass, platform, unique_id) for platform, unique_id in entities}


def unavailable(entities: list[tuple[str, str]]) -> dict[str, str]:
    return {unique_id: "unavailable" for _, unique_id in entities}


async def poll(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_disconnected_dehumidifier_entities_are_unavailable_while_connectivity_reads_off(
    hass: HomeAssistant, setup_entry
) -> None:
    await setup_entry([disconnected(FULL_DEHUMIDIFIER)], options=DEHUMIDIFIER_OPTIONS)

    assert states_of(hass, DEHUMIDIFIER_ENTITIES) == unavailable(DEHUMIDIFIER_ENTITIES)
    assert state_of(hass, "binary_sensor", "DH-1_connectivity") == "off"


async def test_disconnected_air_conditioner_entities_are_unavailable_while_connectivity_reads_off(
    hass: HomeAssistant, setup_entry
) -> None:
    await setup_entry([disconnected(LEGACY_AC)], options=AC_OPTIONS)

    assert states_of(hass, AC_ENTITIES) == unavailable(AC_ENTITIES)
    assert state_of(hass, "binary_sensor", "AC-LEGACY-1_connectivity") == "off"


async def test_unplugging_hides_the_frozen_values_and_plugging_back_in_restores_live_ones(
    hass: HomeAssistant, setup_entry
) -> None:
    """The 2026-10-21 case: the basement dehumidifier was unplugged at 12:26 AM.

    The cloud kept serving the last reported properties, so Home Assistant showed "on" at
    42 % and 71.6 °F all night. The reported properties do not change across the
    disconnect, only connectionState does, so the entities must react to that alone.
    """
    _entry, stub = await setup_entry([FULL_DEHUMIDIFIER], options=DEHUMIDIFIER_OPTIONS)
    assert state_of(hass, "humidifier", "DH-1") == "on"
    assert state_of(hass, "sensor", "DH-1_humidity") == "55.0"

    stub.records["DH-1"]["connectionState"] = "Disconnected"
    await poll(hass)

    assert states_of(hass, DEHUMIDIFIER_ENTITIES) == unavailable(DEHUMIDIFIER_ENTITIES)
    assert state_of(hass, "binary_sensor", "DH-1_connectivity") == "off"

    # Back on the network with a new reading: the next poll is enough, no restart.
    stub.records["DH-1"]["connectionState"] = "Connected"
    stub.records["DH-1"]["properties"]["reported"]["sensorHumidity"] = 42
    await poll(hass)

    assert state_of(hass, "binary_sensor", "DH-1_connectivity") == "on"
    assert state_of(hass, "humidifier", "DH-1") == "on"
    assert state_of(hass, "sensor", "DH-1_humidity") == "42.0"
    assert state_of(hass, "sensor", "DH-1_temperature") == "71.6"
    assert state_of(hass, "switch", "DH-1_display_light") == "on"
    humidifier = hass.states.get(er.async_get(hass).async_get_entity_id("humidifier", "frigidaire", "DH-1"))
    assert humidifier.attributes["current_humidity"] == 42


async def test_a_model_that_reports_no_connection_state_keeps_its_entities_available(
    hass: HomeAssistant, setup_entry
) -> None:
    """None is "unknown", not "disconnected": those models must not go permanently unavailable."""
    record = copy.deepcopy(FULL_DEHUMIDIFIER)
    del record["connectionState"]
    await setup_entry([record], options=DEHUMIDIFIER_OPTIONS)

    states = states_of(hass, DEHUMIDIFIER_ENTITIES)
    assert "unavailable" not in states.values()
    assert states["DH-1"] == "on"
    assert states["DH-1_humidity"] == "55.0"
    assert er.async_get(hass).async_get_entity_id("binary_sensor", "frigidaire", "DH-1_connectivity") is None


async def test_a_failed_poll_still_makes_everything_unavailable_including_connectivity(
    hass: HomeAssistant, setup_entry
) -> None:
    """The connection check is added to the poll check, not swapped in for it."""
    _entry, stub = await setup_entry([FULL_DEHUMIDIFIER], options=DEHUMIDIFIER_OPTIONS)
    stub.details_error = frigidaire.FrigidaireException("Request failed", status_code=503)
    await poll(hass)

    assert states_of(hass, DEHUMIDIFIER_ENTITIES) == unavailable(DEHUMIDIFIER_ENTITIES)
    assert state_of(hass, "binary_sensor", "DH-1_connectivity") == "unavailable"


async def test_no_command_is_sent_to_a_disconnected_appliance(hass: HomeAssistant, setup_entry) -> None:
    """Home Assistant skips unavailable entities in a service call, so the cloud is not asked."""
    _entry, stub = await setup_entry([disconnected(FULL_DEHUMIDIFIER)], options=DEHUMIDIFIER_OPTIONS)
    entity_id = er.async_get(hass).async_get_entity_id("humidifier", "frigidaire", "DH-1")

    await hass.services.async_call("humidifier", "turn_off", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert stub.commands == []
