"""Tests for Govee H5058 leak sensor support.

Tests cover:
- GoveeLeakSensor and GoveeLeakSensorState dataclasses
- leak_sensor_device_info helper
- BFF API: fetch_bff_leak_sensors parsing and error handling
- MQTT multiSync: leak event and button press packet decoding
- Coordinator: leak sensor discovery, state updates, BFF polling fallback
- Binary sensor: moisture, online, gateway online entities
- Sensor: battery, last leak detected, alert status entities
- Event: button press event entity
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.govee.api.mqtt import GoveeAwsIotClient
from custom_components.govee.models.device import (
    GoveeLeakSensor,
    GoveeLeakSensorState,
    leak_sensor_device_info,
)

# ==============================================================================
# Test data factories
# ==============================================================================

SAMPLE_HUB_DEVICE_ID = "09:C2:60:74:F4:64:AB:FA"
SAMPLE_SENSOR_DEVICE_ID = "01:32:7A:C4:06:02:1C:42"
SAMPLE_SENSOR_NAME = "Master sink (14)"
SAMPLE_SENSOR_SNO = 8


def create_leak_sensor(**overrides: Any) -> GoveeLeakSensor:
    """Factory for a GoveeLeakSensor."""
    defaults = {
        "device_id": SAMPLE_SENSOR_DEVICE_ID,
        "name": SAMPLE_SENSOR_NAME,
        "sku": "H5058",
        "hub_device_id": SAMPLE_HUB_DEVICE_ID,
        "sno": SAMPLE_SENSOR_SNO,
        "hw_version": "4.1",
        "sw_version": "1.12",
    }
    defaults.update(overrides)
    return GoveeLeakSensor(**defaults)


def create_bff_device(
    device_id: str = SAMPLE_SENSOR_DEVICE_ID,
    name: str = SAMPLE_SENSOR_NAME,
    sku: str = "H5058",
    sno: int = SAMPLE_SENSOR_SNO,
    battery: int = 90,
    online: bool = True,
    gwonline: bool = True,
    last_time: int = 1775599544000,
    read: bool = True,
) -> dict[str, Any]:
    """Factory for a BFF API device entry."""
    return {
        "device": device_id,
        "deviceName": name,
        "sku": sku,
        "deviceExt": {
            "deviceSettings": {
                "sno": sno,
                "battery": battery,
                "versionHard": "4.1",
                "versionSoft": "1.12",
                "gatewayInfo": {
                    "device": SAMPLE_HUB_DEVICE_ID,
                },
            },
            "lastDeviceData": {
                "online": online,
                "gwonline": gwonline,
                "lastTime": last_time,
                "read": read,
            },
        },
    }


def create_bff_device_json_strings(**kwargs: Any) -> dict[str, Any]:
    """Factory for BFF device with JSON-string encoded ext fields."""
    device = create_bff_device(**kwargs)
    device["deviceExt"]["deviceSettings"] = json.dumps(
        device["deviceExt"]["deviceSettings"]
    )
    device["deviceExt"]["lastDeviceData"] = json.dumps(
        device["deviceExt"]["lastDeviceData"]
    )
    return device


# ==============================================================================
# Packet helpers
# ==============================================================================


def make_leak_packet(
    sno: int = 0,
    wet: bool = True,
) -> str:
    """Create base64-encoded leak event packet (0xEE 0x34)."""
    raw = bytearray(20)
    raw[0] = 0xEE
    raw[1] = 0x34
    raw[2] = sno
    raw[3] = 0x0C
    raw[4] = 0xDA
    raw[5] = 0x01 if wet else 0x00
    raw[6] = 0x03
    raw[7] = 0x02
    raw[8] = 0x69
    raw[9] = 0xD5
    raw[10] = 0x1E
    raw[12] = 0x1C
    raw[13] = 0x41
    # XOR checksum
    xor = 0
    for b in raw[:19]:
        xor ^= b
    raw[19] = xor
    return base64.b64encode(bytes(raw)).decode()


def make_button_packet(
    device_id: str = SAMPLE_SENSOR_DEVICE_ID,
    sno: int = SAMPLE_SENSOR_SNO,
) -> str:
    """Create base64-encoded button press packet (0xEE 0x32)."""
    raw = bytearray(20)
    raw[0] = 0xEE
    raw[1] = 0x32
    # MAC reversed in bytes 2-9
    mac_bytes = bytes(int(x, 16) for x in device_id.split(":"))
    for i, b in enumerate(reversed(mac_bytes)):
        raw[2 + i] = b
    raw[10] = sno
    raw[11] = 0x0C
    raw[12] = 0xDA
    raw[14] = 0x03
    raw[15] = 0x04
    raw[16] = 0x1C
    raw[17] = 0x41
    # XOR checksum
    xor = 0
    for b in raw[:19]:
        xor ^= b
    raw[19] = xor
    return base64.b64encode(bytes(raw)).decode()


# ==============================================================================
# Model tests
# ==============================================================================


class TestGoveeLeakSensor:
    """Tests for GoveeLeakSensor dataclass."""

    def test_create_with_defaults(self) -> None:
        sensor = GoveeLeakSensor(
            device_id="AA:BB",
            name="Test",
            sku="H5058",
            hub_device_id="CC:DD",
            sno=0,
        )
        assert sensor.hw_version == ""
        assert sensor.sw_version == ""

    def test_create_with_versions(self) -> None:
        sensor = create_leak_sensor()
        assert sensor.hw_version == "4.1"
        assert sensor.sw_version == "1.12"
        assert sensor.sno == SAMPLE_SENSOR_SNO

    def test_frozen(self) -> None:
        sensor = create_leak_sensor()
        with pytest.raises(AttributeError):
            sensor.name = "changed"  # type: ignore[misc]


class TestGoveeLeakSensorState:
    """Tests for GoveeLeakSensorState dataclass."""

    def test_defaults(self) -> None:
        state = GoveeLeakSensorState()
        assert state.is_wet is False
        assert state.battery is None
        assert state.online is True
        assert state.gateway_online is True
        assert state.last_wet_time is None
        assert state.read is True
        assert state.last_mqtt_wet_at == 0.0

    def test_mutable(self) -> None:
        state = GoveeLeakSensorState()
        state.is_wet = True
        state.battery = 85
        state.last_mqtt_wet_at = time.time()
        assert state.is_wet is True
        assert state.battery == 85


class TestLeakSensorDeviceInfo:
    """Tests for the shared device_info helper."""

    def test_basic_info(self) -> None:
        sensor = create_leak_sensor()
        info = leak_sensor_device_info(sensor, "govee")
        assert ("govee", SAMPLE_SENSOR_DEVICE_ID) in info["identifiers"]
        assert info["name"] == SAMPLE_SENSOR_NAME
        assert info["manufacturer"] == "Govee"
        assert info["model"] == "H5058"
        assert info["via_device"] == ("govee", SAMPLE_HUB_DEVICE_ID)

    def test_includes_versions(self) -> None:
        sensor = create_leak_sensor(hw_version="4.1", sw_version="1.12")
        info = leak_sensor_device_info(sensor, "govee")
        assert info["hw_version"] == "4.1"
        assert info["sw_version"] == "1.12"

    def test_omits_empty_versions(self) -> None:
        sensor = create_leak_sensor(hw_version="", sw_version="")
        info = leak_sensor_device_info(sensor, "govee")
        assert "hw_version" not in info
        assert "sw_version" not in info


# ==============================================================================
# MQTT packet decoding tests
# ==============================================================================


class TestMultiSyncPacketDecoding:
    """Tests for MQTT multiSync BLE packet decode in mqtt.py."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.callback = MagicMock()
        self.client = GoveeAwsIotClient.__new__(GoveeAwsIotClient)
        self.client._on_state_update = self.callback

    def test_leak_wet_packet(self) -> None:
        """Decode a leak WET packet (0xEE 0x34, byte5=0x01)."""
        b64 = make_leak_packet(sno=0, wet=True)
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)

        self.callback.assert_called_once()
        event = self.callback.call_args[0][1]
        assert event["_leak_event"] is True
        assert event["hub_device_id"] == SAMPLE_HUB_DEVICE_ID
        assert event["sensor_slot"] == 0
        assert event["is_wet"] is True

    def test_leak_dry_packet(self) -> None:
        """Decode a leak DRY packet (0xEE 0x34, byte5=0x00)."""
        b64 = make_leak_packet(sno=5, wet=False)
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)

        event = self.callback.call_args[0][1]
        assert event["is_wet"] is False
        assert event["sensor_slot"] == 5

    def test_button_press_packet(self) -> None:
        """Decode a button press packet (0xEE 0x32) with reversed MAC."""
        b64 = make_button_packet(
            device_id=SAMPLE_SENSOR_DEVICE_ID,
            sno=SAMPLE_SENSOR_SNO,
        )
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)

        event = self.callback.call_args[0][1]
        assert event["_button_press"] is True
        assert event["device_id"] == SAMPLE_SENSOR_DEVICE_ID

    def test_short_packet_ignored(self) -> None:
        """Packets shorter than 6 bytes are silently ignored."""
        short_b64 = base64.b64encode(b"\xee\x34\x00").decode()
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [short_b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)
        self.callback.assert_not_called()

    def test_non_ee_header_ignored(self) -> None:
        """Packets with header != 0xEE are ignored."""
        raw = bytearray(20)
        raw[0] = 0xAA  # Wrong header
        raw[1] = 0x34
        b64 = base64.b64encode(bytes(raw)).decode()
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)
        self.callback.assert_not_called()

    def test_unknown_event_type_ignored(self) -> None:
        """Packets with unknown event type (not 0x34 or 0x32) are ignored."""
        raw = bytearray(20)
        raw[0] = 0xEE
        raw[1] = 0xFF  # Unknown event type
        b64 = base64.b64encode(bytes(raw)).decode()
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)
        self.callback.assert_not_called()

    def test_invalid_base64_ignored(self) -> None:
        """Invalid base64 in command[] is silently ignored."""
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": ["!!!not_base64!!!"]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)
        self.callback.assert_not_called()

    def test_multiple_commands(self) -> None:
        """Multiple packets in one multiSync message are all processed."""
        b64_wet = make_leak_packet(sno=0, wet=True)
        b64_dry = make_leak_packet(sno=1, wet=False)
        data = {
            "sku": "H5043",
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64_wet, b64_dry]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)
        assert self.callback.call_count == 2


