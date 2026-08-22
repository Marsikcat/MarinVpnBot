"""Админ-панель: статистика, выдача дней, промокоды, рассылка, диагностика."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import timedelta
from typing import Tuple, Union

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import PromoCode, User, utcnow
from bot.keyboards import inline as ikb
from bot.services import subscriptions
from bot.services.runtime import runtime
from bot.services.vpn.base import VpnPanelError
from bot.services.vpn.factory import get_panel
from bot.states import AdminStates
from bot.utils.tg import edit_view

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = event.from_user
        return bool(user and settings.is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.callback_query(ikb.AdminCB.filter(F.action == "home"))
async def cb_admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=ikb.admin_kb())
    await callback.answer()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=ikb.admin_kb())


# ---------------------------------------------------------------- статистика
async def _stats_text(session: AsyncSession) -> str:
    now = utcnow()
    total_users = await repo.count_users(session)
    day_users = await repo.count_new_users(session, now - timedelta(days=1))
    week_users = await repo.count_new_users(session, now - timedelta(days=7))
    active = await repo.count_active_subscriptions(session)
    revenue_day = await repo.revenue_since(session, now - timedelta(days=1))
    revenue_month = await repo.revenue_since(session, now - timedelta(days=30))
    revenue_all = await repo.total_revenue(session)

    return (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{total_users}</b> (+{day_users} за сутки, +{week_users} за неделю)\n"
        f"Активных подписок: <b>{active}</b>\n\n"
        f"Выручка за сутки: <b>{revenue_day:.0f} ₽</b>\n"
        f"Выручка за 30 дней: <b>{revenue_month:.0f} ₽</b>\n"
        f"Всего: <b>{revenue_all:.0f} ₽</b>\n\n"
        f"Панель: <code>{settings.vpn_provider}</code> · "
        f"оплата: <code>{', '.join(settings.enabled_payment_methods) or 'нет'}</code>"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    await message.answer(await _stats_text(session))


@router.callback_query(ikb.AdminCB.filter(F.action == "stats"))
async def cb_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.message.edit_text(await _stats_text(session), reply_markup=ikb.admin_kb())
    await callback.answer()


# ---------------------------------------------------------------- список пользователей
PER_PAGE = 10


def _user_line(index: int, user: User, subscription) -> str:
    if subscription is None:
        state = "нет подписки"
    elif subscription.is_active:
        state = f"до {subscription.expires_at:%d.%m.%Y}"
        if subscription.is_trial:
            state += " (триал)"
    else:
        state = f"истекла {subscription.expires_at:%d.%m.%Y}"

    extra = ""
    if user.balance:
        extra += f" · {user.balance:.0f} ₽"
    if user.is_banned:
        extra += " · 🚫"
    return f"{index}. {user.title} (<code>{user.id}</code>) — {state}{extra}"


async def _users_text(session: AsyncSession, page: int, scope: str) -> Tuple[str, int, int]:
    total = await repo.count_users_by_scope(session, scope)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(max(page, 0), pages - 1)
    users = await repo.list_users_page(session, page=page, per_page=PER_PAGE, scope=scope)

    lines = []
    for offset, found in enumerate(users, start=page * PER_PAGE + 1):
        subscription = await repo.get_subscription(session, found.id)
        lines.append(_user_line(offset, found, subscription))

    header = (
        f"👥 <b>Пользователи</b> — {ikb.SCOPE_TITLES.get(scope, scope).lower()}\n"
        f"Всего: <b>{total}</b>"
    )
    body = "\n".join(lines) if lines else "Пусто."
    return f"{header}\n\n{body}", page, pages


@router.message(Command("users"))
async def cmd_users(message: Message, session: AsyncSession) -> None:
    text, page, pages = await _users_text(session, 0, "all")
    await message.answer(text, reply_markup=ikb.users_kb(page, pages, "all"))


@router.callback_query(ikb.UsersCB.filter(F.action.in_({"page", "scope"})))
async def cb_users(
    callback: CallbackQuery, callback_data: ikb.UsersCB, session: AsyncSession
) -> None:
    text, page, pages = await _users_text(session, callback_data.page, callback_data.scope)
    await edit_view(callback, text, ikb.users_kb(page, pages, callback_data.scope))
    await callback.answer()


@router.callback_query(ikb.UsersCB.filter(F.action == "export"))
async def cb_users_export(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выгружает всех пользователей в CSV — удобно для рассылок и отчётности."""
    users = await repo.all_users_for_export(session)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["id", "username", "имя", "регистрация", "подписка до", "статус", "триал", "баланс", "рефералов", "бан"]
    )
    for found in users:
        subscription = await repo.get_subscription(session, found.id)
        writer.writerow(
            [
                found.id,
                found.username or "",
                found.first_name or "",
                f"{found.created_at:%d.%m.%Y}",
                f"{subscription.expires_at:%d.%m.%Y}" if subscription else "",
                (subscription.status.value if subscription else "нет"),
                "да" if found.trial_used else "нет",
                f"{found.balance:.0f}",
                await repo.count_referrals(session, found.id),
                "да" if found.is_banned else "нет",
            ]
        )

    data = buffer.getvalue().encode("utf-8-sig")  # BOM, иначе Excel испортит кириллицу
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"users-{utcnow():%Y%m%d}.csv"),
        caption=f"Выгрузка: {len(users)} пользователей",
    )
    await callback.answer("Готово")


