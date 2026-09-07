"""Humidifier entity behaviour for dehumidifiers."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from payloads import DEHUMIDIFIER, with_reported
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from vendor import frigidaire


def humidifier_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("humidifier", "frigidaire", "DH-1")
    assert entity_id is not None
    return entity_id


def sent(stub, setting: frigidaire.Setting) -> list:
    """Every value the integration sent for one setting, in order."""
    return [value for name, value in stub.commands if name == setting.value]


async def call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call("humidifier", service, {"entity_id": humidifier_id(hass), **data}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_running_dehumidifier_state_and_attributes(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([DEHUMIDIFIER])

    state = hass.states.get(humidifier_id(hass))
    assert state.state == "on"
    assert state.attributes["mode"] == "normal"
    assert state.attributes["humidity"] == 45
    assert state.attributes["current_humidity"] == 55
    assert state.attributes["fan_mode"] == "low"
    assert state.attributes["check_filter"] is False
    assert state.attributes["bin_full"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"alerts": [{"code": "BUCKET_FULL"}]},
        {"alerts": ["BUCKET_FULL"]},
        {"waterBucketLevel": 1},
        {"waterTankFull": "yes"},
    ],
    ids=["alert-dict", "alert-string", "water-bucket-level", "water-tank-full"],
)
async def test_bin_full_is_detected_from_every_reported_signal(hass: HomeAssistant, setup_entry, changes: dict) -> None:
    await setup_entry([with_reported(DEHUMIDIFIER, **changes)])

    assert hass.states.get(humidifier_id(hass)).attributes["bin_full"] is True


async def test_bucket_full_keeps_the_appliance_available(hass: HomeAssistant, setup_entry) -> None:
    """A full bucket is a condition to report, not a reason to hide the entity."""
    await setup_entry([with_reported(DEHUMIDIFIER, alerts=[{"code": "BUCKET_FULL"}])])

    state = hass.states.get(humidifier_id(hass))
    assert state.state == "on"
    assert state.attributes["bin_full"] is True
    assert state.attributes["active_alerts"] == ["BUCKET_FULL"]


async def test_turn_on_and_off_send_power_commands(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await call(hass, "turn_off")
    assert sent(stub, frigidaire.Setting.EXECUTE_COMMAND) == [frigidaire.Power.OFF]

    stub.commands.clear()
    await call(hass, "turn_on")
    assert sent(stub, frigidaire.Setting.EXECUTE_COMMAND) == [frigidaire.Power.ON]


async def test_set_humidity_rounds_to_5_percent_steps(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await call(hass, "set_humidity", humidity=52)

    assert sent(stub, frigidaire.Setting.TARGET_HUMIDITY) == [50]


async def test_set_humidity_keeps_a_mode_that_honours_the_setpoint(hass: HomeAssistant, setup_entry) -> None:
    """The unit is already in Dry: there is nothing to switch, so no mode command."""
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await call(hass, "set_humidity", humidity=50)

    assert sent(stub, frigidaire.Setting.MODE) == []
    assert sent(stub, frigidaire.Setting.TARGET_HUMIDITY) == [50]


async def test_set_humidity_does_not_drop_the_unit_out_of_continuous(hass: HomeAssistant, setup_entry) -> None:
    """Continuous ignores the setpoint, so this one does need switching to Dry."""
    _entry, stub = await setup_entry([with_reported(DEHUMIDIFIER, mode="CONTINUOUS")])

    await call(hass, "set_humidity", humidity=50)

    assert sent(stub, frigidaire.Setting.MODE) == [frigidaire.Mode.DRY]
    # And only once: no power command, no second mode command.
    assert sent(stub, frigidaire.Setting.EXECUTE_COMMAND) == []


async def test_set_humidity_sends_no_power_command_while_running(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await call(hass, "set_humidity", humidity=45)

    assert sent(stub, frigidaire.Setting.EXECUTE_COMMAND) == []


async def test_set_mode_powers_on_a_unit_that_is_off(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([with_reported(DEHUMIDIFIER, applianceState="OFF", mode="DRY")])

    await call(hass, "set_mode", mode="boost")

    assert sent(stub, frigidaire.Setting.EXECUTE_COMMAND) == [frigidaire.Power.ON]
    assert sent(stub, frigidaire.Setting.MODE) == [frigidaire.Mode.CONTINUOUS]


async def test_set_mode_does_not_cancel_a_pending_start_timer(hass: HomeAssistant, setup_entry) -> None:
    """DELAYED_START means the unit is scheduled to start; powering it on now cancels that."""
    _entry, stub = await setup_entry([with_reported(DEHUMIDIFIER, applianceState="DELAYED_START")])

    await call(hass, "set_mode", mode="boost")

    assert sent(stub, frigidaire.Setting.EXECUTE_COMMAND) == []
    assert sent(stub, frigidaire.Setting.MODE) == [frigidaire.Mode.CONTINUOUS]


async def test_a_unit_waiting_on_a_start_timer_is_not_on(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([with_reported(DEHUMIDIFIER, applianceState="DELAYED_START")])

    assert hass.states.get(humidifier_id(hass)).state == "off"


async def test_smart_mode_round_trips(hass: HomeAssistant, setup_entry) -> None:
    """Selecting the mode the UI is already showing must not change the appliance."""
    _entry, stub = await setup_entry([with_reported(DEHUMIDIFIER, mode="SMART")])

    state = hass.states.get(humidifier_id(hass))
    assert state.attributes["mode"] == "smart"
    assert "smart" in state.attributes["available_modes"]

    await call(hass, "set_mode", mode="smart")
    assert sent(stub, frigidaire.Setting.MODE) == [frigidaire.Mode.SMART]


async def test_fan_only_mode_is_mapped_rather_than_warned_about(
    hass: HomeAssistant, setup_entry, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_entry([with_reported(DEHUMIDIFIER, mode="FANONLY")])

    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "fan"
    assert "Unsupported dehumidifier mode" not in caplog.text


async def test_an_unmapped_mode_warns_once_not_on_every_poll(
    hass: HomeAssistant, setup_entry, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_entry([with_reported(DEHUMIDIFIER, mode="SOMETHING_NEW")])

    state = hass.states.get(humidifier_id(hass))
    # Reading the state again is what used to add another log line each time.
    hass.states.get(humidifier_id(hass))
    assert state.attributes["mode"] is None
    assert caplog.text.count("Unsupported dehumidifier mode") == 1


async def test_set_fan_mode_sends_the_fan_speed(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await hass.services.async_call(
        "frigidaire", "set_fan_mode", {"entity_id": humidifier_id(hass), "fan_mode": "high"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sent(stub, frigidaire.Setting.FAN_SPEED) == [frigidaire.FanSpeed.HIGH]


async def test_an_unknown_fan_mode_sends_nothing(hass: HomeAssistant, setup_entry) -> None:
    _entry, stub = await setup_entry([DEHUMIDIFIER])

    await hass.services.async_call(
        "frigidaire", "set_fan_mode", {"entity_id": humidifier_id(hass), "fan_mode": "turbo"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert stub.commands == []


async def test_smart_stays_offered_after_the_unit_leaves_it(hass: HomeAssistant, setup_entry) -> None:
    """Otherwise selecting anything else removes Smart from the list and there is no way back."""
    _entry, stub = await setup_entry([with_reported(DEHUMIDIFIER, mode="SMART")])
    assert "smart" in hass.states.get(humidifier_id(hass)).attributes["available_modes"]

    stub.records["DH-1"]["properties"]["reported"]["mode"] = "DRY"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(humidifier_id(hass))
    assert state.attributes["mode"] == "normal"
    assert "smart" in state.attributes["available_modes"]


async def test_a_model_that_never_reports_smart_is_not_offered_it(hass: HomeAssistant, setup_entry) -> None:
    await setup_entry([DEHUMIDIFIER])

    assert hass.states.get(humidifier_id(hass)).attributes["available_modes"] == [
        "normal",
        "boost",
        "auto",
        "sleep",
    ]
