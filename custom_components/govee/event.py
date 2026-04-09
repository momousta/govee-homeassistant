"""Event platform for Govee integration.

Provides event entities for button presses on Govee leak sensors (H5058).
Button press events are received via MQTT multiSync messages (0xEE 0x32).
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoveeCoordinator
from .models.device import GoveeLeakSensor, leak_sensor_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee event entities from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    entities: list[EventEntity] = []

    for sensor in coordinator.leak_sensors.values():
        entities.append(GoveeLeakButtonEvent(coordinator, sensor))

    if entities:
        async_add_entities(entities)
        _LOGGER.debug("Set up %d Govee leak button event entities", len(entities))


class GoveeLeakButtonEvent(CoordinatorEntity["GoveeCoordinator"], EventEntity):
    """Event entity for button presses on a Govee leak sensor."""

    _attr_has_entity_name = True
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["press"]
    _attr_translation_key = "button"

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        sensor: GoveeLeakSensor,
    ) -> None:
        """Initialize the button event entity."""
        super().__init__(coordinator)

        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_button"
        self._attr_name = "Button"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for device registry."""
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        last_press = self.coordinator._last_button_press
        if last_press and last_press.get("device_id") == self._sensor.device_id:
            # Consume the event
            self.coordinator._last_button_press = None
            self._trigger_event("press")
            self.async_write_ha_state()
        else:
            super()._handle_coordinator_update()
