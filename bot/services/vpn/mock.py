"""Заглушка панели: позволяет крутить бота локально без реального сервера."""
from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Dict, Optional

from bot.services.vpn.base import VpnAccount, VpnPanel


class MockPanel(VpnPanel):
    name = "mock"

    def __init__(self) -> None:
        self._store: Dict[str, VpnAccount] = {}

    async def create_or_update(
        self, username: str, expires_at: datetime, data_limit: int = 0, note: str = ""
    ) -> VpnAccount:
        account = self._store.get(username)
        if account is None:
            client_uuid = str(uuid_lib.uuid4())
            account = VpnAccount(
                username=username,
                uuid=client_uuid,
                subscription_url=f"https://demo.example.com/sub/{client_uuid}",
                links=[
                    f"vless://{client_uuid}@demo.example.com:443"
                    f"?type=tcp&security=reality&sni=www.google.com&fp=chrome#DemoVPN-{username}"
                ],
            )
            self._store[username] = account
        account.expires_at = expires_at
        account.data_limit = data_limit
        account.enabled = True
        return account

    async def get(self, username: str) -> Optional[VpnAccount]:
        return self._store.get(username)

    async def set_enabled(self, username: str, enabled: bool) -> None:
        account = self._store.get(username)
        if account:
            account.enabled = enabled

    async def delete(self, username: str) -> None:
        self._store.pop(username, None)

    async def reset_traffic(self, username: str) -> None:
        account = self._store.get(username)
        if account:
            account.used_traffic = 0