# ---------------------------------------------------------------- заявки по СБП
@router.message(Command("claims"))
async def cmd_claims(message: Message, session: AsyncSession) -> None:
    """Неоплаченные счета по СБП — на случай, если заявка не дошла в личку."""
    payments = await repo.pending_payments(session, "sbp")
    if not payments:
        await message.answer("Открытых счетов по СБП нет.")
        return

    from bot.handlers.payments import sbp_code

    await message.answer(f"🏦 Открытых счетов по СБП: <b>{len(payments)}</b>")
    for payment in payments[:20]:
        payer = await session.get(User, payment.user_id)
        plan_title = payment.plan.title if payment.plan else f"{payment.days} дн."
        await message.answer(
            f"Код: <b>{sbp_code(payment.id)}</b>\n"
            f"От: {payer.title if payer else payment.user_id} (<code>{payment.user_id}</code>)\n"
            f"Тариф: <b>{plan_title}</b> · сумма: <b>{payment.amount:.0f} ₽</b>\n"
            f"Создан: {payment.created_at:%d.%m.%Y %H:%M} UTC",
            reply_markup=ikb.sbp_admin_kb(payment.id),
        )


@router.callback_query(ikb.AdminCB.filter(F.action == "claims"))
async def cb_claims(callback: CallbackQuery, session: AsyncSession) -> None:
    await cmd_claims(callback.message, session)
    await callback.answer()


# ---------------------------------------------------------------- диагностика
@router.callback_query(ikb.AdminCB.filter(F.action == "health"))
async def cb_health(callback: CallbackQuery) -> None:
    panel = get_panel()
    try:
        await panel.get("healthcheck-probe")
        result = f"✅ Панель <b>{panel.name}</b> отвечает"
    except VpnPanelError as exc:
        result = f"❌ Панель <b>{panel.name}</b> недоступна:\n<code>{exc}</code>"
    await callback.message.edit_text(result, reply_markup=ikb.admin_kb())
    await callback.answer()


# ---------------------------------------------------------------- поиск пользователя
@router.callback_query(ikb.AdminCB.filter(F.action == "find"))
async def cb_find(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.find_user)
    await callback.message.answer("Отправьте ID или @username пользователя.")
    await callback.answer()


