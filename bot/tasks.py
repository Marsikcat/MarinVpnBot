"""Фоновые задачи: напоминания, отключение просрочки, синхронизация, добивка платежей."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db import repo
from bot.db.models import PaymentStatus
from bot.keyboards import inline as ikb
from bot.services import subscriptions
from bot.services.payments.base import PaymentError
from bot.services.payments.registry import get_provider
from bot.services.vpn.base import VpnPanelError
from bot.texts import ru
from bot.utils.render import time_left

log = logging.getLogger(__name__)


async def notify_expiring(bot: Bot, session_factory: async_sessionmaker) -> None:
    """Напоминает за 3 дня и за сутки до окончания подписки."""
    async with session_factory() as session:
        plans = await repo.list_plans(session)
        markup = ikb.plans_kb(plans)

        for days, flag in ((3, "notified_3d"), (1, "notified_1d")):
            for subscription in await repo.subscriptions_expiring_within(session, days):
                if getattr(subscription, flag):
                    continue
                if days == 3 and subscription.seconds_left <= 86400:
                    continue  # такому пользователю уйдёт напоминание за сутки
                try:
                    await bot.send_message(
                        subscription.user_id,
                        ru.EXPIRE_SOON.format(
                            left=time_left(subscription).replace("осталось ", ""),
                            until=f"{subscription.expires_at:%d.%m.%Y}",
                        ),
                        reply_markup=markup,
                    )
                except TelegramForbiddenError:
                    pass
                except TelegramAPIError as exc:
                    log.warning("Напоминание %s не доставлено: %s", subscription.user_id, exc)
                setattr(subscription, flag, True)
                await asyncio.sleep(0.05)
        await session.commit()


async def deactivate_expired(bot: Bot, session_factory: async_sessionmaker) -> None:
    """Отключает доступ в панели, когда срок вышел."""
    async with session_factory() as session:
        plans = await repo.list_plans(session)
        markup = ikb.plans_kb(plans)

        for subscription in await repo.subscriptions_expired(session):
            try:
                await subscriptions.disable(session, subscription)
            except VpnPanelError as exc:
                log.error("Не удалось отключить %s: %s", subscription.vpn_username, exc)
                continue
            if not subscription.notified_expired:
                try:
                    await bot.send_message(subscription.user_id, ru.EXPIRED, reply_markup=markup)
                except TelegramForbiddenError:
                    pass
                except TelegramAPIError as exc:
                    log.warning("Уведомление об окончании %s: %s", subscription.user_id, exc)
                subscription.notified_expired = True
            await asyncio.sleep(0.05)
        await session.commit()


async def sync_with_panel(session_factory: async_sessionmaker) -> None:
    """Сверяет подписки с панелью: трафик, срок и включённость."""
    async with session_factory() as session:
        for subscription in await repo.active_subscriptions(session):
            try:
                await subscriptions.sync_usage(session, subscription)
            except VpnPanelError as exc:
                log.warning("Синхронизация %s: %s", subscription.vpn_username, exc)
                return
            await asyncio.sleep(0.05)


async def poll_pending_payments(bot: Bot, session_factory: async_sessionmaker) -> None:
    """Подхватывает оплаты, о которых не пришёл вебхук."""
    async with session_factory() as session:
        for provider_code in ("yookassa", "cryptobot"):
            provider = get_provider(provider_code)
            if provider is None:
                continue
            for payment in await repo.pending_payments(session, provider_code, older_than_minutes=2):
                if not payment.external_id:
                    continue
                try:
                    status = await provider.check(payment.external_id)
                except PaymentError as exc:
                    log.warning("Опрос платежа %s: %s", payment.id, exc)
                    continue
                if status == "paid":
                    try:
                        await subscriptions.complete_payment(session, bot, payment)
                        log.info("Платёж %s подтверждён опросом", payment.id)
                    except VpnPanelError as exc:
                        log.error("Панель недоступна для платежа %s: %s", payment.id, exc)
                elif status == "canceled":
                    payment.status = PaymentStatus.canceled
                    await session.commit()
                await asyncio.sleep(0.1)


async def retry_stars_activations(bot: Bot, session_factory: async_sessionmaker) -> None:
    """Дожимает оплаты звёздами, по которым не удалось выдать доступ (панель была недоступна).

    У таких платежей уже есть charge_id от Telegram, но статус остался pending.
    """
    async with session_factory() as session:
        for payment in await repo.pending_payments(session, "stars"):
            if not payment.external_id:
                continue  # счёт выставлен, но не оплачен
            try:
                await subscriptions.complete_payment(session, bot, payment)
                log.info("Платёж звёздами %s активирован повторной попыткой", payment.id)
            except VpnPanelError as exc:
                log.warning("Повтор активации %s не удался: %s", payment.id, exc)
            await asyncio.sleep(0.1)


async def cleanup_payments(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        closed = await repo.expire_stale_payments(session, hours=24)
        if closed:
            log.info("Просрочено счетов: %s", closed)


def setup_scheduler(bot: Bot, session_factory: async_sessionmaker, timezone: str) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        notify_expiring, "interval", hours=6, args=(bot, session_factory), id="notify_expiring"
    )
    scheduler.add_job(
        deactivate_expired, "interval", minutes=30, args=(bot, session_factory), id="deactivate_expired"
    )
    scheduler.add_job(
        sync_with_panel, "interval", hours=1, args=(session_factory,), id="sync_with_panel"
    )
    scheduler.add_job(
        poll_pending_payments, "interval", minutes=3, args=(bot, session_factory), id="poll_payments"
    )
    scheduler.add_job(
        retry_stars_activations, "interval", minutes=10, args=(bot, session_factory), id="retry_stars"
    )
    scheduler.add_job(cleanup_payments, "cron", hour=4, args=(session_factory,), id="cleanup_payments")
    return scheduler
