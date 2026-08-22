"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from bot.config import settings
from bot.db.session import dispose_db, init_db, session_factory
from bot.handlers import build_router
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.middlewares.user import UserMiddleware
from bot.services.payments.registry import close_providers
from bot.services.runtime import runtime
from bot.services.vpn.factory import close_panel, get_panel
from bot.tasks import setup_scheduler
from bot.utils.logging import setup_logging

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="access", description="Мой доступ и ключи"),
    BotCommand(command="buy", description="Купить подписку"),
    BotCommand(command="renew", description="Продлить подписку"),
    BotCommand(command="trial", description="Пробный период"),
    BotCommand(command="profile", description="Профиль и баланс"),
    BotCommand(command="ref", description="Пригласить друга"),
    BotCommand(command="promo", description="Ввести промокод"),
    BotCommand(command="help", description="Как подключиться"),
]

ADMIN_COMMANDS = COMMANDS + [
    BotCommand(command="admin", description="🛠 Админ-панель"),
    BotCommand(command="plans", description="💼 Редактор тарифов"),
    BotCommand(command="settings", description="⚙️ Настройки бота"),
    BotCommand(command="claims", description="🏦 Заявки по СБП"),
    BotCommand(command="stats", description="📊 Статистика"),
]


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.outer_middleware(DbSessionMiddleware(session_factory))
    dp.update.outer_middleware(UserMiddleware())
    dp.message.middleware(ThrottlingMiddleware(rate=0.4))
    dp.callback_query.middleware(ThrottlingMiddleware(rate=0.3))

    dp.include_router(build_router())
    return dp


async def on_startup(bot: Bot) -> None:
    await init_db()
    async with session_factory() as session:
        await runtime.load(session)  # настройки, изменённые через админку
    get_panel()
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
    me = await bot.me()
    log.info("Бот @%s запущен", me.username)
    if not settings.admin_ids:
        log.warning("ADMIN_IDS не заданы — админ-панель и заявки по СБП работать не будут")
    for admin_id in settings.admin_ids:
        try:
            # у админов в меню команд появляются разделы управления
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            await bot.send_message(admin_id, "🤖 Бот запущен и готов к работе.")
        except Exception as exc:  # noqa: BLE001 - админ мог не нажать /start
            # это же и причина, по которой не дойдут заявки по СБП, — сообщаем громко
            log.warning(
                "Администратор %s недоступен (%s). Проверьте ID (команда /id в боте) "
                "и то, что он нажал /start.",
                admin_id,
                exc,
            )


async def main() -> None:
    setup_logging()
    if not settings.bot_token:
        raise SystemExit("Не задан BOT_TOKEN — скопируйте .env.example в .env и заполните его")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = create_dispatcher()

    await on_startup(bot)

    scheduler = setup_scheduler(bot, session_factory, settings.tz)
    scheduler.start()

    runner = None
    if settings.webhook_enabled:
        from bot.webserver import start_webserver

        runner = await start_webserver(bot, session_factory)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаюсь…")
        scheduler.shutdown(wait=False)
        if runner is not None:
            await runner.cleanup()
        await close_panel()
        await close_providers()
        await dispose_db()
        await bot.session.close()


def run() -> None:
    """Синхронная обёртка — её вызывают `python -m bot` и `python -m bot.main`."""
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        logging.getLogger(__name__).info("Бот остановлен")


if __name__ == "__main__":
    run()
