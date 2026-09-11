"""Local UDP transport for Gree devices."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

from .crypto import EcbCipher, GcmCipher, cipher_for_packet
from .device import GreeDevice, Props
from .models import DeviceInfo

_LOGGER = logging.getLogger(__name__)
DEFAULT_PORT = 7000
STATUS_PROPERTIES = [prop.value for prop in Props]


def _outer_packet(mac: str, inner: dict[str, Any], *, initial: bool = False) -> dict[str, Any]:
    return {
        "cid": "app",
        "i": 1 if initial else 0,
        "pack": inner,
        "t": "pack",
        "tcid": mac,
        "uid": 0,
    }


def _encrypt_packet(
    packet: dict[str, Any], cipher: EcbCipher | GcmCipher
) -> bytes:
    encrypted = dict(packet)
    payload, tag = cipher.encrypt(packet["pack"])
    encrypted["pack"] = payload
    if tag:
        encrypted["tag"] = tag
    return json.dumps(encrypted, separators=(",", ":")).encode()


def _decrypt_packet(
    packet: dict[str, Any], cipher: EcbCipher | GcmCipher
) -> dict[str, Any]:
    return cipher.decrypt(packet["pack"], packet.get("tag"))


async def discover_local_devices(
    broadcast_addresses: list[str], timeout: float = 3
) -> list[DeviceInfo]:
    """Broadcast a Gree scan and return unique LAN responders."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    try:
        request = json.dumps({"t": "scan"}, separators=(",", ":")).encode()
        for address in set(broadcast_addresses or ["255.255.255.255"]):
            await loop.sock_sendto(sock, request, (address, DEFAULT_PORT))

        devices: dict[str, DeviceInfo] = {}
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                data, sender = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), remaining)
            except TimeoutError:
                break
            try:
                packet = json.loads(data)
                inner = _decrypt_packet(packet, cipher_for_packet(packet.get("tag")))
                mac = str(inner.get("mac") or inner.get("cid") or "").lower()
                if mac:
                    devices[mac] = DeviceInfo(
                        ip=sender[0],
                        port=sender[1],
                        mac=mac,
                        name=str(inner.get("name") or mac),
                        brand=inner.get("brand"),
                        model=inner.get("model"),
                        version=inner.get("ver"),
                    )
            except (KeyError, ValueError, json.JSONDecodeError):
                _LOGGER.debug("Ignoring an invalid Gree discovery response")
        return list(devices.values())
    finally:
        sock.close()


class LocalDevice(GreeDevice):
    """Gree device controlled directly over UDP."""

    def __init__(self, device_info: DeviceInfo, timeout: float = 3) -> None:
        super().__init__(device_info)
        self.timeout = timeout
        self._cipher: EcbCipher | GcmCipher | None = None
        self._lock = asyncio.Lock()

    async def _exchange(
        self,
        inner: dict[str, Any],
        cipher: EcbCipher | GcmCipher,
        *,
        initial: bool = False,
    ) -> dict[str, Any]:
        packet = _encrypt_packet(
            _outer_packet(self.device_info.mac, inner, initial=initial), cipher
        )
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            await loop.sock_sendto(
                sock, packet, (self.device_info.ip, self.device_info.port)
            )
            data, _ = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 65535), timeout=self.timeout
            )
            response = json.loads(data)
            return _decrypt_packet(response, cipher)
        finally:
            sock.close()

    async def bind(self) -> None:
        """Negotiate the per-device key, trying both documented LAN ciphers."""
        inner = {"mac": self.device_info.mac, "t": "bind", "uid": 0}
        last_error: Exception | None = None
        for cipher in (EcbCipher(), GcmCipher()):
            try:
                response = await self._exchange(inner, cipher, initial=True)
                key = response.get("key")
                if response.get("t") != "bindok" or not key:
                    raise ValueError("Unexpected Gree bind response")
                self._cipher = type(cipher)(str(key).encode())
                await self.update_state()
                return
            except (TimeoutError, ValueError, KeyError) as error:
                last_error = error
        raise TimeoutError(
            f"Unable to bind local Gree device {self.device_info.mac}"
        ) from last_error

    async def update_state(self) -> None:
        if self._cipher is None:
            raise RuntimeError("Local device has not been bound")
        inner = {
            "cols": [*STATUS_PROPERTIES, "hid"],
            "mac": self.device_info.mac,
            "t": "status",
        }
        async with self._lock:
            response = await self._exchange(inner, self._cipher)
        if response.get("t") != "dat":
            raise ValueError("Unexpected Gree status response")
        columns, values = response.get("cols", []), response.get("dat", [])
        if len(columns) != len(values):
            raise ValueError("Gree status response has mismatched columns")
        self.apply_state(dict(zip(columns, values, strict=True)))

    async def push_state_update(self) -> None:
        if self._cipher is None:
            raise RuntimeError("Local device has not been bound")
        if not self._dirty:
            return
        names = list(self._dirty)
        values = [self.raw_properties[name] for name in names]
        inner = {"mac": self.device_info.mac, "opt": names, "p": values, "t": "cmd"}
        async with self._lock:
            response = await self._exchange(inner, self._cipher)
        if response.get("t") not in ("res", "dat"):
            raise ValueError("Unexpected Gree command response")
        self._dirty.clear()
        returned_names = response.get("opt") or response.get("cols") or []
        returned_values = response.get("val") or response.get("p") or response.get("dat") or []
        if len(returned_names) == len(returned_values):
            self.apply_state(dict(zip(returned_names, returned_values, strict=True)))

    async def close(self) -> None:
        """The request-scoped UDP transport has no persistent resource to close."""
