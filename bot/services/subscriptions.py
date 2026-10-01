"""Выдача, продление и отключение подписок — единая точка правды."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Dict, List, Optional

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
from bot.services.runtime import runtime
from bot.texts import ru
from bot.services.vpn.base import VpnAccount, VpnPanelError
from bot.services.vpn.factory import get_panel

log = logging.getLogger(__name__)

_locks: Dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    """Очередь на изменения подписки одного пользователя.

    Обработчики Telegram, вебхуки и фоновые задачи работают в одном цикле событий
    параллельно. Без очереди два одновременных подтверждения платежа прибавили бы дни
    дважды, а «Обновить данные» или отключение просрочки могли бы перезаписать только
    что продлённый срок старым.
    """
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


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
    reset_traffic: bool = False,
) -> Subscription:
    """Создаёт подписку или продлевает существующую и синхронизирует панель.

    Дни прибавляются к остатку: отсчёт идёт от самой поздней даты — в БД или в панели,
    а если обе в прошлом, то от текущего момента. `reset_traffic` обнуляет расход,
    чтобы оплаченный период начинался с полного лимита трафика.
    """
    async with user_lock(user.id):
        return await _issue_or_extend(
            session, user, days, plan=plan, is_trial=is_trial,
            traffic_gb=traffic_gb, reset_traffic=reset_traffic,
        )


async def _issue_or_extend(
    session: AsyncSession,
    user: User,
    days: int,
    plan: Optional[Plan] = None,
    is_trial: bool = False,
    traffic_gb: Optional[int] = None,
    reset_traffic: bool = False,
) -> Subscription:
    """Тело issue_or_extend. Вызывать только внутри user_lock(user.id)."""
    now = utcnow()
    subscription = await repo.get_subscription(session, user.id)
    if subscription is not None:
        # объект мог загрузиться до очереди — берём то, что записал предыдущий в ней
        await session.refresh(subscription)
    panel = get_panel()
    username = vpn_username(user.id)

    # Срок могли продлить или урезать в панели руками, поэтому без её ответа не продлеваем:
    # отсчёт от устаревшей даты в БД съел бы оставшиеся дни. Ошибка уходит наверх —
    # платёж остаётся pending и будет проведён повторно.
    existing = await panel.get(username)

    db_until = subscription.expires_at if subscription else None
    panel_until = existing.expires_at if existing else None
    base = max(moment for moment in (now, db_until, panel_until) if moment is not None)
    expires_at = base + timedelta(days=days)
    log.info(
        "Срок %s: в БД %s, в панели %s -> от %s +%s дн. = %s",
        username, db_until, panel_until, base, days, expires_at,
    )

    if traffic_gb is None:
        traffic_gb = plan.traffic_gb if plan else 0
    traffic_limit = int(traffic_gb) * 1024 ** 3

    if reset_traffic and existing is not None:
        # Сбрасываем до продления: если продление сорвётся, повторная обработка платежа
        # сбросит трафик ещё раз, но срок не прибавится дважды.
        await panel.reset_traffic(username)

    account = await panel.create_or_update(
        username=username,
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
    if subscription.expires_at != expires_at:
        # новый срок — напоминания об окончании пойдут заново; «Обновить данные»
        # срок не меняет, и напоминать повторно незачем
        subscription.notified_3d = False
        subscription.notified_1d = False
        subscription.notified_expired = False
    subscription.expires_at = expires_at
    subscription.traffic_limit = traffic_limit
    if reset_traffic:
        subscription.traffic_used = 0
    subscription.vpn_uuid = account.uuid or subscription.vpn_uuid
    subscription.subscription_url = account.subscription_url or subscription.subscription_url
    if account.links:
        subscription.links = json.dumps(account.links, ensure_ascii=False)
    subscription.is_trial = is_trial  # платное продление снимает пометку триала

    if is_trial:
        user.trial_used = True

    await session.commit()
    await session.refresh(subscription)
    log.info("Подписка пользователя %s активна до %s", user.id, expires_at)
    return subscription


async def disable(session: AsyncSession, subscription: Subscription) -> None:
    """Отключает доступ в панели и помечает подписку истёкшей."""
    async with user_lock(subscription.user_id):
        try:
            await get_panel().set_enabled(subscription.vpn_username, False)
        except VpnPanelError as exc:
            log.error("Не удалось отключить %s в панели: %s", subscription.vpn_username, exc)
        subscription.status = SubscriptionStatus.expired
        await session.commit()


async def restore(session: AsyncSession, subscription: Subscription) -> None:
    """Включает доступ обратно (например, после разбана). Срок не трогает."""
    async with user_lock(subscription.user_id):
        await get_panel().set_enabled(subscription.vpn_username, True)
        subscription.status = SubscriptionStatus.active
        await session.commit()


async def expire_if_due(session: AsyncSession, subscription: Subscription) -> bool:
    """Отключает доступ, если срок действительно вышел. True — подписка истекла сейчас.

    Перед отключением сверяется с панелью: срок могли продлить там руками или оплатить
    продление прямо в эту минуту — такого клиента отключать нельзя. Ошибку панели
    пробрасывает: отключать вслепую незачем, панель сама не пускает клиентов
    с истёкшим сроком, а задача повторится.
    """
    async with user_lock(subscription.user_id):
        await session.refresh(subscription)
        if subscription.status != SubscriptionStatus.active or subscription.expires_at > utcnow():
            return False
        account = await get_panel().get(subscription.vpn_username)
        apply_account(subscription, account)
        if subscription.is_active:
            await session.commit()
            return False
        if account is not None and account.enabled:
            await get_panel().set_enabled(subscription.vpn_username, False)
        subscription.status = SubscriptionStatus.expired
        await session.commit()
        return True


async def sync_usage(session: AsyncSession, subscription: Subscription) -> Subscription:
    """Приводит запись в БД в соответствие с панелью.

    Панель — источник правды: если срок или доступ поменяли там руками, бот это увидит
    и покажет пользователю реальное положение дел, а не своё представление о нём.
    """
    async with user_lock(subscription.user_id):
        try:
            account = await get_panel().get(subscription.vpn_username)
        except VpnPanelError as exc:
            log.warning("Панель недоступна при синхронизации %s: %s", subscription.vpn_username, exc)
            return subscription
        apply_account(subscription, account)
        await session.commit()
    return subscription


def apply_account(subscription: Subscription, account: Optional[VpnAccount]) -> None:
    """Переносит в запись БД то, что сейчас в панели. Коммит — за вызывающим."""
    if account is None:
        # клиента удалили из панели — доступа фактически нет, каким бы ни был статус в БД
        if subscription.status != SubscriptionStatus.expired:
            log.warning("Клиент %s исчез из панели — помечаю подписку истёкшей", subscription.vpn_username)
            subscription.status = SubscriptionStatus.expired
        return

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

    now = utcnow()
    if not account.enabled:
        # Истёкших клиентов 3x-ui отключает сам — это конец срока, а не «приостановка»:
        # такую подписку оставляем deactivate_expired, она пометит её истёкшей и
        # предупредит клиента. Приостановка — когда отключили при оставшемся сроке.
        if subscription.expires_at > now:
            subscription.status = SubscriptionStatus.disabled
    elif subscription.expires_at > now:
        subscription.status = SubscriptionStatus.active


async def complete_payment(session: AsyncSession, bot: Bot, payment: Payment) -> Optional[Subscription]:
    """Подтверждает платёж и выдаёт подписку. Идемпотентна: повторный вызов ничего не меняет."""
    async with user_lock(payment.user_id):
        # Тот же платёж могли провести параллельно (вебхук и опрос, два нажатия админа):
        # перечитываем статус уже в очереди, в свежей транзакции.
        await session.commit()
        await session.refresh(payment)
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
        if previous is not None:
            await session.refresh(previous)
        previous_until = previous.expires_at if previous else None
        was_running = bool(previous_until and previous_until > utcnow())

        # Сначала выдаём доступ: если панель недоступна, платёж останется pending
        # и будет повторно обработан фоновой задачей — деньги не «сгорят».
        # Оплаченный период начинается с полного лимита — израсходованное обнуляем.
        subscription = await _issue_or_extend(session, user, days=days, plan=plan, reset_traffic=True)

        payment.status = PaymentStatus.paid
        payment.paid_at = utcnow()
        await session.commit()

    plan_title = plan.title if plan else f"{days} дн."
    if was_running:
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


async def grant_trial(session: AsyncSession, user: User) -> Optional[Subscription]:
    """Выдаёт пробный период. None — если он уже был использован."""
    async with user_lock(user.id):
        # проверка в очереди: два быстрых нажатия «Пробный период» не выдадут его дважды
        await session.refresh(user)
        if user.trial_used:
            return None
        return await _issue_or_extend(
            session,
            user,
            days=runtime.trial_days,
            is_trial=True,
            traffic_gb=runtime.trial_traffic_gb,
        )
