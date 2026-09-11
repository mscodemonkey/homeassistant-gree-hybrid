"""Cloud MQTT transport for Gree devices."""

from __future__ import annotations

import asyncio
from typing import Any

from .crypto import EcbCipher
from .device import GreeDevice, Props
from .models import DeviceInfo
from .mqtt import GreeMqttClient

EXTRA_STATUS_PROPERTIES = [
    "WatTmp",
    "SetTemInt",
    "SetTemDec",
    "Wstate",
    "powConsump",
    "ElcAll",
    "CompressorFqy",
]


def parent_mac(mac: str) -> str:
    """Return the MQTT parent address used by multi-part Gree devices."""
    return mac[:-2] if len(mac) > 12 and mac.endswith("00") else mac


class CloudDevice(GreeDevice):
    """Gree device controlled through its regional cloud MQTT broker."""

    def __init__(
        self,
        mqtt_client: GreeMqttClient,
        device_info: DeviceInfo,
        device_key: str,
        *,
        timeout: float = 10,
    ) -> None:
        super().__init__(device_info)
        self.mqtt_client = mqtt_client
        self.child_mac = device_info.mac
        self.parent_mac = parent_mac(device_info.mac)
        self.cipher = EcbCipher(device_key.encode())
        self.timeout = timeout
        self._response_event: asyncio.Event | None = None
        self._response_state: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        mqtt_client.add_message_handler(self._handle_message)

    async def bind(self) -> None:
        await self.mqtt_client.subscribe_to_device(self.parent_mac)
        await self.update_state()

    async def _request(
        self, command: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            self._response_event = asyncio.Event()
            self._response_state = None
            try:
                await self.mqtt_client.publish_command(
                    self.parent_mac,
                    self.child_mac,
                    {"mac": self.child_mac, **command},
                    self.cipher,
                )
                await asyncio.wait_for(
                    self._response_event.wait(), timeout=timeout or self.timeout
                )
                return self._response_state or {}
            finally:
                self._response_event = None
                self._response_state = None

    def _handle_message(self, topic: str, envelope: dict[str, Any]) -> None:
        if self.parent_mac not in topic and self.child_mac not in topic:
            return
        payload = envelope.get("pack")
        if not payload:
            return
        try:
            decoded = self.cipher.decrypt(payload, envelope.get("tag"))
        except (ValueError, KeyError):
            return
        if decoded.get("t") not in ("dat", "res"):
            return
        names = decoded.get("cols") or decoded.get("opt") or []
        values = decoded.get("dat") or decoded.get("val") or decoded.get("p") or []
        if len(names) != len(values):
            return
        state = dict(zip(names, values, strict=True))
        if self._response_event is not None:
            self._response_state = state
            self._response_event.set()
        else:
            self.apply_state(state)

    async def update_state(self) -> None:
        names = [prop.value for prop in Props] + EXTRA_STATUS_PROPERTIES + ["hid"]
        state = await self._request({"t": "status", "cols": names})
        self.apply_state(state)

    def _commands(self) -> list[dict[str, Any]]:
        pending = {name: self.raw_properties[name] for name in self._dirty}
        commands: list[dict[str, Any]] = []
        mode = pending.pop(Props.MODE.value, None)
        if mode is not None:
            commands.append({"t": "cmd", "opt": [Props.MODE.value], "p": [mode]})
        temperature_names = [
            Props.TEMP_SET.value,
            Props.TEMP_BIT.value,
            Props.TEMP_DECI.value,
            Props.TEMP_HALF_DEGREE.value,
            Props.TEMP_UNIT.value,
        ]
        if any(name in pending for name in temperature_names):
            selected = [name for name in temperature_names if name in self.raw_properties]
            commands.append(
                {
                    "t": "cmd",
                    "opt": selected,
                    "p": [self.raw_properties[name] for name in selected],
                }
            )
            for name in temperature_names:
                pending.pop(name, None)
        power = pending.pop(Props.POWER.value, None)
        commands.extend(
            {"t": "cmd", "opt": [name], "p": [value]}
            for name, value in pending.items()
        )
        if power is not None:
            commands.append({"t": "cmd", "opt": [Props.POWER.value], "p": [power]})
        return commands

    async def push_state_update(self) -> None:
        if not self._dirty:
            return
        for command in self._commands():
            try:
                state = await self._request(command, timeout=3)
            except TimeoutError:
                state = {}
            self.apply_state(state)
        self._dirty.clear()

    async def close(self) -> None:
        await self.mqtt_client.unsubscribe_from_device(self.parent_mac)
        self.mqtt_client.remove_message_handler(self._handle_message)

    async def replace_mqtt_client(self, mqtt_client: GreeMqttClient) -> None:
        """Move this device to a newly authenticated shared MQTT connection."""
        self.mqtt_client.remove_message_handler(self._handle_message)
        self.mqtt_client = mqtt_client
        mqtt_client.add_message_handler(self._handle_message)
        await mqtt_client.subscribe_to_device(self.parent_mac)
