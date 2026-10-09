"""Discover Gree transports and expose per-device polling to entity platforms.

Each account entry owns its coordinators and transport lifetimes. Discovery
isolates device failures before handing responsive coordinators to platform setup.
"""

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
from homeassistant.exceptions import ConfigEntryNotReady
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
    """Poll and command an entry-owned device through its selected transport.

    Entity platforms listen to this coordinator. The account entry closes its
    device on unload, while discovery closes it if initialization fails.
    """

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
        """Send pending changes, retrying once after an MQTT reconnection."""
        try:
            await self.device.push_state_update()
        except Exception as error:
            if self.transport == TRANSPORT_CLOUD and _mqtt_disconnected(error):
                if await _reconnect(self.hass, self.config_entry):
                    await self.device.push_state_update()
                    return
            raise


class CloudDiscoveryService:
    """Build account-owned coordinators for responsive cloud and LAN devices.

    The config entry owns returned transports until unload. Failed devices are
    closed and omitted until the next entry reload. Account failures propagate.
    """

    def __init__(
        self, hass: HomeAssistant, entry: GreeHybridConfigEntry, api: GreeCloudApi
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api

    async def discover_devices(
        self, mqtt_client: GreeMqttClient
    ) -> list[DeviceDataUpdateCoordinator]:
        """Return responsive devices, or request a setup retry if none respond."""
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
        try:
            for account_device in account_devices:
                try:
                    coordinator = await self._setup_device(
                        mqtt_client, account_device, local_by_mac
                    )
                except Exception as error:
                    _LOGGER.warning(
                        "Skipping an unreachable Gree device during startup (%s). "
                        "Reload the integration once the device is online",
                        type(error).__name__,
                    )
                    continue
                coordinators.append(coordinator)
                async_dispatcher_send(self.hass, DISPATCH_DEVICE_DISCOVERED, coordinator)
            if account_devices and not coordinators:
                raise ConfigEntryNotReady("No Gree devices responded during startup")
            return coordinators
        except BaseException:
            for coordinator in coordinators:
                await self._close_device(coordinator.device)
            raise

    async def _setup_device(
        self,
        mqtt_client: GreeMqttClient,
        account_device: Any,
        local_by_mac: dict[str, Any],
    ) -> DeviceDataUpdateCoordinator:
        """Transfer one bound, refreshed transport to a coordinator or close it.

        Cancellation propagates after cleanup. A second cancellation during
        cleanup can interrupt closing, as it can during normal entry unload.
        """
        local_info = matching_local_device(account_device.mac, local_by_mac)
        device: GreeDevice | None = None
        try:
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
                try:
                    await device.bind()
                    transport = TRANSPORT_LOCAL
                except Exception as error:
                    await self._close_device(device)
                    device = None
                    _LOGGER.debug("LAN binding failed, selecting cloud (%s)", type(error).__name__)
                    device = self._cloud_device(mqtt_client, account_device)
                    await device.bind()
                    transport = TRANSPORT_CLOUD
            else:
                device = self._cloud_device(mqtt_client, account_device)
                await device.bind()
                transport = TRANSPORT_CLOUD
            coordinator = DeviceDataUpdateCoordinator(self.hass, self.entry, device, transport)
            await coordinator.async_config_entry_first_refresh()
            return coordinator
        except BaseException:
            if device is not None:
                await self._close_device(device)
            raise

    @staticmethod
    async def _close_device(device: GreeDevice) -> None:
        """Close a failed transport without masking its original startup error."""
        try:
            await device.close()
        except Exception as error:
            _LOGGER.debug("Gree device cleanup failed (%s)", type(error).__name__)

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
