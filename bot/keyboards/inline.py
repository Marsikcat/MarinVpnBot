"""Inline-клавиатуры и callback-данные."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.db.models import Plan
from bot.services.runtime import runtime
from bot.utils.links import clean_url


class PlanCB(CallbackData, prefix="plan"):
    plan_id: int


class PayCB(CallbackData, prefix="pay"):
    method: str
    plan_id: int


class CheckCB(CallbackData, prefix="check"):
    payment_id: int


class SbpCB(CallbackData, prefix="sbp"):
    """claim — «я оплатил» от пользователя; ok/no — решение администратора."""

    action: str
    payment_id: int


class MenuCB(CallbackData, prefix="menu"):
    action: str


class AdminCB(CallbackData, prefix="adm"):
    action: str
    value: str = ""


class PlanAdminCB(CallbackData, prefix="padm"):
    """Редактор тарифов: list | card | edit | toggle | ask_del | delete | new."""

    action: str
    plan_id: int = 0
    field: str = ""


class UsersCB(CallbackData, prefix="usr"):
    """Список пользователей: page | scope | export."""

    action: str
    page: int = 0
    scope: str = "all"


class SettingCB(CallbackData, prefix="setadm"):
    """Редактор настроек: list | edit | toggle."""

    action: str
    key: str = ""


def plans_kb(plans: Sequence[Plan]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        label = f"{plan.title} — {plan.price_rub:.0f} ₽"
        builder.row(InlineKeyboardButton(text=label, callback_data=PlanCB(plan_id=plan.id).pack()))
    return builder.as_markup()


def pay_methods_kb(plan: Plan) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    from bot.services.payments.registry import available_methods

    for code, title in available_methods():
        builder.row(InlineKeyboardButton(text=title, callback_data=PayCB(method=code, plan_id=plan.id).pack()))
    builder.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data=MenuCB(action="plans").pack()))
    return builder.as_markup()


def invoice_kb(pay_url: Optional[str], payment_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if pay_url:
        builder.row(InlineKeyboardButton(text="💰 Оплатить", url=pay_url))
    builder.row(
        InlineKeyboardButton(text="✅ Я оплатил", callback_data=CheckCB(payment_id=payment_id).pack())
    )
    builder.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data=MenuCB(action="plans").pack()))
    return builder.as_markup()


def sbp_invoice_kb(payment_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Я перевёл", callback_data=SbpCB(action="claim", payment_id=payment_id).pack()
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data=MenuCB(action="plans").pack()))
    return builder.as_markup()


def sbp_admin_kb(payment_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Деньги пришли", callback_data=SbpCB(action="ok", payment_id=payment_id).pack()
        ),
        InlineKeyboardButton(
            text="❌ Отклонить", callback_data=SbpCB(action="no", payment_id=payment_id).pack()
        ),
    )
    return builder.as_markup()


def access_kb(subscription_url: Optional[str] = None, has_keys: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📱 Приложения", callback_data=MenuCB(action="apps").pack()),
        InlineKeyboardButton(text="📖 Инструкция", callback_data=MenuCB(action="help").pack()),
    )
    if has_keys:
        builder.row(
            InlineKeyboardButton(text="🔑 Ключи по одному", callback_data=MenuCB(action="keys").pack())
        )
    builder.row(InlineKeyboardButton(text="💳 Продлить", callback_data=MenuCB(action="plans").pack()))
    if subscription_url:
        builder.row(
            InlineKeyboardButton(text="🔄 Обновить данные", callback_data=MenuCB(action="refresh").pack())
        )
    return builder.as_markup()


def profile_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Купить подписку", callback_data=MenuCB(action="plans").pack()))
    return builder.as_markup()


def support_kb() -> InlineKeyboardMarkup:
    """Кнопки показываем только для настоящих ссылок — заглушки из шаблона пропускаем."""
    builder = InlineKeyboardBuilder()
    support = clean_url(runtime.support_url)
    channel = clean_url(runtime.channel_url)
    if support:
        builder.row(InlineKeyboardButton(text="🆘 Написать в поддержку", url=support))
    if channel:
        builder.row(InlineKeyboardButton(text="📢 Наш канал", url=channel))
    return builder.as_markup()


def back_kb(action: str = "menu") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=MenuCB(action=action).pack()))
    return builder.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data=AdminCB(action="stats").pack()),
        InlineKeyboardButton(text="👥 Пользователи", callback_data=UsersCB(action="page").pack()),
    )
    builder.row(InlineKeyboardButton(text="🔍 Найти юзера", callback_data=AdminCB(action="find").pack()))
    builder.row(
        InlineKeyboardButton(text="💼 Тарифы", callback_data=PlanAdminCB(action="list").pack()),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data=SettingCB(action="list").pack()),
    )
    builder.row(InlineKeyboardButton(text="🎁 Выдать дни", callback_data=AdminCB(action="grant").pack()))
    if settings.pay_sbp_enabled:
        builder.row(
            InlineKeyboardButton(text="🏦 Заявки СБП", callback_data=AdminCB(action="claims").pack())
        )
    builder.row(InlineKeyboardButton(text="📣 Рассылка", callback_data=AdminCB(action="broadcast").pack()))
    builder.row(InlineKeyboardButton(text="🩺 Проверить панель", callback_data=AdminCB(action="health").pack()))
    return builder.as_markup()


SCOPE_TITLES = {"all": "Все", "active": "С подпиской", "inactive": "Без подписки"}


def users_kb(page: int, pages: int, scope: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=UsersCB(action="page", page=page - 1, scope=scope).pack()
            )
        )
    if pages > 1:
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{pages}", callback_data=UsersCB(action="page", page=page, scope=scope).pack()
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=UsersCB(action="page", page=page + 1, scope=scope).pack()
            )
        )
    if nav:
        builder.row(*nav)

    filters = [
        InlineKeyboardButton(
            text=("• " if code == scope else "") + title,
            callback_data=UsersCB(action="scope", page=0, scope=code).pack(),
        )
        for code, title in SCOPE_TITLES.items()
    ]
    builder.row(*filters)
    builder.row(
        InlineKeyboardButton(
            text="📤 Выгрузить CSV", callback_data=UsersCB(action="export", scope=scope).pack()
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="home").pack()))
    return builder.as_markup()


def plans_admin_kb(plans: Sequence[Plan]) -> InlineKeyboardMarkup:
    """Список тарифов в админке: видны цена, срок и признак активности."""
    builder = InlineKeyboardBuilder()
    for plan in plans:
        mark = "🟢" if plan.is_active else "🔴"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {plan.title} · {plan.price_rub:.0f} ₽ · {plan.days} дн.",
                callback_data=PlanAdminCB(action="card", plan_id=plan.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(text="➕ Новый тариф", callback_data=PlanAdminCB(action="new").pack())
    )
    builder.row(InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="home").pack()))
    return builder.as_markup()


def plan_card_kb(plan: Plan) -> InlineKeyboardMarkup:
    """Карточка тарифа: каждое поле правится отдельной кнопкой."""
    builder = InlineKeyboardBuilder()

    def field(text: str, name: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=PlanAdminCB(action="edit", plan_id=plan.id, field=name).pack()
        )

    builder.row(field("💰 Цена", "price_rub"), field("🏷 Название", "title"))
    builder.row(field("📅 Срок", "days"), field("📊 Трафик", "traffic_gb"))
    builder.row(field("📱 Устройств", "devices"), field("🔢 Порядок", "sort_order"))
    builder.row(
        InlineKeyboardButton(
            text="🔴 Выключить" if plan.is_active else "🟢 Включить",
            callback_data=PlanAdminCB(action="toggle", plan_id=plan.id).pack(),
        ),
        InlineKeyboardButton(
            text="🗑 Удалить", callback_data=PlanAdminCB(action="ask_del", plan_id=plan.id).pack()
        ),
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ К тарифам", callback_data=PlanAdminCB(action="list").pack())
    )
    return builder.as_markup()


def plan_delete_kb(plan_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🗑 Да, удалить", callback_data=PlanAdminCB(action="delete", plan_id=plan_id).pack()
        ),
        InlineKeyboardButton(
            text="⬅️ Отмена", callback_data=PlanAdminCB(action="card", plan_id=plan_id).pack()
        ),
    )
    return builder.as_markup()


def settings_admin_kb(fields, display) -> InlineKeyboardMarkup:
    """fields — список Field из bot.services.runtime, display(key) — текущее значение."""
    builder = InlineKeyboardBuilder()
    for field in fields:
        action = "toggle" if field.kind == "bool" else "edit"
        builder.row(
            InlineKeyboardButton(
                text=f"{field.title}: {display(field.key)}",
                callback_data=SettingCB(action=action, key=field.key).pack(),
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="home").pack()))
    return builder.as_markup()


def confirm_kb(action: str, value: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data=AdminCB(action=action, value=value).pack()),
        InlineKeyboardButton(text="❌ Отмена", callback_data=AdminCB(action="cancel").pack()),
    )
    return builder.as_markup()


def app_links_kb() -> InlineKeyboardMarkup:
    """Ссылки на клиенты в сторах."""
    rows: List[Tuple[str, str]] = [
        ("⭐️ Happ — iPhone, iPad, Mac", "https://apps.apple.com/app/happ-proxy-utility/id6504287215"),
        ("⭐️ Happ — Android", "https://play.google.com/store/apps/details?id=com.happproxy"),
        ("⭐️ Happ — Windows, Linux, macOS", "https://github.com/Happ-proxy/happ-desktop/releases/latest"),
        ("Запасной — Streisand (iPhone)", "https://apps.apple.com/app/streisand/id6450534064"),
        ("Запасной — v2rayNG (Android)", "https://play.google.com/store/apps/details?id=com.v2ray.ang"),
    ]
    builder = InlineKeyboardBuilder()
    for title, url in rows:
        builder.row(InlineKeyboardButton(text=title, url=url))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=MenuCB(action="access").pack()))
    return builder.as_markup()
