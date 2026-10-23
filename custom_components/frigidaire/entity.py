"""Base entity for the frigidaire integration."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import FrigidaireApplianceCoordinator


class FrigidaireEntity(CoordinatorEntity[FrigidaireApplianceCoordinator]):
    """An entity whose state comes from the appliance's reported properties.

    Every platform's entities derive from this except the connectivity binary sensor,
    which must keep reporting "off" while the appliance is unreachable. A disconnected
    appliance (unplugged, or off the Wi-Fi) keeps returning its last-known reported
    properties from the cloud, so without this check an unplugged dehumidifier read "on"
    with a frozen humidity and temperature for as long as it stayed unplugged.
    """

    @property
    def available(self) -> bool:
        # None means the model reports no connection state at all; those appliances keep
        # the coordinator's own availability (the poll succeeded) rather than going
        # permanently unavailable. Subclasses that add a capability check call this
        # through super().available, so the order is: poll ok, appliance reachable,
        # then whatever the entity itself needs.
        return super().available and self.coordinator.is_connected is not False
