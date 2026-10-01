"""Карточка клиента в админке: всё о пользователе на одном экране и правка в пару нажатий.

Срок, трафик и включённость меняются через сервис подписок, то есть сначала в панели:
панель — источник правды, и правка «только в базе» откатилась бы при ближайшей сверке.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone, tzinfo
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Payment, PaymentStatus, Subscription, SubscriptionStatus, User, utcnow
from bot.filters import IsAdmin
from bot.keyboards import inline as ikb
from bot.services import subscriptions
from bot.services.payments.registry import METHOD_TITLES
from bot.services.runtime import runtime
from bot.services.vpn.base import VpnPanelError
from bot.states import AdminStates
from bot.texts import ru
from bot.utils.render import time_left
from bot.utils.tg import edit_view

log = logging.getLogger(__name__)
router = Router(name="admin_users")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

GB = 1024 ** 3
MAX_DAYS = 3650
MAX_TRAFFIC_GB = 100_000

PAYMENT_STATUS = {
    PaymentStatus.paid: "✅ оплачен",
    PaymentStatus.pending: "⏳ ждёт",
    PaymentStatus.canceled: "✖️ отменён",
    PaymentStatus.failed: "⚠️ ошибка",
}


class InputError(ValueError):
    """Админ ввёл что-то не то — показываем подсказку и ждём снова."""


# ---------------------------------------------------------------- время
def _tz() -> tzinfo:
    try:
        return ZoneInfo(settings.tz)
    except Exception:  # noqa: BLE001 - нет базы часовых поясов — показываем UTC
        return timezone.utc


def local_time(moment: datetime) -> str:
    """Дата из БД (наивный UTC) в часовом поясе бота — так её привычнее читать."""
    shown = moment.replace(tzinfo=timezone.utc).astimezone(_tz())
    return f"{shown:%d.%m.%Y %H:%M} {shown.tzname() or 'UTC'}"


def parse_local_date(raw: str) -> Optional[datetime]:
    """«31.12.2026» (до конца дня) или «31.12.2026 18:00» в часовом поясе бота → наивный UTC."""
    for fmt, end_of_day in (("%d.%m.%Y %H:%M", False), ("%d.%m.%Y", True)):
        try:
            moment = datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
        if not 2000 < moment.year < 2100:
            return None
        if end_of_day:
            moment = moment.replace(hour=23, minute=59)
        return moment.replace(tzinfo=_tz()).astimezone(timezone.utc).replace(tzinfo=None)
    return None


# ---------------------------------------------------------------- карточка
def _state(subscription: Subscription) -> str:
    if subscription.status == SubscriptionStatus.disabled and subscription.expires_at > utcnow():
        limit = subscription.traffic_limit
        if limit and subscription.traffic_used >= limit:
            return "📉 трафик исчерпан"
        return "⏸ приостановлена"
    return "🟢 активна" if subscription.is_active else "⌛️ истекла"


async def client_card(session: AsyncSession, user_id: int) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    user = await session.get(User, user_id)
    if user is None:
        return None

    subscription = await repo.get_subscription(session, user_id)
    panel_note = ""
    if subscription is not None:
        try:
            # показываем то, что сейчас в панели, а не то, что помнит база
            subscription = await subscriptions.sync_usage(session, subscription, strict=True)
        except VpnPanelError as exc:
            panel_note = (
                "\n\n⚠️ Панель не ответила — показаны данные из базы:\n"
                f"<code>{html.escape(str(exc))[:200]}</code>"
            )

    paid_count, paid_sum = await repo.user_paid_total(session, user_id)
    recent = await repo.user_payments(session, user_id, limit=20)
    pending = sum(1 for p in recent if p.status == PaymentStatus.pending)

    title = html.escape(user.title)
    if user.username and user.first_name:
        title += f" · {html.escape(user.first_name)}"
    lines = [
        f"👤 <b>{title}</b>",
        f"ID: <code>{user.id}</code> · в боте с {user.created_at:%d.%m.%Y}",
        f"Пробный период: {'использован' if user.trial_used else 'не использован'}"
        + (" · 🚫 <b>в бане</b>" if user.is_banned else ""),
        "",
    ]
    if subscription is None:
        lines.append("🔑 Подписки нет")
    else:
        limit = ru.human_bytes(subscription.traffic_limit) if subscription.traffic_limit else "безлимит"
        lines += [
            f"🔑 Подписка: <b>{_state(subscription)}</b>{' (пробная)' if subscription.is_trial else ''}",
            f"До: <b>{local_time(subscription.expires_at)}</b> ({time_left(subscription)})",
            f"Тариф: {html.escape(subscription.plan.title) if subscription.plan else '—'}",
            f"Трафик: {ru.human_bytes(subscription.traffic_used)} из {limit}",
            f"В панели: <code>{subscription.vpn_username}</code>",
        ]
    lines += ["", f"💳 Оплачено: <b>{paid_count}</b> на <b>{paid_sum:.0f} ₽</b>"]
    if pending:
        lines.append(f"⏳ Неоплаченных счетов: {pending} — см. «Платежи»")
    return "\n".join(lines) + panel_note, ikb.client_card_kb(user, subscription)


async def send_card(message: Message, session: AsyncSession, user_id: int) -> None:
    card = await client_card(session, user_id)
    if card is None:
        await message.answer("Пользователь не найден.")
        return
    await message.answer(card[0], reply_markup=card[1])


async def show_search_results(message: Message, session: AsyncSession, query: str) -> None:
    """Один найденный — сразу карточка, несколько — кнопки на выбор."""
    users = await repo.find_users(session, query)
    if not users:
        await message.answer("Ничего не найдено. Нужен ID или @username — пользователь должен был запустить бота.")
        return
    if len(users) == 1:
        await send_card(message, session, users[0].id)
        return
    await message.answer(f"Найдено: <b>{len(users)}</b>. Выберите:", reply_markup=ikb.clients_kb(users))


async def toggle_ban(session: AsyncSession, target: User) -> str:
    """Банит или разбанивает. Бан приостанавливает доступ, разбан возвращает, если срок не вышел.

    Возвращает итог для админа простым текстом.
    """
    target.is_banned = not target.is_banned
    await session.commit()
    subscription = await repo.get_subscription(session, target.id)
    note = ""
    if subscription is not None:
        try:
            if target.is_banned:
                await subscriptions.suspend(session, subscription)
            elif subscription.expires_at > utcnow():
                await subscriptions.restore(session, subscription)
                note = f"\nДоступ снова включён до {subscription.expires_at:%d.%m.%Y}."
        except VpnPanelError as exc:
            note = f"\n⚠️ Панель не ответила, доступ в ней не изменён: {exc}"
    return f"{'🚫 Заблокирован' if target.is_banned else '✅ Разблокирован'}: {target.title}{note}"


# ---------------------------------------------------------------- открыть карточку
@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    query = (command.args or "").strip()
    if not query:
        await message.answer("Формат: <code>/user 123456789</code> или <code>/user @username</code>")
        return
    await show_search_results(message, session, query)


@router.callback_query(ikb.ClientCB.filter(F.action == "card"))
async def cb_card(
    callback: CallbackQuery, callback_data: ikb.ClientCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    card = await client_card(session, callback_data.user_id)
    if card is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await edit_view(callback, *card)
    await callback.answer()


# ---------------------------------------------------------------- действия одной кнопкой
@router.callback_query(ikb.ClientCB.filter(F.action.in_({"reset", "suspend", "resume", "trial", "ban"})))
async def cb_action(callback: CallbackQuery, callback_data: ikb.ClientCB, session: AsyncSession) -> None:
    user = await session.get(User, callback_data.user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    subscription = await repo.get_subscription(session, user.id)
    action = callback_data.action
    if action in ("reset", "suspend", "resume") and subscription is None:
        await callback.answer("У клиента нет подписки", show_alert=True)
        return

    try:
        if action == "reset":
            await subscriptions.reset_usage(session, subscription)
            result = "Трафик сброшен"
        elif action == "suspend":
            await subscriptions.suspend(session, subscription)
            result = "Доступ приостановлен"
        elif action == "resume":
            if subscription.expires_at <= utcnow():
                await callback.answer("Срок уже вышел — продлите: «➕ Дни» или «📅 Дата».", show_alert=True)
                return
            await subscriptions.restore(session, subscription)
            result = "Доступ возобновлён"
        elif action == "trial":
            user.trial_used = not user.trial_used
            await session.commit()
            result = "Триал отмечен использованным" if user.trial_used else "Триал снова доступен"
        else:
            result = await toggle_ban(session, user)
    except VpnPanelError as exc:
        await callback.answer(f"Панель недоступна: {exc}"[:200], show_alert=True)
        return

    log.info("Админ %s: %s для %s", callback.from_user.id, action, user.id)
    card = await client_card(session, user.id)
    if card is not None:
        await edit_view(callback, *card)
    await callback.answer(result[:200], show_alert="\n" in result)


# ---------------------------------------------------------------- действия с вводом
PROMPTS = {
    "days": "Сколько дней прибавить? Целое число, со знаком минус — убавить.\n"
            "Например: <code>30</code> или <code>-7</code>. Клиент получит уведомление, "
            "приостановленный доступ включится.",
    "date": "Новая дата окончания ({tz}):\n<code>31.12.2026</code> — до конца дня, "
            "или <code>31.12.2026 18:00</code>. Приостановленный доступ включится.",
    "traffic": "Лимит трафика в ГБ, <code>0</code> — безлимит. Сейчас: <b>{current}</b>.",
    "msg": "Текст сообщения клиенту — уйдёт от имени бота, форматирование сохранится.",
}


@router.callback_query(ikb.ClientCB.filter(F.action.in_(set(PROMPTS))))
async def cb_ask(
    callback: CallbackQuery, callback_data: ikb.ClientCB, session: AsyncSession, state: FSMContext
) -> None:
    user = await session.get(User, callback_data.user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    current = "—"
    if callback_data.action == "traffic":
        subscription = await repo.get_subscription(session, user.id)
        if subscription is None:
            await callback.answer("У клиента нет подписки", show_alert=True)
            return
        current = ru.human_bytes(subscription.traffic_limit) if subscription.traffic_limit else "безлимит"

    await state.set_state(AdminStates.client_input)
    await state.update_data(user_id=user.id, field=callback_data.action)
    prompt = PROMPTS[callback_data.action].format(tz=_tz(), current=current)
    await callback.message.answer(f"<b>{html.escape(user.title)}</b>\n{prompt}\n\nОтмена — /cancel.")
    await callback.answer()


@router.message(AdminStates.client_input)
async def on_input(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user = await session.get(User, int(data.get("user_id", 0)))
    field = str(data.get("field", ""))
    if user is None:
        await state.clear()
        await message.answer("Пользователь не найден.")
        return

    try:
        if field == "days":
            result = await _add_days(session, bot, user, message.text or "")
        elif field == "date":
            result = await _set_date(session, user, message.text or "")
        elif field == "traffic":
            result = await _set_traffic(session, user, message.text or "")
        elif field == "msg":
            result = await _send_to_client(bot, user, message)
        else:
            raise InputError("Неизвестное действие.")
    except InputError as exc:
        await message.answer(f"{exc} Попробуйте ещё раз или /cancel.")
        return  # остаёмся в том же вводе
    except VpnPanelError as exc:
        await state.clear()
        await message.answer(f"❌ Панель недоступна, ничего не изменено:\n<code>{html.escape(str(exc))}</code>")
        return

    await state.clear()
    log.info("Админ %s: %s для %s", message.from_user.id, field, user.id)
    await message.answer(result)
    await send_card(message, session, user.id)


async def _add_days(session: AsyncSession, bot: Bot, user: User, raw: str) -> str:
    raw = raw.strip()
    if not re.fullmatch(r"-?\d{1,6}", raw):
        raise InputError("Нужно целое число дней.")
    days = int(raw)
    if days == 0 or abs(days) > MAX_DAYS:
        raise InputError(f"От 1 до {MAX_DAYS} дней (со знаком минус — убавить).")

    current = await repo.get_subscription(session, user.id)
    # меняем только срок: лимит трафика и пометку триала оставляем как есть
    traffic_gb = current.traffic_limit // GB if current else runtime.default_traffic_gb
    subscription = await subscriptions.issue_or_extend(
        session, user, days=days, traffic_gb=traffic_gb, is_trial=bool(current and current.is_trial)
    )
    headline = (
        f"🎁 Вам начислено <b>{days}</b> дн. подписки."
        if days > 0
        else "Срок подписки изменён администратором."
    )
    try:
        await bot.send_message(
            user.id, f"{headline}\nДоступ активен до <b>{subscription.expires_at:%d.%m.%Y}</b>."
        )
    except TelegramAPIError:
        pass
    return f"✅ {'+' if days > 0 else ''}{days} дн. Подписка до <b>{local_time(subscription.expires_at)}</b>."


async def _set_date(session: AsyncSession, user: User, raw: str) -> str:
    moment = parse_local_date(raw)
    if moment is None:
        raise InputError("Не понял дату. Формат: <code>31.12.2026</code> или <code>31.12.2026 18:00</code>.")
    subscription = await subscriptions.set_expiry(session, user, moment)
    tail = "" if moment > utcnow() else "\nДата в прошлом — доступ отключится при ближайшей проверке просрочки."
    return f"✅ Подписка до <b>{local_time(subscription.expires_at)}</b>.{tail}"


async def _set_traffic(session: AsyncSession, user: User, raw: str) -> str:
    raw = raw.strip()
    if not raw.isdigit() or int(raw) > MAX_TRAFFIC_GB:
        raise InputError(f"Нужно целое число ГБ от 0 до {MAX_TRAFFIC_GB}.")
    try:
        subscription = await subscriptions.set_traffic_limit(session, user, int(raw))
    except ValueError as exc:
        raise InputError(f"Нельзя: {exc}.") from None
    limit = ru.human_bytes(subscription.traffic_limit) if subscription.traffic_limit else "безлимит"
    return f"✅ Лимит трафика: <b>{limit}</b>."


async def _send_to_client(bot: Bot, user: User, message: Message) -> str:
    if not message.text:
        raise InputError("Нужен текст.")
    try:
        await bot.send_message(user.id, f"✉️ <b>Сообщение от администрации</b>\n\n{message.html_text}")
    except TelegramForbiddenError:
        return "⚠️ Не доставлено: клиент заблокировал бота."
    except TelegramAPIError as exc:
        return f"⚠️ Не доставлено: <code>{html.escape(str(exc))}</code>"
    return "✅ Сообщение отправлено."


# ---------------------------------------------------------------- платежи
def _payment_line(payment: Payment) -> str:
    method = METHOD_TITLES.get(payment.provider, payment.provider)
    plan = html.escape(payment.plan.title) if payment.plan else f"{payment.days} дн."
    when = local_time(payment.paid_at or payment.created_at)
    status = PAYMENT_STATUS.get(payment.status, payment.status.value)
    return f"#{payment.id} · {when}\n   {method} · {payment.amount:.0f} ₽ · {plan} · {status}"


@router.callback_query(ikb.ClientCB.filter(F.action == "pays"))
async def cb_payments(callback: CallbackQuery, callback_data: ikb.ClientCB, session: AsyncSession) -> None:
    user = await session.get(User, callback_data.user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    payments = await repo.user_payments(session, user.id, limit=15)
    if not payments:
        await callback.answer("Платежей нет", show_alert=True)
        return

    lines = [f"💳 <b>Платежи</b> {html.escape(user.title)} — последние {len(payments)}", ""]
    lines += [_payment_line(p) for p in payments]
    pending_sbp = [p.id for p in payments if p.provider == "sbp" and p.status == PaymentStatus.pending]
    if pending_sbp:
        lines += ["", "Счета СБП, которые ждут подтверждения, — кнопками ниже."]
    await edit_view(callback, "\n".join(lines), ikb.client_payments_kb(user.id, pending_sbp))
    await callback.answer()
