"""Общий интерфейс для панелей управления VPN."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence


class VpnPanelError(RuntimeError):
    """Ошибка при обращении к панели (сеть, авторизация, некорректный ответ)."""


@dataclass(slots=True)
class VpnAccount:
    username: str
    uuid: Optional[str] = None
    subscription_url: Optional[str] = None
    links: List[str] = field(default_factory=list)
    expires_at: Optional[datetime] = None
    data_limit: int = 0
    used_traffic: int = 0
    enabled: bool = True


class VpnPanel(abc.ABC):
    """Каждый метод идемпотентен: повторный вызов не ломает состояние."""

    name: str = "base"

    @abc.abstractmethod
    async def create_or_update(
        self,
        username: str,
        expires_at: datetime,
        data_limit: int = 0,
        note: str = "",
    ) -> VpnAccount:
        """Создаёт пользователя в панели либо продлевает существующего."""

    @abc.abstractmethod
    async def get(self, username: str) -> Optional[VpnAccount]:
        """Возвращает аккаунт или None, если его нет в панели."""

    async def get_many(self, usernames: Sequence[str]) -> Dict[str, Optional[VpnAccount]]:
        """Несколько аккаунтов разом; None — аккаунта нет в панели.

        По умолчанию — запрос на каждого; панели, где это дорого, переопределяют.
        """
        return {username: await self.get(username) for username in usernames}

    @abc.abstractmethod
    async def set_enabled(self, username: str, enabled: bool) -> None:
        """Включает/отключает аккаунт, не удаляя его."""

    @abc.abstractmethod
    async def delete(self, username: str) -> None:
        """Полностью удаляет аккаунт из панели."""

    async def reset_traffic(self, username: str) -> None:  # pragma: no cover - не у всех панелей
        raise NotImplementedError

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
