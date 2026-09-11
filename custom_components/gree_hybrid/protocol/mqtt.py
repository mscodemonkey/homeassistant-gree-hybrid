"""MQTT connection used by Gree cloud-routed devices."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
from collections.abc import Callable
from typing import Any

import aiomqtt

from .crypto import EcbCipher

_LOGGER = logging.getLogger(__name__)
MessageHandler = Callable[[str, dict[str, Any]], None]


class GreeMqttClient:
    """Own one authenticated connection to a regional Gree MQTT broker."""

    def __init__(
        self,
        user_id: int,
        token: str,
        server: str,
        *,
        port: int = 1984,
    ) -> None:
        self.user_id = user_id
        self.token = token
        self.server = server
        self.port = port
        self.client_id = f"app_{secrets.token_hex(8)}"
        self._client: aiomqtt.Client | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._handlers: set[MessageHandler] = set()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._receive_task is not None

    async def connect(self) -> None:
        if self.is_connected:
            return
        self._client = aiomqtt.Client(
            hostname=self.server,
            port=self.port,
            username=str(self.user_id),
            password=self.token,
            identifier=self.client_id,
            keepalive=60,
            protocol=aiomqtt.ProtocolVersion.V311,
            clean_session=True,
            tls_context=ssl.create_default_context(),
            timeout=30,
        )
        await self._client.__aenter__()
        self._receive_task = asyncio.create_task(self._receive())

    async def disconnect(self) -> None:
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def add_message_handler(self, handler: MessageHandler) -> None:
        self._handlers.add(handler)

    def remove_message_handler(self, handler: MessageHandler) -> None:
        self._handlers.discard(handler)

    async def subscribe_to_device(self, mac: str) -> None:
        if self._client is None:
            raise RuntimeError("Gree MQTT is disconnected")
        for topic in (f"response/{mac}/#", f"status/{mac}/#", f"connect/{mac}"):
            await self._client.subscribe(topic, qos=1)

    async def unsubscribe_from_device(self, mac: str) -> None:
        if self._client is None:
            return
        for topic in (f"response/{mac}/#", f"status/{mac}/#", f"connect/{mac}"):
            await self._client.unsubscribe(topic)

    async def publish_command(
        self,
        parent_mac: str,
        child_mac: str,
        command: dict[str, Any],
        cipher: EcbCipher,
    ) -> None:
        if self._client is None:
            raise RuntimeError("Gree MQTT is disconnected")
        encrypted, tag = cipher.encrypt(command)
        envelope: dict[str, Any] = {
            "cid": str(secrets.randbelow(9_000_000_000) + 1_000_000_000),
            "i": 0,
            "pack": encrypted,
            "t": "pack",
            "tcid": child_mac,
            "uid": self.user_id,
        }
        if tag:
            envelope["tag"] = tag
        await self._client.publish(
            f"request/{parent_mac}", json.dumps(envelope, separators=(",", ":")), qos=1
        )

    async def _receive(self) -> None:
        assert self._client is not None
        try:
            async for message in self._client.messages:
                try:
                    payload = json.loads(bytes(message.payload))
                    topic = str(message.topic)
                except (TypeError, ValueError, json.JSONDecodeError):
                    _LOGGER.debug("Ignoring malformed Gree MQTT message")
                    continue
                for handler in tuple(self._handlers):
                    try:
                        handler(topic, payload)
                    except Exception:
                        _LOGGER.exception("Gree MQTT message handler failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Gree MQTT receive loop stopped")
