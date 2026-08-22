"""Проверка продления подписки и лимитов трафика.

Гоняет апдейты через Dispatcher с подставным Bot, работает на data/renew.db.

Запуск:  python scripts/renew_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = ROOT / "data" / "renew.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"
os.environ["VPN_PROVIDER"] = "mock"
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_IDS"] = "555"
os.environ["TRIAL_ENABLED"] = "false"
os.environ["PAY_STARS_ENABLED"] = "true"
os.environ["PAY_SBP_ENABLED"] = "false"
os.environ["DEFAULT_TRAFFIC_GB"] = "0"
if DB_FILE.exists():
    DB_FILE.unlink()

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.methods import TelegramMethod  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.models import utcnow  # noqa: E402
from bot.db.session import init_db, session_factory  # noqa: E402
from bot.keyboards import inline as ikb  # noqa: E402
from bot.main import create_dispatcher  # noqa: E402
from bot.services import subscriptions  # noqa: E402
from bot.services.runtime import runtime  # noqa: E402

ADMIN = TgUser(id=555, is_bot=False, first_name="Админ", username="admin")
USER = TgUser(id=8100, is_bot=False, first_name="Клиент", username="client")
CALLS: list = []
_ids = iter(range(1, 10_000))
PAUSE = 0.45
GB = 1024 ** 3


class FakeBot(Bot):
    async def __call__(self, method: TelegramMethod, request_timeout=None):
        CALLS.append((type(method).__name__, method.model_dump(exclude_none=True)))
        if type(method).__name__ == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="bot", username="testbot")
        return True


def message(text: str, who: TgUser = USER) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=who.id, type="private"),
        from_user=who,
        text=text,
    )
    return Update(update_id=next(_ids), message=msg)


def click(data: str, who: TgUser = USER) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=who.id, type="private"),
        from_user=who,
        text="меню",
    )
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=who, chat_instance="1", data=data, message=msg
        ),
    )


def all_texts() -> list:
    """Тексты сообщений и подписи к фото — подтверждения теперь приходят с QR-кодом."""
    out = []
    for name, payload in CALLS:
        if name == "SendMessage":
            out.append(payload.get("text", ""))
        elif name == "SendPhoto":
            out.append(payload.get("caption", ""))
    return out


def last(method: str) -> dict:
    for name, payload in reversed(CALLS):
        if name == method:
            return payload
    return {}


def check(condition: bool, title: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + title)
    if not condition:
        print("      последние вызовы:", [n for n, _ in CALLS[-6:]])
        raise SystemExit(1)


async def pay(dp, bot, plan_id: int) -> None:
    """Полный цикл оплаты звёздами через хендлеры."""
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PayCB(method="stars", plan_id=plan_id).pack()))
    async with session_factory() as session:
        payment = (await repo.pending_payments(session, "stars"))[-1]
        payment_id = payment.id

    from aiogram.types import SuccessfulPayment

    sp = SuccessfulPayment(
        currency="XTR",
        total_amount=100,
        invoice_payload=f"sub:{payment_id}",
        telegram_payment_charge_id=f"charge_{payment_id}",
        provider_payment_charge_id=f"prov_{payment_id}",
    )
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        successful_payment=sp,
    )
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))


async def main() -> None:
    await init_db()
    bot = FakeBot(token="123456:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()
    async with session_factory() as session:
        await runtime.load(session)
        plan = (await repo.list_plans(session))[0]
        plan.traffic_gb = 50  # проверяем, что лимит тарифа доезжает до подписки
        await session.commit()
        plan_id, plan_days = plan.id, plan.days

    await dp.feed_update(bot, message("/start"))

    print("Первая покупка")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/buy"))
    check("Выберите тариф" in last("SendMessage").get("text", ""), "витрина в режиме покупки")

    await pay(dp, bot, plan_id)
    texts = all_texts()
    check(any("Оплата получена" in t for t in texts), "подтверждение первой оплаты")
    check(any("Ссылка-подписка" in t for t in texts), "сразу после оплаты пришла ссылка-подписка")
    check(any(n == "SendPhoto" for n, _ in CALLS), "сразу после оплаты пришёл QR-код")
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        first_until = sub.expires_at
        check(sub.is_active, "подписка активна")
        check(abs((sub.expires_at - utcnow()).days - plan_days) <= 1, "срок = длительности тарифа")
        check(sub.traffic_limit == 50 * GB, "лимит трафика из тарифа применён")

    print("\nПродление активной подписки")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/renew"))
    header = last("SendMessage").get("text", "")
    check("Продление подписки" in header, "витрина открылась как продление")
    check(f"{first_until:%d.%m.%Y}" in header, "показана текущая дата окончания")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PlanCB(plan_id=plan_id).pack()))
    card = last("EditMessageText").get("text", "")
    expected = f"{first_until + timedelta(days=plan_days):%d.%m.%Y}"
    check(expected in card, f"в карточке видна будущая дата {expected}")

    await pay(dp, bot, plan_id)
    texts = all_texts()
    check(any("Подписка продлена" in t for t in texts), "сообщение о продлении, а не о покупке")
    check(any(f"{first_until:%d.%m.%Y}" in t and "Было до" in t for t in texts),
          "в сообщении видно прежнюю дату")
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(sub.expires_at.date() == (first_until + timedelta(days=plan_days)).date(),
              "дни прибавились к остатку, а не обнулили его")
        second_until = sub.expires_at

    print("\nВозобновление после истечения")
    from bot.services.vpn.factory import get_panel

    # просрочиваем и в БД, и в панели — в жизни это одно и то же состояние
    expired_at = utcnow() - timedelta(days=5)
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        sub.expires_at = expired_at
        await session.commit()
    get_panel()._store[subscriptions.vpn_username(USER.id)].expires_at = expired_at
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/renew"))
    check("Возобновление подписки" in last("SendMessage").get("text", ""),
          "для истёкшей подписки другой заголовок")

    await pay(dp, bot, plan_id)
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(abs((sub.expires_at - utcnow()).days - plan_days) <= 1,
              "истёкшая подписка отсчитывается заново от сегодня")
        check(sub.is_active, "доступ снова активен")

    print("\nЛимит трафика по умолчанию")
    async with session_factory() as session:
        await runtime.set(session, "default_traffic_gb", "25")
    await dp.feed_update(bot, message("/start", ADMIN))
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message(f"/give {USER.id} 10", ADMIN))
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(sub.traffic_limit == 25 * GB, "ручная выдача применила лимит по умолчанию")

    async with session_factory() as session:
        new_plan = await repo.create_plan(
            session, title="Тест", days=7, price_rub=99, traffic_gb=runtime.default_traffic_gb
        )
        check(new_plan.traffic_gb == 25, "новый тариф создан с лимитом по умолчанию")

    print("\nКлючи не меняются при продлении")
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        links_before = subscriptions.parse_links(sub)
        url_before = sub.subscription_url
    await pay(dp, bot, plan_id)
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(subscriptions.parse_links(sub) == links_before, "ключи прежние")
        check(sub.subscription_url == url_before, "ссылка-подписка прежняя")

    print("\nПанель — источник правды")
    from bot.services.vpn.factory import get_panel

    panel = get_panel()
    username = subscriptions.vpn_username(USER.id)

    # админ вручную продлил клиента в панели — бот должен это увидеть
    panel_until = utcnow() + timedelta(days=200)
    panel._store[username].expires_at = panel_until
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/access"))
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(sub.expires_at.date() == panel_until.date(), "срок подтянут из панели")

    # и продление считается уже от панельной даты, а не от старой записи в БД
    await pay(dp, bot, plan_id)
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(sub.expires_at.date() == (panel_until + timedelta(days=plan_days)).date(),
              "продление отсчитано от даты из панели")

    # клиента отключили в панели
    panel._store[username].enabled = False
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/access"))
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(sub.status.value == "disabled", "отключение в панели отражено в боте")
    panel._store[username].enabled = True

    # клиента удалили из панели
    await panel.delete(username)
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message("/access"))
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        check(sub.status.value == "expired", "удаление из панели отражено в боте")

    await bot.session.close()
    print("\nПродление, лимиты и сверка с панелью работают.")


if __name__ == "__main__":
    asyncio.run(main())
