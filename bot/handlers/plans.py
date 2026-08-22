"""Витрина тарифов: покупка и продление."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import User
from bot.keyboards import inline as ikb
from bot.keyboards import reply as rkb
from bot.utils.render import plans_header, render_plan
from bot.utils.tg import edit_view

log = logging.getLogger(__name__)
router = Router(name="plans")


@router.message(Command("buy"))
@router.message(Command("renew"))
@router.message(F.text == rkb.BTN_BUY)
async def show_plans(message: Message, session: AsyncSession, user: User) -> None:
    plans = await repo.list_plans(session)
    if not plans:
        await message.answer("Тарифы временно недоступны, загляните позже.")
        return
    subscription = await repo.get_subscription(session, user.id)
    await message.answer(plans_header(subscription), reply_markup=ikb.plans_kb(plans))


@router.callback_query(ikb.MenuCB.filter(F.action == "plans"))
async def cb_plans(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    plans = await repo.list_plans(session)
    subscription = await repo.get_subscription(session, user.id)
    await edit_view(callback, plans_header(subscription), ikb.plans_kb(plans))
    await callback.answer()


@router.callback_query(ikb.PlanCB.filter())
async def cb_plan(
    callback: CallbackQuery, callback_data: ikb.PlanCB, session: AsyncSession, user: User
) -> None:
    plan = await repo.get_plan(session, callback_data.plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф больше недоступен", show_alert=True)
        return

    subscription = await repo.get_subscription(session, user.id)
    await edit_view(
        callback,
        render_plan(plan, subscription),
        ikb.pay_methods_kb(plan),
    )
    await callback.answer()
