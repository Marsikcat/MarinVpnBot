"""Клиент панели Marzban (https://github.com/Gozargah/Marzban)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

from bot.config import settings
from bot.services.vpn.base import VpnAccount, VpnPanel, VpnPanelError

log = logging.getLogger(__name__)


def _to_ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _from_ts(ts: Optional[int]) -> Optional[datetime]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


class MarzbanPanel(VpnPanel):
    name = "marzban"

    def __init__(self) -> None:
        self.base_url = settings.marzban_url.rstrip("/")
        self.username = settings.marzban_username
        self.password = settings.marzban_password
        self.sub_domain = (settings.marzban_sub_domain or self.base_url).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ infra
    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(ssl=False),
            )
        return self._session

    async def _auth(self, force: bool = False) -> str:
        async with self._lock:
            if self._token and not force:
                return self._token
            session = await self._http()
            data = {"username": self.username, "password": self.password}
            async with session.post(f"{self.base_url}/api/admin/token", data=data) as resp:
                if resp.status != 200:
                    raise VpnPanelError(f"Marzban: авторизация не удалась ({resp.status})")
                payload = await resp.json()
            self._token = payload.get("access_token")
            if not self._token:
                raise VpnPanelError("Marzban: в ответе нет access_token")
            return self._token

    async def _request(
        self, method: str, path: str, *, json: Any = None, retry_auth: bool = True
    ) -> Optional[Dict[str, Any]]:
        token = await self._auth()
        session = await self._http()
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.request(method, url, json=json, headers=headers) as resp:
                if resp.status == 401 and retry_auth:
                    await self._auth(force=True)
                    return await self._request(method, path, json=json, retry_auth=False)
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    body = await resp.text()
                    raise VpnPanelError(f"Marzban {method} {path} -> {resp.status}: {body[:300]}")
                if resp.status == 204 or not resp.content_length:
                    text = await resp.text()
                    if not text.strip():
                        return {}
                    return await resp.json(content_type=None)
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise VpnPanelError(f"Marzban: сеть недоступна ({exc})") from exc

    # ------------------------------------------------------------ mapping
    def _proxies_payload(self) -> Dict[str, Dict[str, Any]]:
        return {proto: {} for proto in settings.marzban_proxies}

    def _inbounds_payload(self) -> Dict[str, list]:
        tags = settings.marzban_inbounds
        if not tags:
            return {}
        return {proto: list(tags) for proto in settings.marzban_proxies}

    def _to_account(self, data: Dict[str, Any]) -> VpnAccount:
        sub = data.get("subscription_url") or ""
        if sub and sub.startswith("/"):
            sub = f"{self.sub_domain}{sub}"
        return VpnAccount(
            username=data["username"],
            uuid=(data.get("proxies", {}).get("vless", {}) or {}).get("id"),
            subscription_url=sub or None,
            links=list(data.get("links") or []),
            expires_at=_from_ts(data.get("expire")),
            data_limit=int(data.get("data_limit") or 0),
            used_traffic=int(data.get("used_traffic") or 0),
            enabled=data.get("status") == "active",
        )

    # ------------------------------------------------------------ api
    async def create_or_update(
        self, username: str, expires_at: datetime, data_limit: int = 0, note: str = ""
    ) -> VpnAccount:
        payload: Dict[str, Any] = {
            "username": username,
            "expire": _to_ts(expires_at),
            "data_limit": int(data_limit),
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": note,
        }
        inbounds = self._inbounds_payload()
        if inbounds:
            payload["inbounds"] = inbounds

        existing = await self._request("GET", f"/api/user/{username}")
        if existing is None:
            payload["proxies"] = self._proxies_payload()
            data = await self._request("POST", "/api/user", json=payload)
        else:
            data = await self._request("PUT", f"/api/user/{username}", json=payload)
        if not data:
            raise VpnPanelError("Marzban: пустой ответ при создании пользователя")
        return self._to_account(data)

    async def get(self, username: str) -> Optional[VpnAccount]:
        data = await self._request("GET", f"/api/user/{username}")
        return self._to_account(data) if data else None

    async def set_enabled(self, username: str, enabled: bool) -> None:
        await self._request(
            "PUT", f"/api/user/{username}", json={"status": "active" if enabled else "disabled"}
        )

    async def delete(self, username: str) -> None:
        await self._request("DELETE", f"/api/user/{username}")

    async def reset_traffic(self, username: str) -> None:
        await self._request("POST", f"/api/user/{username}/reset")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
