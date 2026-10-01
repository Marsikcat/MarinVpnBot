"""Проверка админского интерфейса: редакторы тарифов и настроек, карточка клиента.

Гоняет настоящие апдейты Telegram через Dispatcher с подставным Bot, ничего
не отправляя наружу. Работает на отдельной БД data/admin.db.

Запуск:  python scripts/admin_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = ROOT / "data" / "admin.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"
os.environ["VPN_PROVIDER"] = "mock"
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_IDS"] = "555"
os.environ["TRIAL_ENABLED"] = "true"
os.environ["TRIAL_DAYS"] = "3"
os.environ["DEFAULT_TRAFFIC_GB"] = "20"
os.environ["PAY_STARS_ENABLED"] = "true"
os.environ["PAY_SBP_ENABLED"] = "false"
if DB_FILE.exists():
    DB_FILE.unlink()

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.methods import TelegramMethod  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.session import init_db, session_factory  # noqa: E402
from bot.keyboards import inline as ikb  # noqa: E402
from bot.main import create_dispatcher  # noqa: E402
from bot.services.runtime import runtime  # noqa: E402

ADMIN = TgUser(id=555, is_bot=False, first_name="Админ", username="admin")
CLIENT = TgUser(id=900, is_bot=False, first_name="Клиент", username="client")
CALLS: list = []
_ids = iter(range(1, 10_000))
PAUSE = 0.45  # антифлуд-мидлварь пропускает не чаще одного события в 0.3–0.4 с


class FakeBot(Bot):
    async def __call__(self, method: TelegramMethod, request_timeout=None):
        CALLS.append((type(method).__name__, method.model_dump(exclude_none=True)))
        if type(method).__name__ == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="bot", username="testbot")
        return True


def message(text: str, who: TgUser = ADMIN) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=who.id, type="private"),
        from_user=who,
        text=text,
    )
    return Update(update_id=next(_ids), message=msg)


def click(data: str, who: TgUser = ADMIN) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=who.id, type="private"),
        from_user=who,
        text="панель",
    )
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=who, chat_instance="1", data=data, message=msg
        ),
    )


def last(method: str) -> dict:
    for name, payload in reversed(CALLS):
        if name == method:
            return payload
    return {}


def buttons(payload: dict) -> list:
    rows = payload.get("reply_markup", {}).get("inline_keyboard", [])
    return [b["text"] for row in rows for b in row]


def check(condition: bool, title: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + title)
    if not condition:
        print("      последние вызовы:", [n for n, _ in CALLS[-6:]])
        raise SystemExit(1)


async def check_client_card(dp, bot) -> None:
    """Карточка клиента: каждое действие меняет и панель, и базу."""
    print("\nКарточка клиента")
    from bot.db.models import SubscriptionStatus
    from bot.handlers.admin_users import local_time
    from bot.services import subscriptions
    from bot.services.vpn.factory import get_panel

    panel = get_panel()
    login = subscriptions.vpn_username(CLIENT.id)
    gb = 1024 ** 3

    async def press(action: str) -> None:
        await asyncio.sleep(PAUSE)
        await dp.feed_update(bot, click(ikb.ClientCB(action=action, user_id=CLIENT.id).pack()))

    async def send(text: str) -> None:
        await asyncio.sleep(PAUSE)
        await dp.feed_update(bot, message(text))

    async def current():
        async with session_factory() as session:
            return await repo.get_subscription(session, CLIENT.id), await repo.get_user(session, CLIENT.id)

    await send("/users")
    check(any(b.startswith("@client") for b in buttons(last("SendMessage"))), "в списке клиенты — кнопками")

    await send("/user @client")
    card = last("SendMessage")
    check("<code>900</code>" in card.get("text", "") and "активна" in card.get("text", ""),
          "/user открывает карточку с подпиской")
    wanted = {"➕ Дни", "📅 Дата окончания", "📊 Лимит трафика", "🔄 Сбросить трафик", "💳 Платежи", "✉️ Написать"}
    check(wanted <= set(buttons(card)), "все действия на месте")

    await send("/user i")
    check("Найдено: <b>2</b>" in last("SendMessage").get("text", ""), "несколько найденных — выбор кнопками")
    await send("/user nobody_here")
    check("Ничего не найдено" in last("SendMessage").get("text", ""), "ненайденный — понятный ответ")

    sub, _ = await current()
    before = sub.expires_at
    await press("days")
    check("Сколько дней" in last("SendMessage").get("text", ""), "«Дни» спрашивает число")
    await send("много")
    check("целое число" in last("SendMessage").get("text", ""), "ерунда отклонена, ввод ждёт дальше")
    await send("10")
    sub, _ = await current()
    check(sub.expires_at.date() == (before + timedelta(days=10)).date(), "+10 дней прибавились к остатку")
    check(panel._store[login].expires_at == sub.expires_at, "в панели тот же срок")
    check(sub.traffic_limit == 0, "лимит трафика не тронут")
    check(any(p.get("chat_id") == CLIENT.id and "начислено" in p.get("text", "") for n, p in CALLS if n == "SendMessage"),
          "клиент получил уведомление")
    check("Подписка" in last("SendMessage").get("text", ""), "после правки снова показана карточка")

    await press("date")
    await send("32.13.2030")
    check("Не понял дату" in last("SendMessage").get("text", ""), "неверная дата отклонена")
    await send("31.12.2030")
    sub, _ = await current()
    check(local_time(sub.expires_at).startswith("31.12.2030 23:59"), "дата выставлена — до конца дня")
    check(panel._store[login].expires_at == sub.expires_at, "и в панели")

    await press("date")
    await send("/cancel")
    await send("01.01.2031")
    sub, _ = await current()
    check(local_time(sub.expires_at).startswith("31.12.2030"), "после /cancel ввод не применяется")

    await press("traffic")
    check("безлимит" in last("SendMessage").get("text", ""), "«Лимит трафика» показывает текущий")
    await send("50")
    sub, _ = await current()
    check(sub.traffic_limit == 50 * gb and panel._store[login].data_limit == 50 * gb, "лимит 50 ГБ в базе и панели")
    check(local_time(sub.expires_at).startswith("31.12.2030"), "срок при этом не тронут")

    panel._store[login].used_traffic = 7 * gb
    await press("reset")
    check(panel._store[login].used_traffic == 0, "трафик сброшен в панели")

    await press("suspend")
    sub, _ = await current()
    check(not panel._store[login].enabled and sub.status == SubscriptionStatus.disabled, "доступ приостановлен")
    check("▶️ Возобновить доступ" in buttons(last("EditMessageText")), "в карточке появилась «Возобновить»")
    await press("resume")
    sub, _ = await current()
    check(panel._store[login].enabled and sub.status == SubscriptionStatus.active, "доступ возобновлён")

    _, user = await current()
    trial_before = user.trial_used
    await press("trial")
    _, user = await current()
    check(user.trial_used != trial_before, "отметку о триале можно переключить")
    await press("trial")

    await press("ban")
    _, user = await current()
    check(user.is_banned and not panel._store[login].enabled, "бан из карточки отключает доступ")
    check("в бане" in last("EditMessageText").get("text", ""), "карточка показывает бан")
    await press("ban")
    _, user = await current()
    check(not user.is_banned and panel._store[login].enabled, "разбан возвращает доступ")

    await press("msg")
    await send("Привет, это тест")
    check(any(p.get("chat_id") == CLIENT.id and "Привет, это тест" in p.get("text", "")
              for n, p in CALLS if n == "SendMessage"), "сообщение ушло клиенту от имени бота")

    await press("pays")
    payments = last("EditMessageText").get("text", "")
    check("Платежи" in payments and "⏳ ждёт" in payments, "список платежей со статусами")


async def main() -> None:
    await init_db()
    bot = FakeBot(token="123456:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()
    async with session_factory() as session:
        await runtime.load(session)

    print("Админ-панель")
    await dp.feed_update(bot, message("/start", ADMIN))
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/admin"))
    check("💼 Тарифы" in buttons(last("SendMessage")), "в панели есть раздел «Тарифы»")
    check("⚙️ Настройки" in buttons(last("SendMessage")), "в панели есть раздел «Настройки»")

    print("\nРедактор тарифов")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="list").pack()))
    check(len(buttons(last("EditMessageText"))) == 6, "список из 4 тарифов + создать + назад")

    async with session_factory() as session:
        plan = (await repo.list_plans(session))[0]
        plan_id, old_price = plan.id, plan.price_rub

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="card", plan_id=plan_id).pack()))
    check("💰 Цена" in buttons(last("EditMessageText")), "карточка тарифа открылась")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(
        bot, click(ikb.PlanAdminCB(action="edit", plan_id=plan_id, field="price_rub").pack())
    )
    check("Сейчас" in last("SendMessage").get("text", ""), "бот показал текущую цену")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("не число"))
    check("число" in last("SendMessage").get("text", ""), "нечисловая цена отклонена")
    async with session_factory() as session:
        check((await repo.get_plan(session, plan_id)).price_rub == old_price, "цена не изменилась")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("249"))
    async with session_factory() as session:
        check((await repo.get_plan(session, plan_id)).price_rub == 249, "новая цена сохранена")
    check("249 ₽" in last("SendMessage").get("text", ""), "карточка показывает новую цену")

    # клиент сразу видит изменённую цену
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/start", CLIENT))
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/buy", CLIENT))
    check(any("249 ₽" in b for b in buttons(last("SendMessage"))), "клиент видит новую цену сразу")

    print("\nСкрытие тарифа")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="toggle", plan_id=plan_id).pack()))
    async with session_factory() as session:
        check(not (await repo.get_plan(session, plan_id)).is_active, "тариф выключен")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/buy", CLIENT))
    check(
        not any("249 ₽" in b for b in buttons(last("SendMessage"))),
        "выключенный тариф исчез из витрины",
    )
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="toggle", plan_id=plan_id).pack()))
    async with session_factory() as session:
        check((await repo.get_plan(session, plan_id)).is_active, "тариф включён обратно")

    print("\nСоздание и удаление")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="new").pack()))
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("Неделя | 7 | 59"))
    async with session_factory() as session:
        created = [p for p in await repo.list_plans(session) if p.title == "Неделя"]
        check(len(created) == 1 and created[0].days == 7, "новый тариф создан")
        new_id = created[0].id

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="delete", plan_id=new_id).pack()))
    async with session_factory() as session:
        check(await repo.get_plan(session, new_id) is None, "тариф без платежей удалён")

    # тариф с историей платежей удалять нельзя
    async with session_factory() as session:
        plan = await repo.get_plan(session, plan_id)
        await repo.create_payment(session, CLIENT.id, plan, provider="stars", amount=249)
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="delete", plan_id=plan_id).pack()))
    async with session_factory() as session:
        check(await repo.get_plan(session, plan_id) is not None, "тариф с платежами не удалён")
    check("платежи" in last("AnswerCallbackQuery").get("text", ""), "объяснено, почему нельзя")

    print("\nРедактор настроек")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.SettingCB(action="list").pack()))
    labels = buttons(last("EditMessageText"))
    check(any(b.startswith("Лимит трафика по умолчанию") and b.endswith(": 20") for b in labels), "видно текущее значение из .env")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.SettingCB(action="edit", key="default_traffic_gb").pack()))
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("999999"))
    check("Допустимо" in last("SendMessage").get("text", ""), "значение вне диапазона отклонено")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("35"))
    check(runtime.default_traffic_gb == 35, "значение изменено в рантайме")
    async with session_factory() as session:
        check(await repo.get_setting(session, "default_traffic_gb") == "35", "значение записано в БД")

    # значение переживает перезапуск: новый объект Runtime читает его из БД
    from bot.services.runtime import Runtime

    fresh = Runtime()
    async with session_factory() as session:
        await fresh.load(session)
    check(fresh.default_traffic_gb == 35, "после перезапуска значение берётся из БД")

    print("\nПереключатели и отмена")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.SettingCB(action="toggle", key="trial_enabled").pack()))
    check(runtime.trial_enabled is False, "пробный период выключен кнопкой")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/trial", CLIENT))
    check("недоступен" in last("SendMessage").get("text", ""), "клиенту триал больше не выдаётся")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.SettingCB(action="toggle", key="trial_enabled").pack()))
    check(runtime.trial_enabled is True, "триал включён обратно")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(
        bot, click(ikb.PlanAdminCB(action="edit", plan_id=plan_id, field="days").pack())
    )
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/cancel"))
    check("Отменено" in last("SendMessage").get("text", ""), "/cancel выходит из редактора")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("777"))
    async with session_factory() as session:
        check((await repo.get_plan(session, plan_id)).days != 777, "после отмены ввод не применяется")

    print("\nСписок пользователей")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/users"))
    listing = last("SendMessage").get("text", "")
    check("Пользователи" in listing, "список открылся")
    check("client" in listing and "admin" in listing, "видно обоих пользователей")
    check("Всего: <b>2</b>" in listing, "показано общее число")
    labels = buttons(last("SendMessage"))
    check("📤 Выгрузить CSV" in labels, "есть выгрузка в CSV")
    check(any("С подпиской" in b for b in labels), "есть фильтры")

    # выдаём подписку клиенту, чтобы фильтрам было что различать
    async with session_factory() as session:
        from bot.services import subscriptions

        client = await repo.get_user(session, CLIENT.id)
        await subscriptions.issue_or_extend(session, client, days=30)

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.UsersCB(action="scope", page=0, scope="active").pack()))
    filtered = last("EditMessageText").get("text", "")
    check("Всего: <b>1</b>" in filtered, "фильтр «с подпиской» отобрал одного")
    check("client" in filtered and "@admin" not in filtered, "в выборке только клиент")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.UsersCB(action="scope", page=0, scope="inactive").pack()))
    check("Всего: <b>1</b>" in last("EditMessageText").get("text", ""), "фильтр «без подписки» работает")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.UsersCB(action="export", scope="all").pack()))
    document = last("SendDocument")
    check(bool(document), "CSV отправлен файлом")
    check("2 пользователей" in document.get("caption", ""), "в подписи число выгруженных")

    await check_client_card(dp, bot)

    print("\nПрава доступа")
    await asyncio.sleep(PAUSE)
    before = len(CALLS)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="list").pack(), CLIENT))
    check(len(CALLS) == before, "обычный пользователь не открывает редактор тарифов")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/settings", CLIENT))
    check(len(CALLS) == before, "и не открывает настройки")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.ClientCB(action="days", user_id=CLIENT.id).pack(), CLIENT))
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message(f"/user {CLIENT.id}", CLIENT))
    check(len(CALLS) == before, "и не открывает карточки клиентов")

    await bot.session.close()
    print("\nАдминский интерфейс работает.")


if __name__ == "__main__":
    asyncio.run(main())
