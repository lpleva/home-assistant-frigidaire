"""Climate entity behaviour for air conditioners."""

import copy
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from payloads import LEGACY_AC, TELICA_AC, with_reported
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from vendor import frigidaire

OFF_AC = with_reported(LEGACY_AC, applianceState="OFF", mode="OFF")


def climate_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("climate", "frigidaire", "AC-LEGACY-1")
    assert entity_id is not None
    return entity_id


async def set_hvac_mode(hass: HomeAssistant, entity_id: str, hvac_mode: str) -> None:
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": hvac_mode}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_running_cool_unit_reports_cool_and_cooling(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([LEGACY_AC])

    state = hass.states.get(climate_id(hass))
    assert state.state == "cool"
    assert state.attributes["hvac_action"] == "cooling"
    assert state.attributes["current_temperature"] == 75
    assert state.attributes["temperature"] == 72
    assert state.attributes["fan_mode"] == "auto"
    assert state.attributes["swing_mode"] == "off"
    assert state.attributes["preset_mode"] == "none"


async def test_powered_off_unit_reports_off(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([OFF_AC])

    state = hass.states.get(climate_id(hass))
    assert state.state == "off"
    assert state.attributes["hvac_action"] == "off"


async def test_set_hvac_mode_on_running_unit_always_sends_power_then_mode(hass: HomeAssistant, setup_entry) -> None:
    """The cloud reports desired state, so RUNNING may be stale; power-on must not be skipped."""
    _entry, stub = await setup_entry([LEGACY_AC])

    await set_hvac_mode(hass, climate_id(hass), "fan_only")

    assert stub.commands == [("executeCommand", frigidaire.Power.ON), ("mode", frigidaire.Mode.FAN)]


async def test_set_hvac_mode_from_off_sends_power_mode_then_setpoint(hass: HomeAssistant, setup_entry) -> None:
    """The setpoint goes last: engaging the mode restores the appliance default and wipes an earlier value."""
    _entry, stub = await setup_entry([OFF_AC])

    await set_hvac_mode(hass, climate_id(hass), "cool")

    assert stub.commands == [
        ("executeCommand", frigidaire.Power.ON),
        ("mode", frigidaire.Mode.COOL),
        ("temperatureRepresentation", frigidaire.Unit.FAHRENHEIT),
        ("targetTemperatureF", 72),
    ]


async def test_turn_off_sends_mode_off(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([LEGACY_AC])

    await set_hvac_mode(hass, climate_id(hass), "off")

    assert stub.commands == [("mode", frigidaire.Mode.OFF)]


async def test_failed_poll_marks_climate_unavailable_and_logs(
    hass: HomeAssistant, setup_entry, caplog: pytest.LogCaptureFixture
) -> None:
    _entry, stub = await setup_entry([LEGACY_AC])
    stub.details_error = frigidaire.FrigidaireException("Request failed", status_code=429, error_code="cas_3403")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(climate_id(hass)).state == "unavailable"
    assert "Error communicating with Frigidaire (status=429, error=cas_3403)" in caplog.text


async def test_hvac_action_prefers_reported_mode_state(hass: HomeAssistant, setup_entry) -> None:
    """In eco the requested mode says nothing about what the unit is doing; modeState does."""
    await setup_entry([with_reported(TELICA_AC, mode="eco", modeState="cool")])

    entity_id = er.async_get(hass).async_get_entity_id("climate", "frigidaire", "AC-TELICA-1")
    state = hass.states.get(entity_id)
    assert state.state == "auto"
    assert state.attributes["hvac_action"] == "cooling"


async def test_hvac_action_fan_from_mode_state(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([with_reported(TELICA_AC, mode="eco", modeState="fanOnly")])

    entity_id = er.async_get(hass).async_get_entity_id("climate", "frigidaire", "AC-TELICA-1")
    assert hass.states.get(entity_id).attributes["hvac_action"] == "fan"


async def test_hvac_action_falls_back_to_mode_when_mode_state_absent(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([with_reported(LEGACY_AC, mode="FANONLY")])

    assert hass.states.get(climate_id(hass)).attributes["hvac_action"] == "fan"


async def test_mode_state_overrides_a_concrete_requested_mode(hass: HomeAssistant, setup_entry) -> None:
    """modeState wins even when the requested mode is itself an activity, not just in eco."""
    await setup_entry([with_reported(TELICA_AC, mode="cool", modeState="fanOnly")])

    entity_id = er.async_get(hass).async_get_entity_id("climate", "frigidaire", "AC-TELICA-1")
    assert hass.states.get(entity_id).attributes["hvac_action"] == "fan"


async def test_hvac_action_falls_back_to_drying_for_dry_mode(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([with_reported(LEGACY_AC, mode="DRY")])

    assert hass.states.get(climate_id(hass)).attributes["hvac_action"] == "drying"


async def test_a_missing_temperature_representation_does_not_break_the_entity(hass: HomeAssistant, setup_entry) -> None:
    """temperature_unit is read on every state write, so it must never raise."""
    record = copy.deepcopy(LEGACY_AC)
    del record["properties"]["reported"]["temperatureRepresentation"]
    await setup_entry([record])

    state = hass.states.get(climate_id(hass))
    assert state.state == "cool"
    # Falls back to Fahrenheit, which is what the reported values are in.
    assert state.attributes["temperature"] == 72
    assert state.attributes["current_temperature"] == 75


async def test_an_unmapped_hvac_mode_warns_once(
    hass: HomeAssistant, setup_entry, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_entry([with_reported(LEGACY_AC, mode="SOMETHING_NEW")])

    hass.states.get(climate_id(hass))
    hass.states.get(climate_id(hass))
    assert caplog.text.count("Unsupported HVAC mode") == 1


# Home Assistant 2026.11 renamed three temperature properties this entity provides to
# native_* and reads only those; 2026.10 reads only the old names. Both must answer.

CELSIUS_AC = with_reported(
    LEGACY_AC,
    temperatureRepresentation="CELSIUS",
    ambientTemperatureC=24,
    targetTemperatureC=22,
)


def climate_entity(hass: HomeAssistant):
    entity = hass.data["climate"].get_entity(climate_id(hass))
    assert entity is not None
    return entity


def provided_by_the_integration(entity, name: str) -> bool:
    """True when the integration's own class defines the property, not Home Assistant's base."""
    return name in vars(type(entity))


@pytest.mark.parametrize(
    ("record", "expected"),
    [(LEGACY_AC, "°F"), (CELSIUS_AC, "°C")],
    ids=["fahrenheit", "celsius"],
)
async def test_native_temperature_unit_and_its_old_name(hass: HomeAssistant, setup_entry, record, expected) -> None:
    await setup_entry([record])
    entity = climate_entity(hass)

    assert provided_by_the_integration(entity, "native_temperature_unit")
    assert provided_by_the_integration(entity, "temperature_unit")
    assert entity.native_temperature_unit == expected
    assert entity.temperature_unit == expected


async def test_native_temperature_unit_falls_back_to_fahrenheit(hass: HomeAssistant, setup_entry) -> None:
    record = copy.deepcopy(LEGACY_AC)
    del record["properties"]["reported"]["temperatureRepresentation"]
    await setup_entry([record])
    entity = climate_entity(hass)

    assert entity.native_temperature_unit == "°F"
    assert entity.temperature_unit == "°F"


@pytest.mark.parametrize(
    ("record", "expected"),
    [(LEGACY_AC, 75), (CELSIUS_AC, 24)],
    ids=["fahrenheit", "celsius"],
)
async def test_native_current_temperature_and_its_old_name(hass: HomeAssistant, setup_entry, record, expected) -> None:
    await setup_entry([record])
    entity = climate_entity(hass)

    assert provided_by_the_integration(entity, "native_current_temperature")
    assert provided_by_the_integration(entity, "current_temperature")
    assert entity.native_current_temperature == expected
    assert entity.current_temperature == expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [(LEGACY_AC, 72), (CELSIUS_AC, 22)],
    ids=["fahrenheit", "celsius"],
)
async def test_native_target_temperature_and_its_old_name(hass: HomeAssistant, setup_entry, record, expected) -> None:
    await setup_entry([record])
    entity = climate_entity(hass)

    assert provided_by_the_integration(entity, "native_target_temperature")
    assert provided_by_the_integration(entity, "target_temperature")
    assert entity.native_target_temperature == expected
    assert entity.target_temperature == expected


async def test_old_and_native_target_agree_right_after_a_setpoint_change(hass: HomeAssistant, setup_entry) -> None:
    """The optimistic setpoint shown before the next poll is the same under both names."""
    await setup_entry([LEGACY_AC])
    entity_id = climate_id(hass)

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity_id, "temperature": 68}, blocking=True
    )
    entity = climate_entity(hass)

    assert entity.native_target_temperature == 68
    assert entity.target_temperature == 68


@pytest.mark.parametrize(
    ("record", "hass_unit", "setpoint", "commands", "limits"),
    [
        (
            LEGACY_AC,
            "°F",
            68,
            [("temperatureRepresentation", frigidaire.Unit.FAHRENHEIT), ("targetTemperatureF", 68)],
            (60, 90),
        ),
        (
            CELSIUS_AC,
            "°C",
            20,
            [("temperatureRepresentation", frigidaire.Unit.CELSIUS), ("targetTemperatureC", 20)],
            (16, 32),
        ),
    ],
    ids=["fahrenheit", "celsius"],
)
async def test_setpoint_is_sent_in_the_appliances_own_unit(
    hass: HomeAssistant, setup_entry, record, hass_unit, setpoint, commands, limits
) -> None:
    """The unit sent to Electrolux and the setpoint limits follow the appliance, as before the rename."""
    hass.config.units = METRIC_SYSTEM if hass_unit == "°C" else US_CUSTOMARY_SYSTEM
    _entry, stub = await setup_entry([record])
    entity_id = climate_id(hass)

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity_id, "temperature": setpoint}, blocking=True
    )

    assert stub.commands == commands
    entity = climate_entity(hass)
    assert (entity.min_temp, entity.max_temp) == limits


async def test_only_the_three_temperature_names_this_entity_provides_are_aliased(
    hass: HomeAssistant, setup_entry
) -> None:
    """Every old name the class defines has its native twin in the same class, so 2026.11 logs no deprecation."""
    await setup_entry([LEGACY_AC])
    provided = vars(type(climate_entity(hass)))

    old_names = (
        "temperature_unit",
        "current_temperature",
        "target_temperature",
        "target_temperature_high",
        "target_temperature_low",
    )
    for old in old_names:
        assert (old in provided) == (f"native_{old}" in provided), old
    assert [old for old in old_names if old in provided] == [
        "temperature_unit",
        "current_temperature",
        "target_temperature",
    ]
