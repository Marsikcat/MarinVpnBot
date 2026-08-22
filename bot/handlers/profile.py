"""Профиль, рефералы, промокоды."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import User
from bot.keyboards import inline as ikb
from bot.keyboards import reply as rkb
from bot.services.referrals import referral_link
from bot.states import PromoStates
from bot.utils.render import render_profile, render_referral

log = logging.getLogger(__name__)
router = Router(name="profile")


@router.message(Command("profile"))
@router.message(F.text == rkb.BTN_PROFILE)
async def show_profile(message: Message, session: AsyncSession, user: User) -> None:
    subscription = await repo.get_subscription(session, user.id)
    referrals = await repo.count_referrals(session, user.id)
    await message.answer(render_profile(user, subscription, referrals), reply_markup=ikb.profile_kb())


@router.callback_query(ikb.MenuCB.filter(F.action == "referral"))
async def cb_referral(callback: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    me = await bot.me()
    link = referral_link(me.username, user.id)
    count = await repo.count_referrals(session, user.id)
    await callback.message.edit_text(
        render_referral(user, link, count),
        reply_markup=ikb.back_kb("plans"),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.message(Command("ref"))
async def cmd_ref(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    me = await bot.me()
    link = referral_link(me.username, user.id)
    count = await repo.count_referrals(session, user.id)
    await message.answer(render_referral(user, link, count), disable_web_page_preview=True)


# ---------------------------------------------------------------- промокоды
@router.callback_query(ikb.MenuCB.filter(F.action == "promo"))
async def cb_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_code)
    await callback.message.answer("Отправьте промокод одним сообщением (или /cancel).")
    await callback.answer()


@router.message(Command("promo"))
async def cmd_promo(message: Message, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_code)
    await message.answer("Отправьте промокод одним сообщением (или /cancel).")


@router.message(PromoStates.waiting_code)
async def apply_promo(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    promo = await repo.get_promo(session, code)

    if promo is None or not promo.is_usable():
        await state.clear()
        await message.answer("Промокод не найден или больше не действует.")
        return
    if await repo.promo_used_by(session, promo.id, user.id):
        await state.clear()
        await message.answer("Этот промокод вы уже использовали.")
        return

    await state.set_state(None)
    await state.update_data(promo_code=promo.code)

    bonus = f" и +{promo.bonus_days} дн. к сроку" if promo.bonus_days else ""
    plans = await repo.list_plans(session)
    await message.answer(
        f"✅ Промокод <b>{promo.code}</b> применён: скидка <b>{promo.discount_percent}%</b>{bonus}.\n"
        "Выберите тариф — цена пересчитается при оплате.",
        reply_markup=ikb.plans_kb(plans),
    )
