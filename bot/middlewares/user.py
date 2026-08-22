"""Регистрация пользователя и проверка бана."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update, User as TgUser

from bot.db import repo
from bot.texts import ru

log = logging.getLogger(__name__)


def _inner_event(event: TelegramObject) -> TelegramObject:
    """В outer-middleware на dp.update приходит Update — достаём из него само событие."""
    if isinstance(event, Update):
        return event.message or event.callback_query or event.pre_checkout_query or event
    return event


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: Optional[TgUser] = data.get("event_from_user")
        session = data.get("session")
        if tg_user is None or tg_user.is_bot or session is None:
            return await handler(event, data)

        inner = _inner_event(event)
        user, is_new = await repo.get_or_create_user(
            session,
            user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
        )

        if user.is_banned:
            if isinstance(inner, Message):
                await inner.answer(ru.BANNED)
            elif isinstance(inner, CallbackQuery):
                await inner.answer(ru.BANNED, show_alert=True)
            return None

        data["user"] = user
        data["is_new_user"] = is_new
        return await handler(event, data)
