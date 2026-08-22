"""Простейший антифлуд: не чаще N событий в секунду на пользователя."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.4) -> None:
        self.rate = rate
        self._last: Dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        now = time.monotonic()
        if now - self._last[tg_user.id] < self.rate:
            if isinstance(event, CallbackQuery):
                await event.answer("Не так быстро 🙂")
            return None
        self._last[tg_user.id] = now
        return await handler(event, data)
