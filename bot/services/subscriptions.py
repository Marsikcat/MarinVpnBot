"""Выдача, продление и отключение подписок — единая точка правды."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    Payment,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)
from bot.services.referrals import reward_referrer
from bot.services.runtime import runtime
from bot.texts import ru
from bot.services.vpn.base import VpnPanelError
from bot.services.vpn.factory import get_panel

log = logging.getLogger(__name__)


def vpn_username(user_id: int) -> str:
    return f"tg{user_id}"


def parse_links(subscription: Optional[Subscription]) -> List[str]:
    if not subscription or not subscription.links:
        return []
    try:
        return json.loads(subscription.links)
    except json.JSONDecodeError:
        return []


async def issue_or_extend(
    session: AsyncSession,
    user: User,
    days: int,
    plan: Optional[Plan] = None,
    is_trial: bool = False,
    traffic_gb: Optional[int] = None,
) -> Subscription:
    """Создаёт подписку или продлевает существующую и синхронизирует панель."""
    now = utcnow()
    subscription = await repo.get_subscription(session, user.id)
    panel = get_panel()

    # отсчитываем от того, что реально в панели: срок могли продлить или урезать там руками
    base = now
    if subscription and subscription.expires_at > now:
        base = subscription.expires_at
    try:
        existing = await panel.get(vpn_username(user.id))
    except VpnPanelError as exc:
        log.warning("Панель не ответила перед выдачей %s: %s", user.id, exc)
        existing = None
    if existing and existing.expires_at and existing.expires_at > base:
        log.info("Беру срок из панели для %s: %s", user.id, existing.expires_at)
        base = existing.expires_at

    expires_at = base + timedelta(days=days)

    if traffic_gb is None:
        traffic_gb = plan.traffic_gb if plan else 0
    traffic_limit = int(traffic_gb) * 1024 ** 3

    account = await panel.create_or_update(
        username=vpn_username(user.id),
        expires_at=expires_at,
        data_limit=traffic_limit,
        note=f"tg:{user.id} {user.title}",
    )

    if subscription is None:
        subscription = Subscription(
            user_id=user.id,
            vpn_username=account.username,
            started_at=now,
        )
        session.add(subscription)

    subscription.plan_id = plan.id if plan else subscription.plan_id
    subscription.status = SubscriptionStatus.active
    subscription.expires_at = expires_at
    subscription.traffic_limit = traffic_limit
    subscription.vpn_uuid = account.uuid or subscription.vpn_uuid
    subscription.subscription_url = account.subscription_url or subscription.subscription_url
    if account.links:
        subscription.links = json.dumps(account.links, ensure_ascii=False)
    subscription.is_trial = is_trial  # платное продление снимает пометку триала
    subscription.notified_3d = False
    subscription.notified_1d = False
    subscription.notified_expired = False

    if is_trial:
        user.trial_used = True

    await session.commit()
    await session.refresh(subscription)
    log.info("Подписка пользователя %s активна до %s", user.id, expires_at)
    return subscription


async def disable(session: AsyncSession, subscription: Subscription) -> None:
    """Отключает доступ в панели и помечает подписку истёкшей."""
    try:
        await get_panel().set_enabled(subscription.vpn_username, False)
    except VpnPanelError as exc:
        log.error("Не удалось отключить %s в панели: %s", subscription.vpn_username, exc)
    subscription.status = SubscriptionStatus.expired
    await session.commit()


async def sync_usage(session: AsyncSession, subscription: Subscription) -> Subscription:
    """Приводит запись в БД в соответствие с панелью.

    Панель — источник правды: если срок или доступ поменяли там руками, бот это увидит
    и покажет пользователю реальное положение дел, а не своё представление о нём.
    """
    try:
        account = await get_panel().get(subscription.vpn_username)
    except VpnPanelError as exc:
        log.warning("Панель недоступна при синхронизации %s: %s", subscription.vpn_username, exc)
        return subscription

    if account is None:
        # клиента удалили из панели — доступа фактически нет, каким бы ни был статус в БД
        if subscription.status != SubscriptionStatus.expired:
            log.warning("Клиент %s исчез из панели — помечаю подписку истёкшей", subscription.vpn_username)
            subscription.status = SubscriptionStatus.expired
            await session.commit()
        return subscription

    subscription.traffic_used = account.used_traffic
    if account.data_limit != subscription.traffic_limit:
        subscription.traffic_limit = account.data_limit
    if account.links:
        subscription.links = json.dumps(account.links, ensure_ascii=False)
    if account.subscription_url:
        subscription.subscription_url = account.subscription_url

    # срок из панели (0/None означает «без ограничения» — тогда доверяем своей дате)
    if account.expires_at and abs((account.expires_at - subscription.expires_at).total_seconds()) > 60:
        log.info(
            "Срок %s в панели отличается: %s -> %s",
            subscription.vpn_username,
            subscription.expires_at,
            account.expires_at,
        )
        subscription.expires_at = account.expires_at

    if not account.enabled:
        subscription.status = SubscriptionStatus.disabled
    elif subscription.expires_at > utcnow():
        subscription.status = SubscriptionStatus.active

    await session.commit()
    return subscription


async def complete_payment(session: AsyncSession, bot: Bot, payment: Payment) -> Optional[Subscription]:
    """Подтверждает платёж и выдаёт подписку. Идемпотентна: повторный вызов ничего не меняет."""
    if payment.status == PaymentStatus.paid:
        return await repo.get_subscription(session, payment.user_id)

    user = await session.get(User, payment.user_id)
    if user is None:
        log.error("Платёж %s без пользователя %s", payment.id, payment.user_id)
        return None

    plan = await session.get(Plan, payment.plan_id) if payment.plan_id else None
    days = payment.days or (plan.days if plan else 0)

    # запоминаем состояние до выдачи, чтобы отличить продление от первой покупки
    previous = await repo.get_subscription(session, user.id)
    was_active = bool(previous and previous.is_active)
    previous_until = previous.expires_at if previous else None

    # Сначала выдаём доступ: если панель недоступна, платёж останется pending
    # и будет повторно обработан фоновой задачей — деньги не «сгорят».
    subscription = await issue_or_extend(session, user, days=days, plan=plan)

    payment.status = PaymentStatus.paid
    payment.paid_at = utcnow()
    await session.commit()

    if payment.promo_code:
        promo = await repo.get_promo(session, payment.promo_code)
        if promo and not await repo.promo_used_by(session, promo.id, user.id):
            await repo.register_promo_use(session, promo, user.id)

    await reward_referrer(session, bot, user, payment)

    plan_title = plan.title if plan else f"{days} дн."
    if was_active and previous_until:
        header = ru.PAYMENT_DONE_RENEW.format(plan=plan_title, was=f"{previous_until:%d.%m.%Y}")
    else:
        header = ru.PAYMENT_DONE_NEW.format(plan=plan_title)

    # импорт локальный: delivery тянет render, который зависит от этого модуля
    from bot.services.delivery import send_access

    try:
        await send_access(bot, subscription, header=header, chat_id=user.id)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить данные доступа %s: %s", user.id, exc)

    return subscription


async def grant_trial(session: AsyncSession, user: User) -> Subscription:
    """Выдаёт пробный период. Проверку trial_used делает вызывающая сторона."""
    return await issue_or_extend(
        session,
        user,
        days=runtime.trial_days,
        is_trial=True,
        traffic_gb=runtime.trial_traffic_gb,
    )
