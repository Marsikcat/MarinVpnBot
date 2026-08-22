"""Отправка пользователю всего, что нужно для подключения.

Одна точка на все случаи: покупка, продление, пробный период, раздел «Мой доступ».
Пользователь получает QR-код ссылки-подписки, саму ссылку, срок, остаток трафика
и короткую инструкцию — без дополнительных нажатий.
"""
from __future__ import annotations

import logging
from typing import Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from bot.db.models import Subscription
from bot.keyboards import inline as ikb
from bot.services.subscriptions import parse_links
from bot.utils.qr import make_qr
from bot.utils.render import render_access

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024


def _markup(subscription: Optional[Subscription]):
    if subscription is None or not subscription.is_active:
        return None
    return ikb.access_kb(subscription.subscription_url, has_keys=bool(parse_links(subscription)))


async def send_access(
    target: Union[Bot, Message],
    subscription: Optional[Subscription],
    header: str = "",
    chat_id: Optional[int] = None,
) -> None:
    """Отправляет пакет подключения.

    `target` — либо Bot (тогда нужен chat_id), либо Message, на который отвечаем.
    `header` — что показать перед данными доступа, например подтверждение оплаты.
    """
    text = f"{header}{render_access(subscription)}"
    markup = _markup(subscription)

    qr = None
    if subscription is not None and subscription.is_active and subscription.subscription_url:
        qr = make_qr(subscription.subscription_url)

    async def _photo(caption: str, reply_markup) -> None:
        if isinstance(target, Message):
            await target.answer_photo(photo=qr, caption=caption, reply_markup=reply_markup)
        else:
            await target.send_photo(chat_id, photo=qr, caption=caption, reply_markup=reply_markup)

    async def _text(body: str, reply_markup) -> None:
        if isinstance(target, Message):
            await target.answer(body, reply_markup=reply_markup, disable_web_page_preview=True)
        else:
            await target.send_message(
                chat_id, body, reply_markup=reply_markup, disable_web_page_preview=True
            )

    if qr is not None:
        try:
            if len(text) <= CAPTION_LIMIT:
                await _photo(text, markup)
            else:
                # длинный текст в подпись не влезает — картинка отдельно, детали следом
                await _photo(header.strip() or "🔑 Ваш доступ", None)
                await _text(render_access(subscription), markup)
            return
        except TelegramAPIError as exc:
            log.warning("QR-код не отправлен: %s", exc)

    try:
        await _text(text, markup)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить данные доступа: %s", exc)
