"""Бесплатный пробный период."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import User
from bot.keyboards import inline as ikb
from bot.keyboards import reply as rkb
from bot.services import subscriptions
from bot.services.delivery import send_access
from bot.services.runtime import runtime
from bot.services.vpn.base import VpnPanelError
from bot.texts import ru
from bot.utils.channel import is_subscribed

log = logging.getLogger(__name__)
router = Router(name="trial")


@router.message(Command("trial"))
@router.message(F.text == rkb.BTN_TRIAL)
async def grant_trial(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    if not runtime.trial_enabled:
        await message.answer(ru.TRIAL_DISABLED)
        return
    if user.trial_used:
        plans = await repo.list_plans(session)
        await message.answer(ru.TRIAL_ALREADY, reply_markup=ikb.plans_kb(plans))
        return

    if not await is_subscribed(bot, user.id):
        await message.answer(
            "Чтобы получить пробный период, подпишитесь на наш канал и нажмите кнопку ещё раз.",
            reply_markup=ikb.support_kb(),
        )
        return

    try:
        subscription = await subscriptions.grant_trial(session, user)
    except VpnPanelError as exc:
        log.error("Не удалось выдать триал %s: %s", user.id, exc)
        await message.answer(ru.ERROR_PANEL_TRIAL)
        return
    if subscription is None:  # параллельное нажатие уже выдало триал
        plans = await repo.list_plans(session)
        await message.answer(ru.TRIAL_ALREADY, reply_markup=ikb.plans_kb(plans))
        return

    await message.answer("Готово! Обновил меню 👇", reply_markup=rkb.main_menu(show_trial=False))
    await send_access(
        message,
        subscription,
        header=ru.TRIAL_DONE_HEADER.format(days=ru.plural_days(runtime.trial_days)),
    )
    log.info("Триал выдан пользователю %s до %s", user.id, subscription.expires_at)
