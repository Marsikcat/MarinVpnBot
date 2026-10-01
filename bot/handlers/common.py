"""Старт, меню, справка, поддержка."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from bot.db.models import User
from bot.keyboards import inline as ikb
from bot.keyboards import reply as rkb
from bot.texts import ru
from bot.utils.tg import edit_view

log = logging.getLogger(__name__)
router = Router(name="common")

# Отдельный роутер: подключается раньше всех, чтобы /cancel работал
# из любого состояния FSM, включая редакторы админки.
cancel_router = Router(name="cancel")


@cancel_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=rkb.main_menu(show_trial=not user.trial_used))


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        ru.START.format(name=message.from_user.first_name or "друг"),
        reply_markup=rkb.main_menu(show_trial=not user.trial_used),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=rkb.main_menu(show_trial=not user.trial_used))


@router.message(Command("help"))
@router.message(F.text == rkb.BTN_HELP)
async def show_help(message: Message) -> None:
    await message.answer(ru.HELP, reply_markup=ikb.app_links_kb())


@router.message(F.text == rkb.BTN_SUPPORT)
async def show_support(message: Message) -> None:
    await message.answer(
        "🆘 <b>Поддержка</b>\n\nОпишите проблему — ответим в течение дня.",
        reply_markup=ikb.support_kb(),
    )


@router.callback_query(ikb.MenuCB.filter(F.action == "help"))
async def cb_help(callback: CallbackQuery) -> None:
    await edit_view(callback, ru.HELP, ikb.app_links_kb())
    await callback.answer()


@router.callback_query(ikb.MenuCB.filter(F.action == "apps"))
async def cb_apps(callback: CallbackQuery) -> None:
    await edit_view(callback, ru.APPS, ikb.app_links_kb())
    await callback.answer()


@router.callback_query(ikb.MenuCB.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, user: User) -> None:
    await callback.message.answer(
        "Главное меню:", reply_markup=rkb.main_menu(show_trial=not user.trial_used)
    )
    await callback.answer()


@router.callback_query(ikb.AdminCB.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


async def on_error(event: ErrorEvent) -> bool:
    """Непойманная ошибка в любом обработчике: пишем в лог и отвечаем пользователю.

    Без этого кнопка просто «висела»: Telegram ждал ответа, а пользователь не видел ничего.
    """
    update = event.update
    log.error("Ошибка при обработке апдейта %s", update.update_id, exc_info=event.exception)
    try:
        if update.callback_query:
            await update.callback_query.answer(ru.ERROR_GENERIC, show_alert=True)
        elif update.message:
            await update.message.answer(ru.ERROR_GENERIC)
    except TelegramAPIError:
        pass  # на запрос уже ответили или чат недоступен — лог уже есть
    return True


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>")


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(
        "📄 <b>Условия использования</b>\n\n"
        "• Подписка активируется сразу после оплаты и действует выбранный срок.\n"
        "• Сервис не ведёт логи посещаемых сайтов.\n"
        "• Запрещены спам, DDoS, торренты и любая незаконная активность — "
        "такие аккаунты блокируются без возврата средств.\n"
        "• Возврат возможен в течение 24 часов после оплаты, если сервис не удалось запустить "
        "и поддержка не смогла помочь."
    )