# ==============================================================================
# BFF API parsing tests
# ==============================================================================


class TestBffLeakSensorParsing:
    """Tests for fetch_bff_leak_sensors response parsing."""

    @pytest.mark.asyncio
    async def test_parse_standard_response(self) -> None:
        """Parse BFF response with dict-type deviceExt fields."""
        from custom_components.govee.api.auth import GoveeAuthClient

        device = create_bff_device()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"data": {"devices": [device]}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)

        client = GoveeAuthClient(session=mock_session)
        result = await client.fetch_bff_leak_sensors("test_token")

        assert len(result) == 1
        assert result[0]["device_id"] == SAMPLE_SENSOR_DEVICE_ID
        assert result[0]["name"] == SAMPLE_SENSOR_NAME
        assert result[0]["sno"] == SAMPLE_SENSOR_SNO
        assert result[0]["battery"] == 90
        assert result[0]["online"] is True
        assert result[0]["gateway_online"] is True
        assert result[0]["read"] is True
        assert result[0]["hub_device_id"] == SAMPLE_HUB_DEVICE_ID

    @pytest.mark.asyncio
    async def test_parse_json_string_fields(self) -> None:
        """Parse BFF response where deviceExt fields are JSON strings."""
        from custom_components.govee.api.auth import GoveeAuthClient

        device = create_bff_device_json_strings()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"data": {"devices": [device]}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)

        client = GoveeAuthClient(session=mock_session)
        result = await client.fetch_bff_leak_sensors("test_token")

        assert len(result) == 1
        assert result[0]["sno"] == SAMPLE_SENSOR_SNO

    @pytest.mark.asyncio
    async def test_skip_non_leak_devices(self) -> None:
        """Non-leak SKUs are filtered out."""
        from custom_components.govee.api.auth import GoveeAuthClient

        light = {"device": "AA:BB", "deviceName": "Light", "sku": "H6072"}
        leak = create_bff_device()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"data": {"devices": [light, leak]}}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)

        client = GoveeAuthClient(session=mock_session)
        result = await client.fetch_bff_leak_sensors("test_token")

        assert len(result) == 1
        assert result[0]["sku"] == "H5058"

    @pytest.mark.asyncio
    async def test_skip_sensor_without_sno(self) -> None:
        """Sensors without sno field are skipped."""
        from custom_components.govee.api.auth import GoveeAuthClient

        device = create_bff_device()
        del device["deviceExt"]["deviceSettings"]["sno"]

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"data": {"devices": [device]}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)

        client = GoveeAuthClient(session=mock_session)
        result = await client.fetch_bff_leak_sensors("test_token")

        assert len(result) == 0


