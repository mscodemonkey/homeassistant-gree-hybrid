"""Data structures shared by Gree transports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DeviceInfo:
    """Network and identity information for a Gree device."""

    ip: str
    port: int
    mac: str
    name: str
    brand: str | None = None
    model: str | None = None
    version: str | None = None


@dataclass(slots=True)
class CloudDeviceInfo:
    """Device identity and key returned by the Gree account service."""

    name: str
    mac: str
    key: str
    model: str | None = None
    version: str | None = None
    online: bool = True


@dataclass(slots=True)
class CloudCredentials:
    """Short-lived credentials used to connect to Gree MQTT."""

    user_id: int
    token: str
