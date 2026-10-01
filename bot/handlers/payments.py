"""Оплата: Telegram Stars, СБП переводом по номеру, ЮKassa и CryptoBot."""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import PaymentStatus, Plan, User
from bot.keyboards import inline as ikb
from bot.services import subscriptions
from bot.services.payments.base import PaymentError, rub_to_stars
from bot.services.payments.registry import get_provider
from bot.services.runtime import runtime
from bot.services.vpn.base import VpnPanelError
from bot.texts import ru
from bot.utils.tg import edit_view

log = logging.getLogger(__name__)
router = Router(name="payments")

STARS_PAYLOAD = "sub"


@router.callback_query(ikb.PayCB.filter())
async def cb_pay(
    callback: CallbackQuery,
    callback_data: ikb.PayCB,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    plan = await repo.get_plan(session, callback_data.plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф больше недоступен", show_alert=True)
        return

    method = callback_data.method
    price, days = plan.price_rub, plan.days

    if method == "stars":
        await _pay_with_stars(callback, session, user, plan, price, days, bot)
        return
    if method == "sbp":
        await _pay_sbp(callback, session, user, plan, price, days)
        return

    provider = get_provider(method)
    if provider is None:
        await callback.answer("Способ оплаты сейчас недоступен", show_alert=True)
        return

    payment = await repo.create_payment(
        session, user.id, plan, provider=method, amount=price, days=days
    )
    try:
        invoice = await provider.create_invoice(
            payment.id, price, description=f"Подписка {plan.title}"
        )
    except PaymentError as exc:
        log.error("Не удалось создать счёт (%s): %s", method, exc)
        payment.status = PaymentStatus.failed
        await session.commit()
        await callback.answer("Платёжный сервис недоступен, попробуйте другой способ", show_alert=True)
        return

    payment.external_id = invoice.external_id
    payment.pay_url = invoice.url
    payment.currency = provider.currency
    payment.amount = price
    await session.commit()

    amount_label = (
        f"{price:.0f} ₽"
        if provider.currency == "RUB"
        else f"{provider.convert(price)} {settings.cryptobot_asset.upper()} (≈{price:.0f} ₽)"
    )
    await edit_view(
        callback,
        ru.PAYMENT_CREATED.format(plan=plan.title, amount=amount_label),
        ikb.invoice_kb(invoice.url, payment.id),
    )
    await callback.answer()


async def _pay_with_stars(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    plan: Plan,
    price: float,
    days: int,
    bot: Bot,
) -> None:
    stars = rub_to_stars(price)
    payment = await repo.create_payment(
        session,
        user.id,
        plan,
        provider="stars",
        amount=price,
        currency="XTR",
        days=days,
    )
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"Подписка {plan.title}",
        description=f"Доступ к VPN на {ru.plural_days(days)}. Оплата звёздами Telegram.",
        payload=f"{STARS_PAYLOAD}:{payment.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка {plan.title}", amount=stars)],
    )
    await callback.answer()


# ---------------------------------------------------------------- СБП вручную
def sbp_code(payment_id: int) -> str:
    return f"VPN-{payment_id}"


async def _pay_sbp(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    plan: Plan,
    price: float,
    days: int,
) -> None:
    payment = await repo.create_payment(
        session, user.id, plan, provider="sbp", amount=price, days=days
    )
    payment.external_id = sbp_code(payment.id)
    await session.commit()

    # edit_view, а не edit_text: в старых сообщениях кнопки оплаты висят под фото с QR-кодом,
    # а у фото нет текста — edit_text падает, и пользователь не видит ничего
    await edit_view(
        callback,
        ru.SBP_INVOICE.format(
            plan=plan.title,
            amount=price,
            code=sbp_code(payment.id),
            phone=runtime.sbp_phone,
            bank=f"Банк получателя: <b>{runtime.sbp_bank}</b>\n" if runtime.sbp_bank else "",
            receiver=f"Получатель: <b>{runtime.sbp_receiver}</b>\n" if runtime.sbp_receiver else "",
        ),
        ikb.sbp_invoice_kb(payment.id),
    )
    await callback.answer()


