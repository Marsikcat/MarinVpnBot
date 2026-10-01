"""Раздел «Мой доступ»: ссылка-подписка, QR-код, ключи и инструкция."""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Subscription, User
from bot.keyboards import inline as ikb
from bot.keyboards import reply as rkb
from bot.services import subscriptions
from bot.services.delivery import send_access
from bot.services.vpn.base import VpnPanelError
from bot.texts import ru
from bot.utils.render import render_access
from bot.utils.tg import edit_view

log = logging.getLogger(__name__)
router = Router(name="access")


async def _load(
    session: AsyncSession, user: User, refresh: bool = False
) -> Tuple[Optional[Subscription], str, InlineKeyboardMarkup]:
    subscription = await repo.get_subscription(session, user.id)
    if subscription and refresh:
        try:
            subscription = await subscriptions.sync_usage(session, subscription)
        except VpnPanelError as exc:
            log.warning("Панель недоступна при обновлении данных: %s", exc)

    text = render_access(subscription)
    if subscription is None or not subscription.is_active:
        plans = await repo.list_plans(session)
        return subscription, text, ikb.plans_kb(plans)

    has_keys = bool(subscriptions.parse_links(subscription))
    return subscription, text, ikb.access_kb(subscription.subscription_url, has_keys=has_keys)


async def _send(message: Message, session: AsyncSession, user: User, refresh: bool = False) -> None:
    """Присылает всё для подключения: QR-код, ссылку-подписку и инструкцию."""
    subscription, text, markup = await _load(session, user, refresh=refresh)
    if subscription is None or not subscription.is_active:
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return
    await send_access(message, subscription)


@router.message(Command("access"))
@router.message(F.text == rkb.BTN_ACCESS)
async def show_access(message: Message, session: AsyncSession, user: User) -> None:
    await _send(message, session, user, refresh=True)


@router.callback_query(ikb.MenuCB.filter(F.action == "access"))
async def cb_access(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    _, text, markup = await _load(session, user)
    await edit_view(callback, text, markup)
    await callback.answer()


@router.callback_query(ikb.MenuCB.filter(F.action == "keys"))
async def cb_keys(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Отдельные ключи — запасной вариант, если приложение не понимает подписку."""
    subscription = await repo.get_subscription(session, user.id)
    links = subscriptions.parse_links(subscription) if subscription else []
    if not links:
        await callback.answer(ru.KEYS_EMPTY, show_alert=True)
        return

    rendered = "\n\n".join(f"<code>{link}</code>" for link in links[:5])
    await callback.message.answer(
        ru.KEYS_MESSAGE.format(links=rendered), disable_web_page_preview=True
    )
    await callback.answer()


@router.callback_query(ikb.MenuCB.filter(F.action == "refresh"))
async def cb_refresh(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    subscription = await repo.get_subscription(session, user.id)
    if subscription is None:
        await callback.answer(ru.NO_SUBSCRIPTION.format(trial_hint=""), show_alert=True)
        return
    # Перевыдача включает клиента в панели. Для истёкшей подписки или отключённой
    # админом это открыло бы доступ без оплаты, поэтому сначала смотрим, что в панели,
    # и для неактивной подписки просто показываем её состояние.
    subscription = await subscriptions.sync_usage(session, subscription)
    if not subscription.is_active:
        _, text, markup = await _load(session, user)
        await edit_view(callback, text, markup)
        await callback.answer()
        return

    try:
        await subscriptions.issue_or_extend(
            session,
            user,
            days=0,
            is_trial=subscription.is_trial,
            traffic_gb=subscription.traffic_limit // 1024 ** 3,
        )
    except VpnPanelError as exc:
        log.error("Не удалось обновить ключи для %s: %s", user.id, exc)
        await callback.answer("Панель недоступна, попробуйте позже", show_alert=True)
        return

    _, text, markup = await _load(session, user, refresh=True)
    await edit_view(callback, text, markup)
    await callback.answer("Данные обновлены")