# ==============================================================================
# Coordinator leak state tests
# ==============================================================================


class TestCoordinatorLeakState:
    """Tests for coordinator leak event handling."""

    def test_handle_leak_event_wet(self) -> None:
        """MQTT leak event sets is_wet=True on correct sensor."""
        from custom_components.govee.coordinator import GoveeCoordinator

        coordinator = GoveeCoordinator.__new__(GoveeCoordinator)
        coordinator._leak_sensors = {SAMPLE_SENSOR_DEVICE_ID: create_leak_sensor()}
        coordinator._leak_states = {SAMPLE_SENSOR_DEVICE_ID: GoveeLeakSensorState()}
        coordinator._sno_to_sensor_id = {
            (SAMPLE_HUB_DEVICE_ID, SAMPLE_SENSOR_SNO): SAMPLE_SENSOR_DEVICE_ID
        }
        coordinator._states = {}
        mock_hass = MagicMock()

        def _consume_coro(coro):
            """Close the coroutine to prevent 'never awaited' warning."""
            coro.close()

        mock_hass.async_create_task = _consume_coro
        coordinator.hass = mock_hass
        coordinator.async_set_updated_data = MagicMock()

        state_data = {
            "_leak_event": True,
            "hub_device_id": SAMPLE_HUB_DEVICE_ID,
            "sensor_slot": SAMPLE_SENSOR_SNO,
            "is_wet": True,
        }
        coordinator._handle_leak_event(state_data)

        state = coordinator._leak_states[SAMPLE_SENSOR_DEVICE_ID]
        assert state.is_wet is True
        assert state.last_mqtt_wet_at > 0

    def test_handle_leak_event_dry(self) -> None:
        """MQTT leak event sets is_wet=False."""
        from custom_components.govee.coordinator import GoveeCoordinator

        coordinator = GoveeCoordinator.__new__(GoveeCoordinator)
        coordinator._leak_sensors = {SAMPLE_SENSOR_DEVICE_ID: create_leak_sensor()}
        state = GoveeLeakSensorState(is_wet=True, last_mqtt_wet_at=time.time())
        coordinator._leak_states = {SAMPLE_SENSOR_DEVICE_ID: state}
        coordinator._sno_to_sensor_id = {
            (SAMPLE_HUB_DEVICE_ID, SAMPLE_SENSOR_SNO): SAMPLE_SENSOR_DEVICE_ID
        }
        coordinator._states = {}
        mock_hass = MagicMock()

        def _consume_coro2(coro):
            """Close the coroutine to prevent 'never awaited' warning."""
            coro.close()

        mock_hass.async_create_task = _consume_coro2
        coordinator.hass = mock_hass
        coordinator.async_set_updated_data = MagicMock()

        state_data = {
            "_leak_event": True,
            "hub_device_id": SAMPLE_HUB_DEVICE_ID,
            "sensor_slot": SAMPLE_SENSOR_SNO,
            "is_wet": False,
        }
        coordinator._handle_leak_event(state_data)

        assert state.is_wet is False

    def test_handle_leak_event_unknown_sensor(self) -> None:
        """Leak event for unknown sensor slot is silently ignored."""
        from custom_components.govee.coordinator import GoveeCoordinator

        coordinator = GoveeCoordinator.__new__(GoveeCoordinator)
        coordinator._leak_sensors = {}
        coordinator._leak_states = {}
        coordinator._sno_to_sensor_id = {}
        coordinator._states = {}
        coordinator.async_set_updated_data = MagicMock()

        state_data = {
            "_leak_event": True,
            "hub_device_id": "unknown_hub",
            "sensor_slot": 99,
            "is_wet": True,
        }
        coordinator._handle_leak_event(state_data)
        coordinator.async_set_updated_data.assert_not_called()

    def test_handle_button_press(self) -> None:
        """MQTT button press sets _last_button_press."""
        from custom_components.govee.coordinator import GoveeCoordinator

        coordinator = GoveeCoordinator.__new__(GoveeCoordinator)
        coordinator._leak_sensors = {SAMPLE_SENSOR_DEVICE_ID: create_leak_sensor()}
        coordinator._leak_states = {SAMPLE_SENSOR_DEVICE_ID: GoveeLeakSensorState()}
        coordinator._last_button_press = None
        coordinator._states = {}
        mock_hass = MagicMock()

        def _consume_coro(coro):
            """Close the coroutine to prevent 'never awaited' warning."""
            coro.close()

        mock_hass.async_create_task = _consume_coro
        coordinator.hass = mock_hass
        coordinator.async_set_updated_data = MagicMock()

        state_data = {
            "_button_press": True,
            "device_id": SAMPLE_SENSOR_DEVICE_ID,
        }
        coordinator._handle_button_press(state_data)

        assert coordinator._last_button_press is not None
        assert coordinator._last_button_press["device_id"] == SAMPLE_SENSOR_DEVICE_ID

    def test_handle_button_press_unknown_sensor(self) -> None:
        """Button press for unknown sensor is silently ignored."""
        from custom_components.govee.coordinator import GoveeCoordinator

        coordinator = GoveeCoordinator.__new__(GoveeCoordinator)
        coordinator._leak_sensors = {}
        coordinator._last_button_press = None
        coordinator.async_set_updated_data = MagicMock()

        state_data = {
            "_button_press": True,
            "device_id": "unknown_sensor",
        }
        coordinator._handle_button_press(state_data)
        assert coordinator._last_button_press is None


