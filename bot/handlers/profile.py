"""Профиль пользователя."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import User
from bot.keyboards import inline as ikb
from bot.keyboards import reply as rkb
from bot.utils.render import render_profile

log = logging.getLogger(__name__)
router = Router(name="profile")


@router.message(Command("profile"))
@router.message(F.text == rkb.BTN_PROFILE)
async def show_profile(message: Message, session: AsyncSession, user: User) -> None:
    subscription = await repo.get_subscription(session, user.id)
    await message.answer(render_profile(user, subscription), reply_markup=ikb.profile_kb())
