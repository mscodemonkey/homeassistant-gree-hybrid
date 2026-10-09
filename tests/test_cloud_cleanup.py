"""Verify cloud transport ownership at the shared MQTT handler boundary.

Tests own fake broker failures and consume the real transport close method.
No connection or device commands leave the test process.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from gree_hybrid_protocol.cloud import CloudDevice
from gree_hybrid_protocol.models import DeviceInfo


def test_unsubscribe_failure_still_removes_handler() -> None:
    """A failed broker unsubscribe must not retain a skipped device's handler."""
    mqtt = SimpleNamespace(add_message_handler=Mock(), remove_message_handler=Mock(),
                           unsubscribe_from_device=AsyncMock(side_effect=RuntimeError()))
    device = CloudDevice(mqtt, DeviceInfo("127.0.0.1", 0, "synthetic", "Test unit"),
                         "0123456789abcdef")
    with pytest.raises(RuntimeError):
        asyncio.run(device.close())
    mqtt.remove_message_handler.assert_called_once_with(device._handle_message)