# ==============================================================================
# BFF polling fallback tests
# ==============================================================================


class TestBffFallbackWetDetection:
    """Tests for the BFF polling fallback that forces wet when MQTT is down."""

    def test_fallback_triggers_when_mqtt_missed(self) -> None:
        """BFF detects recent leak + MQTT didn't report → force wet."""
        state = GoveeLeakSensorState()
        state.last_mqtt_wet_at = 0.0  # MQTT never reported wet
        state.is_wet = False

        now_s = time.time()
        now_ms = int(now_s * 1000)
        recent_wet_ms = now_ms - 60000  # 1 minute ago
        poll_interval = 300  # 5 minutes

        age_ms = now_ms - recent_wet_ms
        mqtt_wet_age = now_s - state.last_mqtt_wet_at

        should_force = age_ms < (poll_interval * 1000) and mqtt_wet_age > poll_interval
        assert should_force is True

    def test_fallback_skipped_when_mqtt_reported(self) -> None:
        """BFF detects leak but MQTT already reported → no force."""
        state = GoveeLeakSensorState()
        state.last_mqtt_wet_at = time.time() - 30  # MQTT reported 30s ago
        state.is_wet = True

        now_s = time.time()
        poll_interval = 300

        mqtt_wet_age = now_s - state.last_mqtt_wet_at

        should_force = mqtt_wet_age > poll_interval
        assert should_force is False

    def test_fallback_skipped_when_old_event(self) -> None:
        """BFF lastTime is older than poll window → no force."""
        state = GoveeLeakSensorState()
        state.last_mqtt_wet_at = 0.0

        now_ms = int(time.time() * 1000)
        old_wet_ms = now_ms - 600000  # 10 minutes ago
        poll_interval = 300  # 5 minute window

        age_ms = now_ms - old_wet_ms
        should_force = age_ms < (poll_interval * 1000)
        assert should_force is False


