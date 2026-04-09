"""Binary sensor platform for Govee integration.

Exposes per-device connectivity status for each transport (Cloud REST
API, AWS IoT MQTT, direct BLE) as CONNECTIVITY diagnostic entities.

Entities are opt-in via the ``expose_transport_entities`` option to avoid
creating 3Ã—N diagnostic entities by default.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_EXPOSE_TRANSPORT_ENTITIES,
    DEFAULT_EXPOSE_TRANSPORT_ENTITIES,
)
from .coordinator import GoveeCoordinator
from .models.device import GoveeLeakSensor, leak_sensor_device_info
from .entity import GoveeEntity
from .models import TransportKind
from homeassistant.helpers.update_coordinator import CoordinatorEntity

_LOGGER = logging.getLogger(__name__)


_TRANSPORT_SPECS: tuple[tuple[TransportKind, str, str], ...] = (
    ("cloud_api", "cloud_api_connectivity", "mdi:cloud"),
    ("mqtt", "mqtt_connectivity", "mdi:cloud-sync"),
    ("ble", "ble_connectivity", "mdi:bluetooth"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee binary sensors from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    if not entry.options.get(
        CONF_EXPOSE_TRANSPORT_ENTITIES, DEFAULT_EXPOSE_TRANSPORT_ENTITIES
    ):
        _LOGGER.debug(
            "Transport connectivity entities disabled via options; skipping"
        )
        return

    entities: list[BinarySensorEntity] = []
    for device in coordinator.devices.values():
        if device.is_group:
            continue
        for kind, translation_key, icon in _TRANSPORT_SPECS:
            entities.append(
                GoveeTransportConnectivity(
                    coordinator=coordinator,
                    device=device,
                    transport=kind,
                    translation_key=translation_key,
                    icon=icon,
                )
            )

    if entities:

    # Add leak sensor entities
    for sensor in coordinator.leak_sensors.values():
        entities.append(GoveeLeakBinarySensor(coordinator, sensor))
        entities.append(GoveeLeakOnlineSensor(coordinator, sensor))
        entities.append(GoveeLeakGatewayOnlineSensor(coordinator, sensor))

        async_add_entities(entities)
        _LOGGER.debug("Set up %d transport connectivity entities", len(entities))


class GoveeTransportConnectivity(GoveeEntity, BinarySensorEntity):
    """Per-device connectivity status for a single transport."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        device: Any,
        transport: TransportKind,
        translation_key: str,
        icon: str,
    ) -> None:
        """Initialize the connectivity binary sensor."""
        super().__init__(coordinator, device)
        self._transport = transport
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_unique_id = f"{device.device_id}_{transport}_connectivity"

    @property
    def is_on(self) -> bool | None:
        """Return True when the transport is currently usable for this device."""
        health = self.coordinator.get_transport_health(
            self._device_id, self._transport
        )
        if health is None:
            return None
        return health.is_available

    @property
    def available(self) -> bool:
        """Connectivity sensors are available whenever the coordinator is.

        They report their own state (on/off) rather than inheriting the
        main device's online flag â€” otherwise an offline device would
        hide the very diagnostic needed to understand why.
        """
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return timestamps and failure reason for this transport."""
        health = self.coordinator.get_transport_health(
            self._device_id, self._transport
        )
        if health is None:
            return {}
        attrs: dict[str, Any] = {}
        if health.last_success_ts is not None:
            attrs["last_success"] = health.last_success_ts.isoformat()
        if health.last_failure_ts is not None:
            attrs["last_failure"] = health.last_failure_ts.isoformat()
        if health.last_failure_reason is not None:
            attrs["last_failure_reason"] = health.last_failure_reason
        return attrs

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
