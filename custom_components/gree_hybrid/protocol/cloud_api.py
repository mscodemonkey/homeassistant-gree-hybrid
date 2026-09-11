"""Gree+ account authentication and device discovery."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import aiohttp
from Crypto.Cipher import AES

from .models import CloudCredentials, CloudDeviceInfo

GREE_CLOUD_SERVERS = {
    "Australia": "https://augrih.gree.com",
    "China Mainland": "https://grih.gree.com",
    "East South Asia": "https://hkgrih.gree.com",
    "Europe": "https://eugrih.gree.com",
    "India": "https://ingrih.gree.com",
    "Latin American": "https://lagrih.gree.com",
    "Middle East": "https://megrih.gree.com",
    "North American": "https://nagrih.gree.com",
    "Russia": "https://rugrih.gree.com",
    "South American": "https://sagrih.gree.com",
}

_APP_ID = "4920681951525131286"
_APP_HASH = "0fa513124aa97781d1f3f40d61ca1a89"
_API_AES_KEY = b"#G$&^jgfujy6ujxt"


def _md5(value: str) -> str:
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def _encrypt_request(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    padding = AES.block_size - len(raw) % AES.block_size
    encrypted = AES.new(_API_AES_KEY, AES.MODE_ECB).encrypt(
        raw + bytes([padding]) * padding
    )
    return base64.b64encode(encrypted).decode()


def _decrypt_response(value: str) -> dict[str, Any]:
    raw = AES.new(_API_AES_KEY, AES.MODE_ECB).decrypt(base64.b64decode(value))
    padding = raw[-1]
    if (
        padding < 1
        or padding > AES.block_size
        or raw[-padding:] != bytes([padding]) * padding
    ):
        raise ValueError("Invalid Gree cloud response padding")
    return json.loads(raw[:-padding])


class GreeCloudApi:
    """Small client for the Gree+ endpoints needed by this integration."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.user_id: int | None = None
        self.token: str | None = None
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10)
        )

    @classmethod
    def for_server(
        cls, server: str, username: str, password: str
    ) -> GreeCloudApi:
        try:
            base_url = GREE_CLOUD_SERVERS[server]
        except KeyError as error:
            raise ValueError(f"Unsupported Gree cloud region: {server}") from error
        return cls(base_url, username, password)

    def _body(
        self, payload: dict[str, Any], signed_fields: list[str], now: datetime
    ) -> dict[str, Any]:
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        nonce = int(now.timestamp())
        signature = _md5(f"{_APP_ID}_{_APP_HASH}_{timestamp}_{nonce}")
        signed_values = "_".join(str(payload[field]) for field in signed_fields)
        return {
            "api": {"appId": _APP_ID, "r": nonce, "t": timestamp, "vc": signature},
            "datVc": _md5(f"{_APP_HASH}_{signed_values}"),
            **payload,
        }

    async def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Charset": "utf-8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Gaen1": "5ac2bdf935bcca70",
        }
        async with self._session.post(
            f"{self.base_url}{endpoint}", data=_encrypt_request(body), headers=headers
        ) as response:
            response.raise_for_status()
            envelope = await response.json()
        if "enRes" not in envelope:
            raise ValueError("Gree cloud response did not contain encrypted data")
        result = _decrypt_response(envelope["enRes"])
        if result.get("r", 200) != 200:
            raise ValueError(result.get("msg") or "Gree cloud request failed")
        return result

    async def login(self) -> CloudCredentials:
        now = datetime.now(UTC)
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        password_stage = _md5(_md5(self.password) + self.password)
        payload = {
            "psw": _md5(password_stage + timestamp),
            "t": timestamp,
            "user": self.username,
        }
        result = await self._post(
            "/App/UserLoginV2", self._body(payload, ["user", "psw", "t"], now)
        )
        data = result.get("data", result)
        self.user_id = int(data["uid"])
        self.token = str(data["token"])
        return CloudCredentials(self.user_id, self.token)

    def _authenticated_body(
        self, extra: dict[str, Any], signed_fields: list[str]
    ) -> dict[str, Any]:
        if self.user_id is None or self.token is None:
            raise RuntimeError("Gree cloud login is required")
        now = datetime.now(UTC)
        payload = {"token": self.token, **extra, "uid": self.user_id}
        return self._body(payload, signed_fields, now)

    async def get_all_devices(self) -> list[CloudDeviceInfo]:
        homes_result = await self._post(
            "/App/GetHomes", self._authenticated_body({}, ["token", "uid"])
        )
        devices: list[CloudDeviceInfo] = []
        for home in homes_result.get("home", []):
            result = await self._post(
                "/App/GetDevsInRoomsOfHomeV2",
                self._authenticated_body(
                    {"homeId": home["id"]}, ["token", "uid", "homeId"]
                ),
            )
            for room in result.get("rooms", []):
                for raw in room.get("devs", []):
                    devices.append(
                        CloudDeviceInfo(
                            name=str(raw.get("name") or raw["mac"]).strip(),
                            mac=str(raw["mac"]).strip().lower(),
                            key=str(raw["key"]).strip(),
                            model=raw.get("model"),
                            version=raw.get("ver"),
                            online=bool(raw.get("online", 1)),
                        )
                    )
        return self._deduplicate(devices)

    @staticmethod
    def _deduplicate(devices: list[CloudDeviceInfo]) -> list[CloudDeviceInfo]:
        by_key: defaultdict[str, list[CloudDeviceInfo]] = defaultdict(list)
        for device in devices:
            by_key[device.key].append(device)
        result: list[CloudDeviceInfo] = []
        for group in by_key.values():
            preferred = [item for item in group if len(item.mac) > 12 and item.mac.endswith("00")]
            result.extend(preferred or group)
        return result

    async def close(self) -> None:
        await self._session.close()