# ==============================================================================
# Real captured packet tests
# ==============================================================================


class TestRealCapturedPackets:
    """Tests using actual captured MQTT packets for validation."""

    def setup_method(self) -> None:
        self.callback = MagicMock()
        self.client = GoveeAwsIotClient.__new__(GoveeAwsIotClient)
        self.client._on_state_update = self.callback

    def test_real_wet_packet(self) -> None:
        """Decode real captured WET packet from Zain's bathroom (sno=0)."""
        # Captured: EE 34 00 0C DA 01 03 02 69 D5 1E 2C 1C 41 CE 00 00 00 00 11
        b64 = "7jQADNoBAwJp1R4sHEHOAAAAABE="
        data = {
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)

        event = self.callback.call_args[0][1]
        assert event["_leak_event"] is True
        assert event["sensor_slot"] == 0
        assert event["is_wet"] is True

    def test_real_dry_packet(self) -> None:
        """Decode real captured DRY packet from Zain's bathroom (sno=0)."""
        # Captured: EE 34 00 0C DA 00 03 02 69 D5 1E 6D 1C 41 D2 00 00 00 00 4D
        b64 = "7jQADNoAAwJp1R5tHEHSAAAAAE0="
        data = {
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)

        event = self.callback.call_args[0][1]
        assert event["_leak_event"] is True
        assert event["sensor_slot"] == 0
        assert event["is_wet"] is False

    def test_real_button_press_packet(self) -> None:
        """Decode real captured button press from Master sink (sno=8)."""
        # Captured: EE 32 42 1C 02 06 C4 7A 32 01 08 0C DA 00 03 04 1C 41 C8 47
        b64 = "7jJCHAIGxHoyAQgM2gADBBxByEc="
        data = {
            "device": SAMPLE_HUB_DEVICE_ID,
            "cmd": "multiSync",
            "op": {"command": [b64]},
        }
        self.client._handle_multisync(SAMPLE_HUB_DEVICE_ID, data)

        event = self.callback.call_args[0][1]
        assert event["_button_press"] is True
        assert event["device_id"] == "01:32:7A:C4:06:02:1C:42"


