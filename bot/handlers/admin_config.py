"""Редакторы тарифов и настроек внутри бота (раздел /admin)."""
from __future__ import annotations

import logging
from typing import Dict, Union

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Plan
from bot.keyboards import inline as ikb
from bot.services.runtime import FIELDS, Field, ValidationError, parse, runtime
from bot.states import AdminStates
from bot.texts import ru

log = logging.getLogger(__name__)
router = Router(name="admin_config")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = event.from_user
        return bool(user and settings.is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


# ---------------------------------------------------------------- тарифы
PLAN_FIELDS: Dict[str, Field] = {
    "title": Field("title", "Название", "str"),
    "price_rub": Field("price_rub", "Цена, ₽", "float", maximum=1_000_000),
    "days": Field("days", "Срок, дней", "int", minimum=1, maximum=3650),
    "traffic_gb": Field("traffic_gb", "Трафик, ГБ", "int", "0 — безлимит", maximum=100_000),
    "devices": Field("devices", "Устройств", "int", minimum=1, maximum=100),
    "sort_order": Field("sort_order", "Порядок в списке", "int", "меньше — выше", maximum=999),
}

PLANS_HEADER = (
    "💼 <b>Тарифы</b>\n\n"
    "Нажмите на тариф, чтобы изменить цену, срок или отключить его.\n"
    "Изменения видны пользователям сразу, перезапуск не нужен."
)


async def _plan_card(session: AsyncSession, plan: Plan) -> str:
    sales = await repo.count_plan_sales(session, plan.id)
    traffic = "безлимит" if not plan.traffic_gb else f"{plan.traffic_gb} ГБ"
    per_month = f" (~{plan.price_per_month:.0f} ₽/мес)" if plan.days >= 60 else ""
    return (
        f"💼 <b>{plan.title}</b>\n\n"
        f"Цена: <b>{plan.price_rub:.0f} ₽</b>{per_month}\n"
        f"Срок: <b>{ru.plural_days(plan.days)}</b>\n"
        f"Трафик: <b>{traffic}</b>\n"
        f"Устройств: <b>{plan.devices}</b>\n"
        f"Статус: <b>{'🟢 активен' if plan.is_active else '🔴 скрыт'}</b>\n"
        f"Порядок: {plan.sort_order} · код <code>{plan.code}</code>\n\n"
        f"Оплачен раз: <b>{sales}</b>"
    )


async def _show_plans(target: Union[Message, CallbackQuery], session: AsyncSession) -> None:
    plans = await repo.list_plans(session, only_active=False)
    markup = ikb.plans_admin_kb(plans)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(PLANS_HEADER, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(PLANS_HEADER, reply_markup=markup)


@router.message(Command("plans"))
async def cmd_plans(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await _show_plans(message, session)


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "list"))
async def cb_plans(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await _show_plans(callback, session)


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "card"))
async def cb_plan_card(
    callback: CallbackQuery, callback_data: ikb.PlanAdminCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    plan = await repo.get_plan(session, callback_data.plan_id)
    if plan is None:
        await callback.answer("Тариф не найден", show_alert=True)
        await _show_plans(callback, session)
        return
    await callback.message.edit_text(await _plan_card(session, plan), reply_markup=ikb.plan_card_kb(plan))
    await callback.answer()


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "edit"))
async def cb_plan_edit(
    callback: CallbackQuery, callback_data: ikb.PlanAdminCB, session: AsyncSession, state: FSMContext
) -> None:
    plan = await repo.get_plan(session, callback_data.plan_id)
    field = PLAN_FIELDS.get(callback_data.field)
    if plan is None or field is None:
        await callback.answer("Не найдено", show_alert=True)
        return

    await state.set_state(AdminStates.plan_field)
    await state.update_data(plan_id=plan.id, field=field.key)
    current = getattr(plan, field.key)
    hint = f"\n<i>{field.hint}</i>" if field.hint else ""
    await callback.message.answer(
        f"Тариф «{plan.title}» → <b>{field.title}</b>\n"
        f"Сейчас: <b>{current}</b>{hint}\n\n"
        "Отправьте новое значение или /cancel."
    )
    await callback.answer()