@router.callback_query(ikb.SbpCB.filter(F.action == "claim"))
async def cb_sbp_claim(
    callback: CallbackQuery, callback_data: ikb.SbpCB, session: AsyncSession, user: User, bot: Bot
) -> None:
    """Пользователь сообщил о переводе — отправляем заявку администраторам."""
    payment = await repo.get_payment(session, callback_data.payment_id)
    if payment is None or payment.user_id != user.id:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if payment.status == PaymentStatus.paid:
        await callback.answer("Этот платёж уже подтверждён ✅", show_alert=True)
        return
    if payment.status != PaymentStatus.pending:
        await callback.answer(ru.PAYMENT_CANCELED, show_alert=True)
        return

    plan_title = payment.plan.title if payment.plan else f"{payment.days} дн."
    request_text = (
        "🏦 <b>Заявка на оплату по СБП</b>\n\n"
        f"От: {user.title} (<code>{user.id}</code>)\n"
        f"Тариф: <b>{plan_title}</b>\n"
        f"Сумма: <b>{payment.amount:.0f} ₽</b>\n"
        f"Код платежа: <b>{sbp_code(payment.id)}</b>\n\n"
        "Проверьте поступление и подтвердите — подписка выдастся автоматически."
    )
    delivered = 0
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, request_text, reply_markup=ikb.sbp_admin_kb(payment.id))
            delivered += 1
        except TelegramAPIError as exc:
            log.warning("Заявка СБП не доставлена админу %s: %s", admin_id, exc)

    if not delivered:
        # Клиент, возможно, уже перевёл деньги — заявку нельзя терять.
        # Счёт остаётся pending и виден администратору по команде /claims.
        log.error(
            "Заявку СБП %s не удалось доставить ни одному администратору (ADMIN_IDS=%s). "
            "Проверьте ID и то, что администратор нажал /start в боте.",
            payment.id,
            settings.admin_ids,
        )
        await edit_view(callback, ru.SBP_CLAIMED_OFFLINE.format(code=sbp_code(payment.id)))
        await callback.answer()
        return

    await edit_view(callback, ru.SBP_CLAIMED)
    await callback.answer()


@router.callback_query(ikb.SbpCB.filter(F.action.in_({"ok", "no"})))
async def cb_sbp_decide(
    callback: CallbackQuery, callback_data: ikb.SbpCB, session: AsyncSession, bot: Bot
) -> None:
    """Решение администратора по переводу."""
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    payment = await repo.get_payment(session, callback_data.payment_id)
    if payment is None:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if payment.status == PaymentStatus.paid:
        await callback.answer("Уже подтверждён", show_alert=True)
        return

    if callback_data.action == "no":
        payment.status = PaymentStatus.canceled
        await session.commit()
        try:
            await bot.send_message(payment.user_id, ru.SBP_REJECTED.format(code=sbp_code(payment.id)))
        except TelegramAPIError:
            pass
        await callback.message.edit_text(
            callback.message.html_text + "\n\n❌ <b>Отклонено</b>", reply_markup=None
        )
        await callback.answer()
        return

    try:
        subscription = await subscriptions.complete_payment(session, bot, payment)
    except VpnPanelError as exc:
        log.error("Панель недоступна при подтверждении СБП %s: %s", payment.id, exc)
        await callback.answer("Панель недоступна — попробуйте нажать ещё раз через минуту", show_alert=True)
        return

    until = f"{subscription.expires_at:%d.%m.%Y}" if subscription else "—"
    await callback.message.edit_text(
        callback.message.html_text + f"\n\n✅ <b>Подтверждено</b> · доступ до {until}", reply_markup=None
    )
    await callback.answer("Подписка выдана")


# ---------------------------------------------------------------- Telegram Stars
@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message, session: AsyncSession, bot: Bot
) -> None:
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith(f"{STARS_PAYLOAD}:"):
        log.warning("Неизвестный payload платежа: %s", payload)
        return

    payment_id = int(payload.split(":", 1)[1])
    payment = await repo.get_payment(session, payment_id)
    if payment is None:
        log.error("Платёж %s не найден", payment_id)
        await message.answer(ru.ERROR_GENERIC)
        return

    payment.external_id = sp.telegram_payment_charge_id
    await session.commit()

    try:
        await subscriptions.complete_payment(session, bot, payment)
    except VpnPanelError as exc:
        log.error("Панель недоступна после оплаты %s: %s", payment.id, exc)
        await message.answer(ru.ERROR_PANEL)
        return


# ---------------------------------------------------------------- ручная проверка
@router.callback_query(ikb.CheckCB.filter())
async def cb_check(
    callback: CallbackQuery, callback_data: ikb.CheckCB, session: AsyncSession, bot: Bot
) -> None:
    payment = await repo.get_payment(session, callback_data.payment_id)
    if payment is None:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if payment.status == PaymentStatus.paid:
        await callback.answer("Платёж уже подтверждён ✅", show_alert=True)
        return

    provider = get_provider(payment.provider)
    if provider is None or not payment.external_id:
        await callback.answer(ru.PAYMENT_PENDING, show_alert=True)
        return

    try:
        status = await provider.check(payment.external_id)
    except PaymentError as exc:
        log.warning("Проверка платежа %s не удалась: %s", payment.id, exc)
        await callback.answer("Платёжный сервис не отвечает, попробуйте через минуту", show_alert=True)
        return

    if status == "paid":
        try:
            await subscriptions.complete_payment(session, bot, payment)
        except VpnPanelError as exc:
            log.error("Панель недоступна после оплаты %s: %s", payment.id, exc)
            await callback.answer("Оплата принята, выдаём доступ…", show_alert=True)
            await callback.message.answer(ru.ERROR_PANEL)
            return
        await edit_view(callback, "✅ Оплата подтверждена. Подписка активна!")
        await callback.answer()
    elif status == "canceled":
        payment.status = PaymentStatus.canceled
        await session.commit()
        await callback.answer(ru.PAYMENT_CANCELED, show_alert=True)
    else:
        await callback.answer(ru.PAYMENT_PENDING, show_alert=True)
