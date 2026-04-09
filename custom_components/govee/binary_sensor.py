"""Binary sensor platform for Govee integration.

Provides binary sensor entities for Govee leak sensors (H5058):
- Moisture detection (real-time via MQTT multiSync)
- Sensor connectivity (BFF API polling)
- Gateway connectivity (BFF API polling)
- Alert unread status (BFF API polling)
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
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
    """Set up Govee binary sensors from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    entities: list[BinarySensorEntity] = []

    for sensor in coordinator.leak_sensors.values():
        entities.append(GoveeLeakBinarySensor(coordinator, sensor))
        entities.append(GoveeLeakOnlineSensor(coordinator, sensor))
        entities.append(GoveeLeakGatewayOnlineSensor(coordinator, sensor))

    if entities:
        async_add_entities(entities)
        _LOGGER.debug("Set up %d Govee leak binary sensor entities", len(entities))


class GoveeLeakBinarySensor(CoordinatorEntity["GoveeCoordinator"], BinarySensorEntity):
    """Binary sensor for Govee leak detection (MQTT real-time)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, coordinator: GoveeCoordinator, sensor: GoveeLeakSensor) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_leak"
        self._attr_name = None  # Use device name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.leak_states.get(self._sensor.device_id)
        return state.is_wet if state else None

    @property
    def available(self) -> bool:
        return (
            super().available and self._sensor.device_id in self.coordinator.leak_states
        )


class GoveeLeakOnlineSensor(CoordinatorEntity["GoveeCoordinator"], BinarySensorEntity):
    """Binary sensor for leak sensor connectivity (BFF polling)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GoveeCoordinator, sensor: GoveeLeakSensor) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_online"
        self._attr_name = "Online"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.leak_states.get(self._sensor.device_id)
        return state.online if state else None


class GoveeLeakGatewayOnlineSensor(
    CoordinatorEntity["GoveeCoordinator"], BinarySensorEntity
):
    """Binary sensor for gateway hub connectivity (BFF polling)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GoveeCoordinator, sensor: GoveeLeakSensor) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_gateway_online"
        self._attr_name = "Gateway online"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.leak_states.get(self._sensor.device_id)
        return state.gateway_online if state else None
