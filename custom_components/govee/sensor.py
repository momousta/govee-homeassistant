"""Sensor platform for Govee integration.

Provides sensor entities for:
- Rate limit remaining (diagnostic)
- MQTT connection status (diagnostic)
- Leak sensor battery level (from BFF API polling)
- Leak sensor last wet event timestamp (from BFF API polling)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo  # type: ignore[attr-defined]
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
    """Set up Govee sensors from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        GoveeRateLimitSensor(coordinator, entry.entry_id),
    ]

    # Add MQTT status sensor if MQTT is configured
    if coordinator.mqtt_client is not None:
        entities.append(GoveeMqttStatusSensor(coordinator, entry.entry_id))

    # Add leak sensor entities
    for sensor in coordinator.leak_sensors.values():
        entities.append(GoveeLeakBatterySensor(coordinator, sensor))
        entities.append(GoveeLeakLastWetSensor(coordinator, sensor))
        entities.append(GoveeLeakAlertStatusSensor(coordinator, sensor))

    async_add_entities(entities)
    _LOGGER.debug("Set up %d Govee sensor entities", len(entities))


class GoveeRateLimitSensor(CoordinatorEntity["GoveeCoordinator"], SensorEntity):
    """Sensor showing API rate limit remaining.

    Helps users monitor their API usage and avoid hitting limits.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "rate_limit_remaining"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "requests"
    _attr_icon = "mdi:speedometer"

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the rate limit sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{entry_id}_rate_limit"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the integration hub."""
        return DeviceInfo(
            identifiers={(DOMAIN, "hub")},
            name="Govee Integration",
            manufacturer="Govee",
            model="Cloud API",
        )

    @property
    def native_value(self) -> int:
        """Return the current rate limit remaining."""
        return self.coordinator.api_rate_limit_remaining

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Return additional rate limit info."""
        return {
            "total_limit": self.coordinator.api_rate_limit_total,
            "reset_time": self.coordinator.api_rate_limit_reset,
        }


class GoveeMqttStatusSensor(CoordinatorEntity["GoveeCoordinator"], SensorEntity):
    """Sensor showing MQTT connection status.

    Indicates whether real-time push updates are working.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "mqtt_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["connected", "disconnected", "unavailable"]
    _attr_icon = "mdi:cloud-sync"

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the MQTT status sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{entry_id}_mqtt_status"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the integration hub."""
        return DeviceInfo(
            identifiers={(DOMAIN, "hub")},
            name="Govee Integration",
            manufacturer="Govee",
            model="Cloud API",
        )

    @property
    def native_value(self) -> str:
        """Return the current MQTT status."""
        mqtt_client = self.coordinator.mqtt_client
        if mqtt_client is None:
            return "unavailable"
        return "connected" if mqtt_client.connected else "disconnected"


class GoveeLeakBatterySensor(CoordinatorEntity["GoveeCoordinator"], SensorEntity):
    """Sensor showing leak sensor battery level (from BFF API polling)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GoveeCoordinator, sensor: GoveeLeakSensor) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_battery"
        self._attr_name = "Battery"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.leak_states.get(self._sensor.device_id)
        return state.battery if state else None


class GoveeLeakLastWetSensor(CoordinatorEntity["GoveeCoordinator"], SensorEntity):
    """Sensor showing when the last leak was detected (from BFF API polling)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: GoveeCoordinator, sensor: GoveeLeakSensor) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_last_wet"
        self._attr_name = "Last leak detected"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.leak_states.get(self._sensor.device_id)
        if state and state.last_wet_time:
            return datetime.fromtimestamp(state.last_wet_time / 1000, tz=timezone.utc)
        return None


class GoveeLeakAlertStatusSensor(CoordinatorEntity["GoveeCoordinator"], SensorEntity):
    """Sensor showing leak alert acknowledgment status (from BFF API polling)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Pending", "Acknowledged"]
    _attr_icon = "mdi:bell-alert"

    def __init__(self, coordinator: GoveeCoordinator, sensor: GoveeLeakSensor) -> None:
        super().__init__(coordinator)
        self._sensor = sensor
        self._attr_unique_id = f"{sensor.device_id}_alert_status"
        self._attr_name = "Alert status"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(**leak_sensor_device_info(self._sensor, DOMAIN))

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.leak_states.get(self._sensor.device_id)
        if state is None:
            return None
        return "Acknowledged" if state.read else "Pending"
