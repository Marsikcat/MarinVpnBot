"""Реферальная программа: процент от платежей приглашённых на баланс пригласившего."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Payment, User
from bot.services.runtime import runtime

log = logging.getLogger(__name__)


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref{user_id}"


def parse_referrer(payload: Optional[str]) -> Optional[int]:
    """Разбирает deep-link `?start=ref123456`."""
    if not payload:
        return None
    raw = payload.strip()
    if raw.startswith("ref"):
        raw = raw[3:]
    return int(raw) if raw.isdigit() else None


async def reward_referrer(session: AsyncSession, bot: Bot, payer: User, payment: Payment) -> None:
    """Начисляет пригласившему процент от оплаты. Вызывается после подтверждения платежа."""
    if not runtime.referral_enabled or not payer.referrer_id or payment.provider == "balance":
        return

    referrer = await session.get(User, payer.referrer_id)
    if referrer is None or referrer.is_banned:
        return

    reward = round(payment.amount * runtime.referral_percent / 100, 2)
    if reward <= 0:
        return

    referrer.balance = round((referrer.balance or 0) + reward, 2)
    referrer.referral_earned = round((referrer.referral_earned or 0) + reward, 2)
    await session.commit()

    try:
        await bot.send_message(
            referrer.id,
            f"🎁 Реферальное вознаграждение: <b>{reward:.0f} ₽</b>\n"
            f"Приглашённый пользователь оплатил подписку.\n"
            f"Баланс: <b>{referrer.balance:.0f} ₽</b>",
        )
    except TelegramAPIError as exc:
        log.warning("Не удалось уведомить реферера %s: %s", referrer.id, exc)
