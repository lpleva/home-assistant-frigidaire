"""ClimateEntity for frigidaire integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.humidifier import HumidifierDeviceClass, HumidifierEntity
from homeassistant.components.humidifier.const import (
    MODE_AUTO,
    MODE_BOOST,
    MODE_NORMAL,
    MODE_SLEEP,
    HumidifierEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FrigidaireApplianceCoordinator
from .helpers import normalize_enum_value, suggest_area
from .parsers import bucket_is_full, filter_needs_attention, normalize_alerts
from .vendor import frigidaire

_LOGGER = logging.getLogger(__name__)

FAN_LOW = "low"
FAN_MEDIUM = "medium"
FAN_HIGH = "high"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up frigidaire from a config entry."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "set_fan_mode",
        {vol.Required("fan_mode"): cv.string},
        "set_fan_mode",
    )

    coordinators: dict[str, FrigidaireApplianceCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    appliances: list[frigidaire.Appliance] = hass.data[DOMAIN][entry.entry_id]["appliances"]

    async_add_entities(
        FrigidaireDehumidifier(coordinators[appliance.appliance_id], suggest_area(hass, appliance.nickname))
        for appliance in appliances
        if appliance.destination == frigidaire.Destination.DEHUMIDIFIER
    )


# Smart and fan-only get their own Home Assistant modes rather than folding into
# "auto"/"normal": collapsing them made the mapping lossy, so selecting the displayed mode
# sent a different one back and silently switched the appliance out of Smart.
MODE_FAN = "fan"
MODE_SMART = "smart"

FRIGIDAIRE_TO_HA_MODE = {
    frigidaire.Mode.DRY: MODE_NORMAL,
    frigidaire.Mode.CONTINUOUS: MODE_BOOST,
    frigidaire.Mode.QUIET: MODE_SLEEP,
    frigidaire.Mode.AUTO: MODE_AUTO,
    frigidaire.Mode.SMART: MODE_SMART,
    frigidaire.Mode.FAN: MODE_FAN,
}

# Bijective by construction, so a mode always round-trips.
HA_TO_FRIGIDAIRE_MODE = {v: k for k, v in FRIGIDAIRE_TO_HA_MODE.items()}

# Every model has these four.
BASE_MODES = [MODE_NORMAL, MODE_BOOST, MODE_AUTO, MODE_SLEEP]

# Modes in which the appliance works towards the target humidity. CONTINUOUS runs
# regardless of the setpoint and FANONLY does not dehumidify at all, so those are the
# only two set_humidity has to switch away from.
SETPOINT_MODES = frozenset(
    {frigidaire.Mode.DRY, frigidaire.Mode.AUTO, frigidaire.Mode.SMART, frigidaire.Mode.QUIET}
)

FRIGIDAIRE_TO_HA_FAN_MODE = {
    frigidaire.FanSpeed.LOW: FAN_LOW,
    frigidaire.FanSpeed.MEDIUM: FAN_MEDIUM,
    frigidaire.FanSpeed.HIGH: FAN_HIGH,
}

HA_TO_FRIGIDAIRE_FAN_MODE = {v: k for k, v in FRIGIDAIRE_TO_HA_FAN_MODE.items()}


class FrigidaireDehumidifier(CoordinatorEntity[FrigidaireApplianceCoordinator], HumidifierEntity):
    """Representation of a Frigidaire dehumidifier."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FrigidaireApplianceCoordinator, suggested_area: str | None = None):
        """Build FrigidaireDehumidifier.

        coordinator: shared per-appliance coordinator that polls the frigidaire API
        """

        super().__init__(coordinator)
        self._client: frigidaire.Frigidaire = coordinator.client
        self._appliance: frigidaire.Appliance = coordinator.appliance

        # Entity Class Attributes
        self._attr_unique_id = self._appliance.appliance_id
        # The primary entity for the device carries the device's own name; setting both
        # to the nickname is exactly what has_entity_name replaced.
        self._attr_name = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._appliance.appliance_id)},
            name=self._appliance.nickname,
            manufacturer="Frigidaire",
            suggested_area=suggested_area,
        )
        self._attr_supported_features = HumidifierEntityFeature.MODES

        self._attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER

        # Fan speed is exposed through the frigidaire.set_fan_mode service and the
        # fan_mode state attribute rather than a feature flag, because the humidifier
        # entity has no standard fan-mode feature.

        # Warned-about modes, so an unmapped value does not log on every poll.
        self._warned_modes: set = set()

    @property
    def _details(self) -> dict:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        # Prefer applianceState when present; fall back to a reported mode, since
        # some models omit applianceState from their API response.
        if not super().available:
            return False
        appliance_state = self._details.get(frigidaire.Detail.APPLIANCE_STATE)
        mode = self._details.get(frigidaire.Detail.MODE)
        return appliance_state is not None or mode is not None

    @property
    def is_on(self):
        return (
            normalize_enum_value(self._details.get(frigidaire.Detail.APPLIANCE_STATE))
            == frigidaire.ApplianceState.RUNNING
        )

    @property
    def target_humidity(self):
        """Return the humidity we try to reach."""
        return self._details.get(frigidaire.Detail.TARGET_HUMIDITY)

    @property
    def available_modes(self) -> list[str]:
        """Modes offered in the UI.

        Smart and fan-only exist on some models and not others, so they are offered once
        the appliance reports being in one. Offering a mode the unit silently ignores is
        worse than not offering it.
        """
        modes = list(BASE_MODES)
        current = FRIGIDAIRE_TO_HA_MODE.get(normalize_enum_value(self._details.get(frigidaire.Detail.MODE)))
        if current is not None and current not in modes:
            modes.append(current)
        return modes

    @property
    def mode(self):
        """Return current operation i.e. dry, continuous."""
        frigidaire_mode = normalize_enum_value(self._details.get(frigidaire.Detail.MODE))

        if frigidaire_mode == frigidaire.Mode.OFF:
            return MODE_NORMAL

        if frigidaire_mode not in FRIGIDAIRE_TO_HA_MODE:
            # Once per distinct value: this is a state property, so Home Assistant
            # evaluates it on every poll and an unmapped mode would otherwise log
            # 2,880 identical warnings a day.
            if frigidaire_mode not in self._warned_modes:
                self._warned_modes.add(frigidaire_mode)
                _LOGGER.warning("Unsupported dehumidifier mode '%s' reported by device.", frigidaire_mode)
            return None

        return FRIGIDAIRE_TO_HA_MODE[frigidaire_mode]

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Add extra state attributes specific to Frigidaire dehumidifiers"""
        fan_speed = normalize_enum_value(self._details.get(frigidaire.Detail.FAN_SPEED))

        attrib = {
            "current_humidity": self._details.get(frigidaire.Detail.SENSOR_HUMIDITY),
            "check_filter": filter_needs_attention(self._details.get(frigidaire.Detail.FILTER_STATE)) or False,
            "fan_mode": FRIGIDAIRE_TO_HA_FAN_MODE.get(fan_speed),
        }

        # The following attributes only exist on some models of dehumidifier
        alerts = normalize_alerts(self._details.get(frigidaire.Detail.ALERTS))
        if alerts is not None:
            attrib["active_alerts"] = alerts

        # Shared with the Bucket Status binary sensor; None (unreported) stays
        # False here to preserve the attribute's historical always-bool shape.
        attrib["bin_full"] = (
            bucket_is_full(
                alerts,
                self._details.get(frigidaire.Detail.WATER_BUCKET_LEVEL),
                self._details.get(frigidaire.Detail.WATER_TANK_FULL),
            )
            or False
        )

        return attrib

    @property
    def min_humidity(self):
        """Return the minimum humidity."""
        return 35

    @property
    def max_humidity(self):
        """Return the maximum humidity."""
        return 85

    def turn_on(self, **kwargs: Any) -> None:
        self._client.execute_action(self._appliance, frigidaire.Action.set_power(frigidaire.Power.ON))
        self.schedule_update_ha_state(force_refresh=True)

    def turn_off(self, **kwargs: Any) -> None:
        self._client.execute_action(self._appliance, frigidaire.Action.set_power(frigidaire.Power.OFF))
        self.schedule_update_ha_state(force_refresh=True)

    def set_humidity(self, humidity: int) -> None:
        """Set new target humidity."""
        if humidity is None:
            return
        # The appliance only accepts 5% steps. Home Assistant has already range-checked
        # against min_humidity/max_humidity, so rounding stays inside 35-85.
        humidity = 5 * round(humidity / 5)

        current_mode = normalize_enum_value(self._details.get(frigidaire.Detail.MODE))
        if current_mode not in SETPOINT_MODES:
            # Switch to Dry only when the active mode would ignore the setpoint. Doing it
            # unconditionally dropped a unit out of Continuous every time the humidity
            # slider moved, which is not what anyone asked for.
            self._client.execute_action(self._appliance, frigidaire.Action.set_mode(frigidaire.Mode.DRY))

        self._client.execute_action(self._appliance, frigidaire.Action.set_humidity(humidity))
        # One refresh for the whole operation: each one is a full account fetch.
        self.schedule_update_ha_state(force_refresh=True)

    def set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        # Guard against unexpected fan modes
        if fan_mode not in HA_TO_FRIGIDAIRE_FAN_MODE:
            return

        action = frigidaire.Action.set_fan_speed(HA_TO_FRIGIDAIRE_FAN_MODE[fan_mode])
        self._client.execute_action(self._appliance, action)
        self.schedule_update_ha_state(force_refresh=True)

    def set_mode(self, mode: str) -> None:
        """Set new target operation mode."""

        # Guard against unexpected modes
        if mode not in HA_TO_FRIGIDAIRE_MODE:
            return

        state = normalize_enum_value(self._details.get(frigidaire.Detail.APPLIANCE_STATE))
        # Only OFF gets a power command. DELAYED_START is excluded on purpose: the unit is
        # already scheduled to start, and powering it on now would cancel that timer. The
        # power command is sent directly rather than through turn_on(), which would queue
        # a second full refresh of its own.
        if state == frigidaire.ApplianceState.OFF:
            self._client.execute_action(self._appliance, frigidaire.Action.set_power(frigidaire.Power.ON))

        self._client.execute_action(self._appliance, frigidaire.Action.set_mode(HA_TO_FRIGIDAIRE_MODE[mode]))
        self.schedule_update_ha_state(force_refresh=True)
