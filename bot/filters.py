"""Общие фильтры для роутеров."""
from __future__ import annotations

from typing import Union

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot.config import settings


class IsAdmin(BaseFilter):
    """Пропускает только пользователей из ADMIN_IDS."""

    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = event.from_user
        return bool(user and settings.is_admin(user.id))
