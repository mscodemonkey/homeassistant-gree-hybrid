"""Home Assistant coordinators and hybrid Gree discovery."""

from __future__ import annotations

import asyncio
import copy
import importlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.components.network import async_get_ipv4_broadcast_addresses
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DISCOVERY_TIMEOUT,
    DISPATCH_DEVICE_DISCOVERED,
    DOMAIN,
    HWHP_PROP_WATER_TEMP,
    LOCAL_UPDATE_INTERVAL,
    MAX_ERRORS,
    TRANSPORT_CLOUD,
    TRANSPORT_LOCAL,
    UPDATE_INTERVAL,
)
from .protocol.cloud import CloudDevice
from .protocol.cloud_api import GreeCloudApi
from .protocol.device import GreeDevice
from .protocol.local import LocalDevice, discover_local_devices
from .protocol.models import DeviceInfo
from .protocol.mqtt import GreeMqttClient
from .routing import local_devices_by_mac, matching_local_device

_LOGGER = logging.getLogger(__name__)


@dataclass
class GreeHybridRuntimeData:
    """Resources owned by one Gree+ account config entry."""

    cloud_api: GreeCloudApi
    mqtt_client: GreeMqttClient
    coordinators: list[DeviceDataUpdateCoordinator]
    mqtt_reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


type GreeHybridConfigEntry = ConfigEntry[GreeHybridRuntimeData]
GreeCloudConfigEntry = GreeHybridConfigEntry
GreeCloudRuntimeData = GreeHybridRuntimeData


def is_hwhp_device(coordinator: DeviceDataUpdateCoordinator) -> bool:
    """Return whether returned state identifies a hot-water heat pump."""
    value = coordinator.device.raw_properties.get(HWHP_PROP_WATER_TEMP)
    return value is not None and value > 0


def _mqtt_disconnected(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ("code:4", "not connected", "disconnected"))


async def _reconnect(hass: HomeAssistant, entry: GreeHybridConfigEntry) -> bool:
    module = importlib.import_module(__name__.rsplit(".", 1)[0])
    return await module.async_reconnect_mqtt(hass, entry)


class DeviceDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll and command one device through its selected transport."""

    config_entry: GreeHybridConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: GreeHybridConfigEntry,
        device: GreeDevice,
        transport: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}-{device.device_info.name}",
            update_interval=timedelta(
                seconds=LOCAL_UPDATE_INTERVAL
                if transport == TRANSPORT_LOCAL
                else UPDATE_INTERVAL
            ),
            always_update=False,
        )
        self.device = device
        self.transport = transport
        self._error_count = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            await self.device.update_state()
            self._error_count = 0
            return copy.deepcopy(self.device.raw_properties)
        except Exception as error:
            if self.transport == TRANSPORT_CLOUD and _mqtt_disconnected(error):
                if await _reconnect(self.hass, self.config_entry):
                    await self.device.update_state()
                    self._error_count = 0
                    return copy.deepcopy(self.device.raw_properties)
            self._error_count += 1
            if self._error_count >= MAX_ERRORS:
                raise UpdateFailed(
                    f"{self.device.device_info.name} is unavailable via {self.transport}"
                ) from error
            _LOGGER.warning(
                "State update failed for %s via %s (%d/%d): %s",
                self.device.device_info.name,
                self.transport,
                self._error_count,
                MAX_ERRORS,
                error,
            )
            return copy.deepcopy(self.device.raw_properties)

    async def push_state_update(self) -> None:
        try:
            await self.device.push_state_update()
        except Exception as error:
            if self.transport == TRANSPORT_CLOUD and _mqtt_disconnected(error):
                if await _reconnect(self.hass, self.config_entry):
                    await self.device.push_state_update()
                    return
            raise


class CloudDiscoveryService:
    """Join Gree+ account discovery with local UDP discovery by MAC address."""

    def __init__(
        self, hass: HomeAssistant, entry: GreeHybridConfigEntry, api: GreeCloudApi
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api

    async def discover_devices(
        self, mqtt_client: GreeMqttClient
    ) -> list[DeviceDataUpdateCoordinator]:
        account_devices = await self.api.get_all_devices()
        broadcasts = [
            str(address) for address in await async_get_ipv4_broadcast_addresses(self.hass)
        ]
        try:
            local_devices = await discover_local_devices(broadcasts, DISCOVERY_TIMEOUT)
        except Exception as error:
            _LOGGER.warning("Local Gree discovery failed; cloud remains available: %s", error)
            local_devices = []
        local_by_mac = local_devices_by_mac(local_devices)

        coordinators: list[DeviceDataUpdateCoordinator] = []
        current_device: GreeDevice | None = None
        try:
            for account_device in account_devices:
                local_info = matching_local_device(account_device.mac, local_by_mac)
                device: GreeDevice
                transport: str
                if local_info is not None:
                    device = LocalDevice(
                        DeviceInfo(
                            ip=local_info.ip,
                            port=local_info.port,
                            mac=account_device.mac,
                            name=account_device.name,
                            brand=local_info.brand,
                            model=account_device.model or local_info.model,
                            version=account_device.version or local_info.version,
                        )
                    )
                    current_device = device
                    try:
                        await device.bind()
                        transport = TRANSPORT_LOCAL
                    except Exception as error:
                        await device.close()
                        _LOGGER.info(
                            "LAN binding failed for %s; selecting cloud: %s",
                            account_device.name,
                            error,
                        )
                        device = self._cloud_device(mqtt_client, account_device)
                        current_device = device
                        await device.bind()
                        transport = TRANSPORT_CLOUD
                else:
                    device = self._cloud_device(mqtt_client, account_device)
                    current_device = device
                    await device.bind()
                    transport = TRANSPORT_CLOUD

                _LOGGER.info(
                    "Using %s transport for %s (MAC: %s)",
                    transport,
                    device.device_info.name,
                    device.device_info.mac,
                )
                coordinator = DeviceDataUpdateCoordinator(
                    self.hass, self.entry, device, transport
                )
                await coordinator.async_config_entry_first_refresh()
                coordinators.append(coordinator)
                current_device = None
                async_dispatcher_send(self.hass, DISPATCH_DEVICE_DISCOVERED, coordinator)
            return coordinators
        except Exception:
            if current_device is not None:
                await current_device.close()
            for coordinator in coordinators:
                await coordinator.device.close()
            raise

    @staticmethod
    def _cloud_device(mqtt_client: GreeMqttClient, account_device: Any) -> CloudDevice:
        return CloudDevice(
            mqtt_client,
            DeviceInfo(
                ip="0.0.0.0",
                port=0,
                mac=account_device.mac,
                name=account_device.name,
                model=account_device.model,
                version=account_device.version,
            ),
            account_device.key,
        )
