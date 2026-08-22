"""Нижнее меню бота."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from bot.services.runtime import runtime

BTN_ACCESS = "🔑 Мой доступ"
BTN_BUY = "💳 Купить подписку"
BTN_PROFILE = "👤 Профиль"
BTN_TRIAL = "🎁 Пробный период"
BTN_HELP = "📖 Инструкция"
BTN_SUPPORT = "🆘 Поддержка"


def main_menu(show_trial: bool = True) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=BTN_ACCESS), KeyboardButton(text=BTN_BUY))
    if show_trial and runtime.trial_enabled:
        builder.row(KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_TRIAL))
    else:
        builder.row(KeyboardButton(text=BTN_PROFILE))
    builder.row(KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_SUPPORT))
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Выберите действие")
