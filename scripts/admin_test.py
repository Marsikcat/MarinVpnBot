"""Проверка админского интерфейса: редактор тарифов и редактор настроек.

Гоняет настоящие апдейты Telegram через Dispatcher с подставным Bot, ничего
не отправляя наружу. Работает на отдельной БД data/admin.db.

Запуск:  python scripts/admin_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
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

    print("\nПрава доступа")
    await asyncio.sleep(PAUSE)
    before = len(CALLS)
    await dp.feed_update(bot, click(ikb.PlanAdminCB(action="list").pack(), CLIENT))
    check(len(CALLS) == before, "обычный пользователь не открывает редактор тарифов")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/settings", CLIENT))
    check(len(CALLS) == before, "и не открывает настройки")

    await bot.session.close()
    print("\nАдминский интерфейс работает.")


if __name__ == "__main__":
    asyncio.run(main())
