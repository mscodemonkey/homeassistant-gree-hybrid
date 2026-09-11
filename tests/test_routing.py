"""Tests for transport routing helpers."""

import importlib.util
from dataclasses import dataclass
from pathlib import Path

ROUTING_PATH = Path(__file__).parents[1] / "custom_components" / "gree_hybrid" / "routing.py"
SPEC = importlib.util.spec_from_file_location("gree_hybrid_routing", ROUTING_PATH)
assert SPEC and SPEC.loader
routing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routing)

local_devices_by_mac = routing.local_devices_by_mac
matching_local_device = routing.matching_local_device
normalize_mac = routing.normalize_mac


@dataclass
class Device:
    mac: str


def test_normalize_mac_accepts_common_formats() -> None:
    assert normalize_mac("94:24:B8:6C:01:78") == "9424b86c0178"
    assert normalize_mac("94-24-b8-6c-01-78") == "9424b86c0178"
    assert normalize_mac("9424B86C0178") == "9424b86c0178"


def test_cloud_device_matches_lan_discovery_by_mac() -> None:
    local = Device("94:24:B8:6C:01:78")
    indexed = local_devices_by_mac([local])

    assert matching_local_device("9424b86c0178", indexed) is local
    assert matching_local_device("580d0d35fcd5", indexed) is None
