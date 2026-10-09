"""Exercise startup isolation without network access or an installed HA runtime.

Tests own temporary import stubs and fake transports. The real discovery service
consumes these boundaries, so failures exercise its actual ownership decisions.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parents[1] / "custom_components" / "gree_hybrid"


@pytest.fixture
def discovery(monkeypatch):
    """Load the real discovery module against isolated, per-test HA boundaries."""
    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    class Generic:
        def __class_getitem__(cls, item):
            return cls

    class ConfigEntryNotReady(Exception):
        pass

    class Coordinator(Generic):
        def __init__(self, *args, **kwargs):
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            if self.device.refresh_error:
                raise self.device.refresh_error

    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.network", async_get_ipv4_broadcast_addresses=AsyncMock(
        return_value=[]
    ))
    module("homeassistant.config_entries", ConfigEntry=Generic)
    module("homeassistant.core", HomeAssistant=Generic)
    module("homeassistant.exceptions", ConfigEntryNotReady=ConfigEntryNotReady)
    module("homeassistant.helpers")
    dispatch = []
    module("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda *args: dispatch.append(
        args[-1]
    ))
    module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator,
           UpdateFailed=RuntimeError)
    package = module("startup_test_gree")
    package.__path__ = [str(ROOT)]
    protocol = module("startup_test_gree.protocol")
    protocol.__path__ = [str(ROOT / "protocol")]
    module("startup_test_gree.const", DISCOVERY_TIMEOUT=1, DISPATCH_DEVICE_DISCOVERED="discovered",
           DOMAIN="gree_hybrid", HWHP_PROP_WATER_TEMP="WatTmp", LOCAL_UPDATE_INTERVAL=1,
           MAX_ERRORS=3, TRANSPORT_CLOUD="cloud", TRANSPORT_LOCAL="local", UPDATE_INTERVAL=1)
    for name, symbol in (("cloud", "CloudDevice"), ("cloud_api", "GreeCloudApi"),
                         ("device", "GreeDevice"), ("mqtt", "GreeMqttClient")):
        module(f"startup_test_gree.protocol.{name}", **{symbol: Generic})
    local = module("startup_test_gree.protocol.local", LocalDevice=Generic,
                   discover_local_devices=AsyncMock(return_value=[]))
    spec = importlib.util.spec_from_file_location("startup_test_gree.coordinator",
                                                 ROOT / "coordinator.py")
    value = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, value)
    spec.loader.exec_module(value)
    yield SimpleNamespace(module=value, local=local, dispatch=dispatch,
                          not_ready=ConfigEntryNotReady)


def account(index):
    """Build a synthetic account record consumed by routing and discovery."""
    return SimpleNamespace(mac=f"00000000000{index}", name=f"Unit {index}", model=None,
                           version=None, key="synthetic")


def transport(bind_error=None, refresh_error=None, close_error=None):
    """Create an observable transport with independently injectable failures."""
    return SimpleNamespace(device_info=SimpleNamespace(name="Synthetic unit"),
                           bind=AsyncMock(side_effect=bind_error),
                           close=AsyncMock(side_effect=close_error), refresh_error=refresh_error)


def service(discovery, monkeypatch, accounts, devices):
    """Wire a discovery service to fake account and cloud transport boundaries."""
    api = SimpleNamespace(get_all_devices=AsyncMock(return_value=accounts))
    result = discovery.module.CloudDiscoveryService(object(), object(), api)
    monkeypatch.setattr(result, "_cloud_device", lambda mqtt, info: devices[info.mac])
    return result


@pytest.mark.parametrize("offline", [0, 1, 2])
def test_offline_unit_does_not_discard_others(discovery, monkeypatch, offline, caplog):
    """A failed first, middle or last unit leaves successful coordinators intact."""
    accounts = [account(i) for i in range(3)]
    devices = {item.mac: transport(TimeoutError("private-device-key") if i == offline else None)
               for i, item in enumerate(accounts)}
    result = asyncio.run(service(discovery, monkeypatch, accounts, devices).discover_devices(None))
    expected = [devices[item.mac] for i, item in enumerate(accounts) if i != offline]
    assert [item.device for item in result] == expected
    assert discovery.dispatch == result
    devices[accounts[offline].mac].close.assert_awaited_once()
    for device in expected:
        device.close.assert_not_awaited()
    assert "private-device-key" not in caplog.text
    assert all(item.mac not in caplog.text for item in accounts)


def test_all_offline_requests_setup_retry(discovery, monkeypatch):
    """No responsive units must retain HA's automatic setup retry path."""
    accounts = [account(0), account(1)]
    devices = {item.mac: transport(TimeoutError()) for item in accounts}
    with pytest.raises(discovery.not_ready):
        asyncio.run(service(discovery, monkeypatch, accounts, devices).discover_devices(None))
    for device in devices.values():
        device.close.assert_awaited_once()
    assert not discovery.dispatch