@router.message(AdminStates.find_user)
async def do_find(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    users = await repo.find_users(session, message.text or "")
    if not users:
        await message.answer("Ничего не найдено.")
        return

    chunks = []
    for found in users:
        subscription = await repo.get_subscription(session, found.id)
        status = "нет"
        if subscription:
            status = (
                f"до {subscription.expires_at:%d.%m.%Y}"
                f"{' (активна)' if subscription.is_active else ' (истекла)'}"
            )
        chunks.append(
            f"<b>{found.title}</b>\n"
            f"ID: <code>{found.id}</code>\n"
            f"Подписка: {status}\n"
            f"Баланс: {found.balance:.0f} ₽ · рефералов: "
            f"{await repo.count_referrals(session, found.id)}\n"
            f"Регистрация: {found.created_at:%d.%m.%Y}"
        )
    await message.answer("\n\n".join(chunks))


# ---------------------------------------------------------------- выдача дней
@router.callback_query(ikb.AdminCB.filter(F.action == "grant"))
async def cb_grant(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.grant_days)
    await callback.message.answer("Формат: <code>user_id дни</code>\nНапример: <code>123456789 30</code>")
    await callback.answer()


@router.message(AdminStates.grant_days)
async def do_grant(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await _grant(message.text or "", message, session, bot)


async def _grant(raw: str, reply_to: Message, session: AsyncSession, bot: Bot) -> None:
    parts = raw.split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].lstrip("-").isdigit():
        await reply_to.answer("Неверный формат. Пример: <code>123456789 30</code>")
        return

    user_id, days = int(parts[0]), int(parts[1])
    target = await session.get(User, user_id)
    if target is None:
        await reply_to.answer("Пользователь не найден — он должен сначала запустить бота.")
        return

    try:
        subscription = await subscriptions.issue_or_extend(
            session, target, days=days, traffic_gb=runtime.default_traffic_gb
        )
    except VpnPanelError as exc:
        await reply_to.answer(f"Панель недоступна: <code>{exc}</code>")
        return

    await reply_to.answer(f"✅ {target.title}: подписка до <b>{subscription.expires_at:%d.%m.%Y}</b>")
    try:
        await bot.send_message(
            user_id,
            f"🎁 Вам начислено <b>{days}</b> дн. подписки.\n"
            f"Доступ активен до <b>{subscription.expires_at:%d.%m.%Y}</b>.",
        )
    except TelegramAPIError:
        pass


@router.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    """Быстрая выдача: /give 123456789 30"""
    if not command.args:
        await message.answer("Формат: <code>/give user_id дни</code>")
        return
    await _grant(command.args, message, session, bot)


# ---------------------------------------------------------------- промокоды
@router.callback_query(ikb.AdminCB.filter(F.action == "promo_new"))
async def cb_promo_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.promo_new)
    await callback.message.answer(
        "Формат: <code>КОД скидка% бонус_дней макс_использований</code>\n"
        "Например: <code>SUMMER 20 3 100</code>\n"
        "Бонус и лимит можно опустить: <code>SUMMER 20</code>"
    )
    await callback.answer()


@router.message(AdminStates.promo_new)
async def do_promo_new(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Неверный формат.")
        return

    code = parts[0].upper()
    try:
        discount = int(parts[1])
        bonus = int(parts[2]) if len(parts) > 2 else 0
        max_uses = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        await message.answer("Числа заданы неверно.")
        return

    if await repo.get_promo(session, code):
        await message.answer("Такой промокод уже существует.")
        return

    session.add(
        PromoCode(code=code, discount_percent=discount, bonus_days=bonus, max_uses=max_uses)
    )
    await session.commit()
    await message.answer(
        f"✅ Промокод <b>{code}</b>: −{discount}%, +{bonus} дн., "
        f"лимит {max_uses or '∞'}"
    )


# ---------------------------------------------------------------- рассылка
@router.callback_query(ikb.AdminCB.filter(F.action == "broadcast"))
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast)
    await callback.message.answer("Отправьте сообщение для рассылки (поддерживается HTML).")
    await callback.answer()


@router.message(AdminStates.broadcast)
async def do_broadcast(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    text = message.html_text if message.text else None
    if not text:
        await message.answer("Нужно текстовое сообщение.")
        return

    user_ids = await repo.all_user_ids(session)
    await message.answer(f"Начинаю рассылку на {len(user_ids)} пользователей…")

    sent = blocked = failed = 0
    for index, user_id in enumerate(user_ids, 1):
        try:
            await bot.send_message(user_id, text, disable_web_page_preview=True)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError as exc:
            failed += 1
            log.warning("Рассылка %s: %s", user_id, exc)
        if index % 25 == 0:
            await asyncio.sleep(1)  # держимся в лимитах Telegram
        else:
            await asyncio.sleep(0.05)

    await message.answer(
        f"📣 Готово.\nДоставлено: <b>{sent}</b>\nЗаблокировали бота: <b>{blocked}</b>\n"
        f"Ошибок: <b>{failed}</b>"
    )


# ---------------------------------------------------------------- возврат звёзд
@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    """Возврат Telegram Stars: /refund user_id charge_id"""
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Формат: <code>/refund user_id charge_id</code>")
        return
    try:
        await bot.refund_star_payment(user_id=int(parts[0]), telegram_payment_charge_id=parts[1])
    except TelegramAPIError as exc:
        await message.answer(f"Не удалось: <code>{exc}</code>")
        return
    await message.answer("✅ Звёзды возвращены.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: <code>/ban user_id</code>")
        return
    target = await session.get(User, int(command.args.strip()))
    if target is None:
        await message.answer("Пользователь не найден.")
        return
    target.is_banned = not target.is_banned
    await session.commit()
    subscription = await repo.get_subscription(session, target.id)
    if subscription and target.is_banned:
        await subscriptions.disable(session, subscription)
    await message.answer(
        f"{'🚫 Заблокирован' if target.is_banned else '✅ Разблокирован'}: {target.title}"
    )