@router.message(AdminStates.plan_field)
async def do_plan_edit(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    plan = await repo.get_plan(session, int(data.get("plan_id", 0)))
    field = PLAN_FIELDS.get(str(data.get("field", "")))
    if plan is None or field is None:
        await state.clear()
        await message.answer("Тариф не найден.")
        return

    try:
        value = parse(field, message.text or "")
    except ValidationError as exc:
        await message.answer(f"{exc} Попробуйте ещё раз или /cancel.")
        return

    setattr(plan, field.key, value)
    await session.commit()
    await state.clear()
    log.info("Тариф %s: %s = %r (админ %s)", plan.code, field.key, value, message.from_user.id)

    await message.answer(await _plan_card(session, plan), reply_markup=ikb.plan_card_kb(plan))


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "toggle"))
async def cb_plan_toggle(
    callback: CallbackQuery, callback_data: ikb.PlanAdminCB, session: AsyncSession
) -> None:
    plan = await repo.get_plan(session, callback_data.plan_id)
    if plan is None:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    plan.is_active = not plan.is_active
    await session.commit()
    await callback.message.edit_text(await _plan_card(session, plan), reply_markup=ikb.plan_card_kb(plan))
    await callback.answer("Тариф показан клиентам" if plan.is_active else "Тариф скрыт из списка")


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "ask_del"))
async def cb_plan_ask_delete(
    callback: CallbackQuery, callback_data: ikb.PlanAdminCB, session: AsyncSession
) -> None:
    plan = await repo.get_plan(session, callback_data.plan_id)
    if plan is None:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    if await repo.plan_payments_exist(session, plan.id):
        # удаление испортило бы историю платежей — предлагаем скрыть
        await callback.answer(
            "По этому тарифу есть платежи. Его нельзя удалить, чтобы не испортить статистику — "
            "используйте «Выключить»: он исчезнет из списка у клиентов.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        f"Удалить тариф <b>{plan.title}</b>?\nДействие необратимо.",
        reply_markup=ikb.plan_delete_kb(plan.id),
    )
    await callback.answer()


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "delete"))
async def cb_plan_delete(
    callback: CallbackQuery, callback_data: ikb.PlanAdminCB, session: AsyncSession
) -> None:
    plan = await repo.get_plan(session, callback_data.plan_id)
    if plan is None:
        await callback.answer("Уже удалён", show_alert=True)
        await _show_plans(callback, session)
        return
    if await repo.plan_payments_exist(session, plan.id):
        await callback.answer("По тарифу есть платежи — удаление запрещено", show_alert=True)
        return

    title = plan.title
    await repo.delete_plan(session, plan)
    log.info("Тариф %s удалён админом %s", title, callback.from_user.id)
    await _show_plans(callback, session)
    await callback.answer(f"Тариф «{title}» удалён")


@router.callback_query(ikb.PlanAdminCB.filter(F.action == "new"))
async def cb_plan_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.plan_new)
    await callback.message.answer(
        "Новый тариф одной строкой:\n"
        "<code>Название | дней | цена</code>\n\n"
        "Например: <code>Неделя | 7 | 59</code>\n"
        "Трафик и число устройств настроите в карточке. Отмена — /cancel."
    )
    await callback.answer()


@router.message(AdminStates.plan_new)
async def do_plan_new(message: Message, session: AsyncSession, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) != 3:
        await message.answer("Нужны три части через «|». Пример: <code>Неделя | 7 | 59</code>")
        return

    try:
        title = parse(PLAN_FIELDS["title"], parts[0])
        days = parse(PLAN_FIELDS["days"], parts[1])
        price = parse(PLAN_FIELDS["price_rub"], parts[2])
    except ValidationError as exc:
        await message.answer(f"{exc} Попробуйте ещё раз или /cancel.")
        return

    plan = await repo.create_plan(
        session, title=title, days=days, price_rub=price, traffic_gb=runtime.default_traffic_gb
    )
    await state.clear()
    log.info("Создан тариф %s (%s) админом %s", plan.title, plan.code, message.from_user.id)
    await message.answer(await _plan_card(session, plan), reply_markup=ikb.plan_card_kb(plan))


# ---------------------------------------------------------------- настройки
SETTINGS_HEADER = (
    "⚙️ <b>Настройки</b>\n\n"
    "Меняются на лету и сохраняются в базе.\n"
    "Токены, доступы к панели и способы оплаты — в файле <code>.env</code> "
    "(там же нужен перезапуск бота)."
)


async def _show_settings(target: Union[Message, CallbackQuery]) -> None:
    markup = ikb.settings_admin_kb(FIELDS, runtime.display)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(SETTINGS_HEADER, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(SETTINGS_HEADER, reply_markup=markup)


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_settings(message)


@router.callback_query(ikb.SettingCB.filter(F.action == "list"))
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_settings(callback)


@router.callback_query(ikb.SettingCB.filter(F.action == "toggle"))
async def cb_setting_toggle(
    callback: CallbackQuery, callback_data: ikb.SettingCB, session: AsyncSession
) -> None:
    try:
        value = await runtime.toggle(session, callback_data.key)
    except ValidationError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(SETTINGS_HEADER, reply_markup=ikb.settings_admin_kb(FIELDS, runtime.display))
    await callback.answer("Включено" if value else "Выключено")


@router.callback_query(ikb.SettingCB.filter(F.action == "edit"))
async def cb_setting_edit(
    callback: CallbackQuery, callback_data: ikb.SettingCB, state: FSMContext
) -> None:
    field = runtime.field(callback_data.key)
    if field is None:
        await callback.answer("Настройка не найдена", show_alert=True)
        return

    await state.set_state(AdminStates.setting_value)
    await state.update_data(key=field.key)
    hint = f"\n<i>{field.hint}</i>" if field.hint else ""
    await callback.message.answer(
        f"<b>{field.title}</b>\nСейчас: <b>{runtime.display(field.key)}</b>{hint}\n\n"
        "Отправьте новое значение или /cancel."
    )
    await callback.answer()


@router.message(AdminStates.setting_value)
async def do_setting_edit(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    key = str(data.get("key", ""))
    try:
        await runtime.set(session, key, message.text or "")
    except ValidationError as exc:
        await message.answer(f"{exc} Попробуйте ещё раз или /cancel.")
        return

    await state.clear()
    field = runtime.field(key)
    await message.answer(
        f"✅ {field.title}: <b>{runtime.display(key)}</b>",
        reply_markup=ikb.settings_admin_kb(FIELDS, runtime.display),
    )
