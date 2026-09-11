"""Pure helpers for matching Gree cloud and LAN device identities."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_mac(value: str | None) -> str:
    """Return a lower-case twelve-digit MAC address for matching."""
    if not value:
        return ""
    return "".join(character for character in value.lower() if character in "0123456789abcdef")


def local_devices_by_mac(devices: Iterable[Any]) -> dict[str, Any]:
    """Index discovered LAN devices by their normalized MAC address."""
    return {
        normalized: device
        for device in devices
        if (normalized := normalize_mac(getattr(device, "mac", None)))
    }


def matching_local_device(cloud_mac: str, devices_by_mac: dict[str, Any]) -> Any | None:
    """Find the LAN discovery record belonging to a cloud device."""
    return devices_by_mac.get(normalize_mac(cloud_mac))
