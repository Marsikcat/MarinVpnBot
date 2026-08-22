"""Проверка подписки на канал (опциональное условие для триала)."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.config import settings

log = logging.getLogger(__name__)
_ALLOWED = {"creator", "administrator", "member"}


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    channel = settings.required_channel
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
    except TelegramAPIError as exc:
        log.warning("Не удалось проверить подписку на %s: %s", channel, exc)
        return True  # не блокируем пользователя из-за проблем с API
    return member.status in _ALLOWED