# ==============================================================================
# Entity property tests
# ==============================================================================


def _make_mock_coordinator(
    leak_states: dict[str, GoveeLeakSensorState] | None = None,
) -> MagicMock:
    """Create a mock coordinator with leak_states property."""
    coordinator = MagicMock()
    coordinator.leak_states = leak_states or {}
    coordinator._last_button_press = None
    return coordinator


class TestBinarySensorEntities:
    """Tests for binary sensor entity property logic."""

    def test_moisture_sensor_is_on_when_wet(self) -> None:
        from custom_components.govee.binary_sensor import GoveeLeakBinarySensor

        state = GoveeLeakSensorState(is_wet=True)
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakBinarySensor.__new__(GoveeLeakBinarySensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.is_on is True

    def test_moisture_sensor_is_off_when_dry(self) -> None:
        from custom_components.govee.binary_sensor import GoveeLeakBinarySensor

        state = GoveeLeakSensorState(is_wet=False)
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakBinarySensor.__new__(GoveeLeakBinarySensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.is_on is False

    def test_moisture_sensor_none_when_no_state(self) -> None:
        from custom_components.govee.binary_sensor import GoveeLeakBinarySensor

        coordinator = _make_mock_coordinator({})
        entity = GoveeLeakBinarySensor.__new__(GoveeLeakBinarySensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.is_on is None

    def test_online_sensor_reflects_state(self) -> None:
        from custom_components.govee.binary_sensor import GoveeLeakOnlineSensor

        state = GoveeLeakSensorState()
        state.online = False
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakOnlineSensor.__new__(GoveeLeakOnlineSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.is_on is False

    def test_gateway_online_sensor_reflects_state(self) -> None:
        from custom_components.govee.binary_sensor import GoveeLeakGatewayOnlineSensor

        state = GoveeLeakSensorState()
        state.gateway_online = True
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakGatewayOnlineSensor.__new__(GoveeLeakGatewayOnlineSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.is_on is True


class TestSensorEntities:
    """Tests for sensor entity property logic."""

    def test_battery_sensor_returns_value(self) -> None:
        from custom_components.govee.sensor import GoveeLeakBatterySensor

        state = GoveeLeakSensorState()
        state.battery = 72
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakBatterySensor.__new__(GoveeLeakBatterySensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.native_value == 72

    def test_battery_sensor_none_when_no_state(self) -> None:
        from custom_components.govee.sensor import GoveeLeakBatterySensor

        coordinator = _make_mock_coordinator({})
        entity = GoveeLeakBatterySensor.__new__(GoveeLeakBatterySensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.native_value is None

    def test_last_wet_sensor_returns_datetime(self) -> None:
        from datetime import datetime, timezone

        from custom_components.govee.sensor import GoveeLeakLastWetSensor

        state = GoveeLeakSensorState()
        state.last_wet_time = 1775599544000
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakLastWetSensor.__new__(GoveeLeakLastWetSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        result = entity.native_value
        assert isinstance(result, datetime)
        assert result.tzinfo == timezone.utc

    def test_last_wet_sensor_none_when_no_time(self) -> None:
        from custom_components.govee.sensor import GoveeLeakLastWetSensor

        state = GoveeLeakSensorState()
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakLastWetSensor.__new__(GoveeLeakLastWetSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.native_value is None

    def test_alert_status_acknowledged(self) -> None:
        from custom_components.govee.sensor import GoveeLeakAlertStatusSensor

        state = GoveeLeakSensorState()
        state.read = True
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakAlertStatusSensor.__new__(GoveeLeakAlertStatusSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.native_value == "Acknowledged"

    def test_alert_status_pending(self) -> None:
        from custom_components.govee.sensor import GoveeLeakAlertStatusSensor

        state = GoveeLeakSensorState()
        state.read = False
        coordinator = _make_mock_coordinator({SAMPLE_SENSOR_DEVICE_ID: state})
        entity = GoveeLeakAlertStatusSensor.__new__(GoveeLeakAlertStatusSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.native_value == "Pending"

    def test_alert_status_none_when_no_state(self) -> None:
        from custom_components.govee.sensor import GoveeLeakAlertStatusSensor

        coordinator = _make_mock_coordinator({})
        entity = GoveeLeakAlertStatusSensor.__new__(GoveeLeakAlertStatusSensor)
        entity._sensor = create_leak_sensor()
        entity.coordinator = coordinator
        assert entity.native_value is None
