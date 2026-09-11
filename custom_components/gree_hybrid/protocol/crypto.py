"""Gree packet encryption primitives."""

from __future__ import annotations

import base64
import json
from typing import Any

from Crypto.Cipher import AES

LOCAL_ECB_KEY = b"a3K8Bx%2r8Y7#xDh"
LOCAL_GCM_KEY = b"{yxAHAY_Lm6pbC/<"
GCM_NONCE = b"\x54\x40\x78\x44\x49\x67\x5a\x51\x6c\x5e\x63\x13"
GCM_ASSOCIATED_DATA = b"qualcomm-test"


def _encode_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _pad(data: bytes) -> bytes:
    length = AES.block_size - len(data) % AES.block_size
    return data + bytes([length]) * length


def _unpad(data: bytes) -> bytes:
    length = data[-1]
    if length < 1 or length > AES.block_size or data[-length:] != bytes([length]) * length:
        raise ValueError("Invalid encrypted packet padding")
    return data[:-length]


class EcbCipher:
    """AES-128-ECB used by first-generation Gree modules and cloud devices."""

    def __init__(self, key: bytes = LOCAL_ECB_KEY) -> None:
        if len(key) != AES.block_size:
            raise ValueError("Gree AES keys must be 16 bytes")
        self.key = key

    def encrypt(self, value: dict[str, Any]) -> tuple[str, None]:
        encrypted = AES.new(self.key, AES.MODE_ECB).encrypt(_pad(_encode_json(value)))
        return base64.b64encode(encrypted).decode(), None

    def decrypt(self, payload: str, tag: str | None = None) -> dict[str, Any]:
        decrypted = AES.new(self.key, AES.MODE_ECB).decrypt(base64.b64decode(payload))
        return json.loads(_unpad(decrypted))


class GcmCipher:
    """AES-128-GCM used by later LAN-capable Gree modules."""

    def __init__(self, key: bytes = LOCAL_GCM_KEY) -> None:
        if len(key) != AES.block_size:
            raise ValueError("Gree AES keys must be 16 bytes")
        self.key = key

    def _cipher(self) -> AES.GcmMode:
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=GCM_NONCE)
        cipher.update(GCM_ASSOCIATED_DATA)
        return cipher

    def encrypt(self, value: dict[str, Any]) -> tuple[str, str]:
        encrypted, tag = self._cipher().encrypt_and_digest(_encode_json(value))
        return base64.b64encode(encrypted).decode(), base64.b64encode(tag).decode()

    def decrypt(self, payload: str, tag: str | None = None) -> dict[str, Any]:
        if tag is None:
            raise ValueError("GCM packet is missing its authentication tag")
        decrypted = self._cipher().decrypt_and_verify(
            base64.b64decode(payload), base64.b64decode(tag)
        )
        return json.loads(decrypted)


def cipher_for_packet(tag: str | None) -> EcbCipher | GcmCipher:
    """Choose the generic cipher advertised by a discovery/bind packet."""
    return GcmCipher() if tag else EcbCipher()
