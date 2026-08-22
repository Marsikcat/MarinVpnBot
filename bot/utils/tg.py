"""Мелкие помощники для работы с сообщениями Telegram."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

log = logging.getLogger(__name__)


async def edit_view(
    callback: CallbackQuery, text: str, markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """Меняет содержимое сообщения независимо от того, текст это или фото с подписью.

    Сообщение с QR-кодом — это фото, у него правится caption, а не text.
    """
    message = callback.message
    if message is None:
        return
    try:
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        # редактирование невозможно (например, сообщение слишком старое) — шлём новое
        log.debug("Не удалось отредактировать сообщение: %s", exc)
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
