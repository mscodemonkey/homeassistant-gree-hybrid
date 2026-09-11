"""Tests for the repository-owned Gree protocol layer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from gree_hybrid_protocol.cloud import CloudDevice, parent_mac
from gree_hybrid_protocol.crypto import EcbCipher, GcmCipher
from gree_hybrid_protocol.device import GreeDevice, Mode, Props, TemperatureUnits
from gree_hybrid_protocol.models import DeviceInfo

COMMAND = {"mac": "580d0d35fcd5", "t": "cmd", "opt": ["Pow"], "p": [1]}


class MemoryDevice(GreeDevice):
    """Minimal concrete device used to exercise transport-neutral state."""

    async def bind(self) -> None: ...

    async def update_state(self) -> None: ...

    async def push_state_update(self) -> None: ...

    async def close(self) -> None: ...


class FakeMqttClient:
    """MQTT double that immediately returns an encrypted state response."""

    def __init__(self) -> None:
        self.handlers: set[Any] = set()
        self.commands: list[dict[str, Any]] = []
        self.subscriptions: list[str] = []

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.add(handler)

    def remove_message_handler(self, handler: Any) -> None:
        self.handlers.discard(handler)

    async def subscribe_to_device(self, mac: str) -> None:
        self.subscriptions.append(mac)

    async def unsubscribe_from_device(self, mac: str) -> None:
        self.subscriptions.remove(mac)

    async def publish_command(
        self,
        parent: str,
        child: str,
        command: dict[str, Any],
        cipher: EcbCipher,
    ) -> None:
        self.commands.append(command)
        encrypted, _ = cipher.encrypt({"t": "dat", "cols": ["Pow"], "dat": [0]})
        for handler in tuple(self.handlers):
            handler(f"response/{parent}/state", {"pack": encrypted, "tcid": child})


def device_info(mac: str = "580d0d35fcd5") -> DeviceInfo:
    return DeviceInfo("0.0.0.0", 0, mac, "Test air conditioner")


def test_ecb_cipher_has_stable_wire_format_and_round_trips() -> None:
    cipher = EcbCipher()
    encrypted, tag = cipher.encrypt(COMMAND)

    assert encrypted == (
        "dYKC0cIXD1sJRoqHr+jzi0REd6CQ//PalYIFONh8AnSjvGcN3ZjUlJKOUa6evB6t"
        "x8TNzdee8kXzWs4dDYwKyQ=="
    )
    assert tag is None
    assert cipher.decrypt(encrypted) == COMMAND


def test_gcm_cipher_authenticates_packets() -> None:
    cipher = GcmCipher()
    encrypted, tag = cipher.encrypt(COMMAND)

    assert cipher.decrypt(encrypted, tag) == COMMAND
    with pytest.raises(ValueError):
        cipher.decrypt(encrypted, "AAAAAAAAAAAAAAAAAAAAAA==")


def test_device_state_does_not_mutate_received_packet() -> None:
    device = MemoryDevice(device_info())
    state = {"hid": "moduleV3.10.bin", "Pow": 1, "SetTem": 23, "TemUn": 0}

    device.apply_state(state)

    assert state["hid"] == "moduleV3.10.bin"
    assert device.version == "3.10"
    assert device.power is True
    assert device.target_temperature == 23


def test_temperature_setter_marks_supported_half_degree_properties() -> None:
    device = MemoryDevice(device_info())
    device.apply_state(
        {
            Props.TEMP_UNIT.value: TemperatureUnits.C,
            Props.TEMP_HALF_ENABLED.value: 1,
        }
    )

    device.target_temperature = 22.5

    assert device.raw_properties[Props.TEMP_SET.value] == 22
    assert device.raw_properties[Props.TEMP_BIT.value] == 1
    assert device.raw_properties[Props.TEMP_DECI.value] == 225


def test_cloud_request_includes_device_mac_and_applies_response() -> None:
    async def exercise() -> None:
        mqtt = FakeMqttClient()
        device = CloudDevice(mqtt, device_info(), "0123456789abcdef")

        await device.bind()

        assert mqtt.subscriptions == ["580d0d35fcd5"]
        assert mqtt.commands[0]["mac"] == "580d0d35fcd5"
        assert mqtt.commands[0]["t"] == "status"
        assert device.power is False

    asyncio.run(exercise())


def test_cloud_commands_apply_mode_before_power() -> None:
    mqtt = FakeMqttClient()
    device = CloudDevice(mqtt, device_info(), "0123456789abcdef")
    device.apply_state({Props.POWER.value: 0, Props.MODE.value: Mode.Auto})
    device.power = True
    device.mode = Mode.Cool

    commands = device._commands()

    assert commands[0]["opt"] == [Props.MODE.value]
    assert commands[-1]["opt"] == [Props.POWER.value]


def test_parent_mac_strips_cloud_child_suffix_only() -> None:
    assert parent_mac("aabbccddeeff00") == "aabbccddeeff"
    assert parent_mac("aabbccddeeff") == "aabbccddeeff"