def test_empty_account(discovery, monkeypatch):
    """An empty account keeps its pre-existing successful empty result."""
    assert asyncio.run(service(discovery, monkeypatch, [], {}).discover_devices(None)) == []


@pytest.mark.parametrize("cloud_failure", [False, True])
def test_lan_fallback_with_failing_cleanup(discovery, monkeypatch, cloud_failure):
    """A broken LAN close must not prevent cloud fallback or other devices."""
    accounts = [account(0), account(1)]
    lan = transport(TimeoutError(), close_error=RuntimeError("cleanup"))
    cloud = transport(TimeoutError() if cloud_failure else None)
    devices = {accounts[0].mac: cloud, accounts[1].mac: transport()}
    info = SimpleNamespace(mac=accounts[0].mac, ip="127.0.0.1", port=1,
                           brand=None, model=None, version=None)
    discovery.local.discover_local_devices.return_value = [info]
    monkeypatch.setattr(discovery.module, "LocalDevice", lambda info: lan)
    result = asyncio.run(service(discovery, monkeypatch, accounts, devices).discover_devices(None))
    lan.close.assert_awaited_once()
    assert len(result) == (1 if cloud_failure else 2)
    assert result[-1].device is devices[accounts[1].mac]
    if cloud_failure:
        cloud.close.assert_awaited_once()


def test_successful_lan_keeps_local_route(discovery, monkeypatch):
    """Responsive LAN transports retain priority over the cloud route."""
    item = account(0)
    lan = transport()
    info = SimpleNamespace(mac=item.mac, ip="127.0.0.1", port=1,
                           brand=None, model=None, version=None)
    discovery.local.discover_local_devices.return_value = [info]
    monkeypatch.setattr(discovery.module, "LocalDevice", lambda info: lan)
    result = asyncio.run(service(discovery, monkeypatch, [item], {}).discover_devices(None))
    assert result[0].device is lan
    assert result[0].transport == "local"


def test_first_refresh_failure_is_isolated(discovery, monkeypatch):
    """A coordinator refresh failure closes only its own transport."""
    accounts = [account(0), account(1)]
    devices = {accounts[0].mac: transport(refresh_error=RuntimeError()),
               accounts[1].mac: transport()}
    result = asyncio.run(service(discovery, monkeypatch, accounts, devices).discover_devices(None))
    assert [item.device for item in result] == [devices[accounts[1].mac]]
    devices[accounts[0].mac].close.assert_awaited_once()


@pytest.mark.parametrize("stage", ["bind", "refresh", "lan_close"])
def test_cancellation_closes_current_and_previous(discovery, monkeypatch, stage):
    """Cancelled discovery cleans retained resources and stops before later units."""
    accounts = [account(i) for i in range(3)]
    current = transport(asyncio.CancelledError() if stage == "bind" else None,
                        asyncio.CancelledError() if stage == "refresh" else None)
    previous = transport(close_error=RuntimeError("cannot close"))
    devices = {accounts[0].mac: previous, accounts[1].mac: current,
               accounts[2].mac: transport()}
    if stage == "lan_close":
        current.bind.side_effect = TimeoutError()
        current.close.side_effect = [asyncio.CancelledError(), None]
        info = SimpleNamespace(mac=accounts[1].mac, ip="127.0.0.1", port=1,
                               brand=None, model=None, version=None)
        discovery.local.discover_local_devices.return_value = [info]
        monkeypatch.setattr(discovery.module, "LocalDevice", lambda info: current)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service(discovery, monkeypatch, accounts, devices).discover_devices(None))
    assert current.close.await_count >= 1
    previous.close.assert_awaited_once()
    devices[accounts[2].mac].bind.assert_not_awaited()


def test_system_exit_propagates(discovery, monkeypatch):
    """Process termination must never be converted into an offline device."""
    item = account(0)
    device = transport(SystemExit(2))
    with pytest.raises(SystemExit):
        asyncio.run(service(discovery, monkeypatch, [item], {item.mac: device})
                    .discover_devices(None))
    device.close.assert_awaited_once()


def test_account_discovery_error_propagates(discovery, monkeypatch):
    """An account service failure remains an account-level setup failure."""
    instance = service(discovery, monkeypatch, [], {})
    instance.api.get_all_devices.side_effect = ValueError("account failed")
    with pytest.raises(ValueError, match="account failed"):
        asyncio.run(instance.discover_devices(None))


def test_local_discovery_error_uses_cloud(discovery, monkeypatch):
    """A LAN scan failure leaves account devices eligible for cloud setup."""
    discovery.local.discover_local_devices.side_effect = OSError("scan failed")
    item = account(0)
    device = transport()
    result = asyncio.run(service(discovery, monkeypatch, [item], {item.mac: device})
                         .discover_devices(None))
    assert result[0].device is device
