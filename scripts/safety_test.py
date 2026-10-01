"""Гонки и защитные проверки: одновременные действия, отключение просрочки,
бан и разбан, ошибки обработчиков, проверки перед оплатой.

Запуск:  python scripts/safety_test.py
Работает на data/safety.db и mock-панели — реальные сервисы не трогает.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = ROOT / "data" / "safety.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"
os.environ["VPN_PROVIDER"] = "mock"
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_IDS"] = "555"
os.environ["TRIAL_ENABLED"] = "true"
os.environ["TRIAL_DAYS"] = "3"
os.environ["TRIAL_TRAFFIC_GB"] = "10"
os.environ["DEFAULT_TRAFFIC_GB"] = "0"
os.environ["PAY_STARS_ENABLED"] = "true"
os.environ["PAY_SBP_ENABLED"] = "true"
os.environ["SBP_PHONE"] = "+7 900 000-00-00"
os.environ["PAY_YOOKASSA_ENABLED"] = "false"
os.environ["PAY_CRYPTOBOT_ENABLED"] = "false"
os.environ["REQUIRED_CHANNEL_ID"] = ""
for suffix in ("", "-wal", "-shm"):
    Path(f"{DB_FILE}{suffix}").unlink(missing_ok=True)

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.methods import TelegramMethod  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User as TgUser,
)

from bot import tasks  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.models import PaymentStatus, SubscriptionStatus, User, utcnow  # noqa: E402
from bot.db.session import init_db, session_factory  # noqa: E402
from bot.keyboards import inline as ikb  # noqa: E402
from bot.main import create_dispatcher  # noqa: E402
from bot.services import subscriptions  # noqa: E402
from bot.services.runtime import runtime  # noqa: E402
from bot.services.vpn.factory import get_panel  # noqa: E402
from bot.texts import ru  # noqa: E402
from bot.utils.render import plans_header, render_access  # noqa: E402

ADMIN = TgUser(id=555, is_bot=False, first_name="Админ", username="admin")
USER = TgUser(id=9100, is_bot=False, first_name="Клиент", username="client")
CALLS: list = []
_ids = iter(range(1, 100_000))
PAUSE = 0.45
GB = 1024 ** 3


class FakeBot(Bot):
    async def __call__(self, method: TelegramMethod, request_timeout=None):
        CALLS.append((type(method).__name__, method.model_dump(exclude_none=True)))
        if type(method).__name__ == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="bot", username="testbot")
        return True


def _msg(who: TgUser, **fields) -> Message:
    return Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=who.id, type="private"),
        from_user=who,
        **fields,
    )


def message(text: str, who: TgUser = USER) -> Update:
    return Update(update_id=next(_ids), message=_msg(who, text=text))


def click(data: str, who: TgUser = USER) -> Update:
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=who, chat_instance="1", data=data, message=_msg(who, text="меню")
        ),
    )


def calls_since(mark: int, method: str) -> list:
    return [payload for name, payload in CALLS[mark:] if name == method]


def check(condition: bool, title: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + title)
    if not condition:
        print("      последние вызовы:", [n for n, _ in CALLS[-6:]])
        raise SystemExit(1)


def count_creates(panel):
    """Считает обращения к панели на выдачу и растягивает их, чтобы вызовы пересеклись."""
    counter = {"n": 0}
    original = panel.create_or_update

    async def slow_create(*args, **kwargs):
        counter["n"] += 1
        await asyncio.sleep(0.2)
        return await original(*args, **kwargs)

    panel.create_or_update = slow_create
    return counter


async def subscription_of(user_id: int):
    async with session_factory() as session:
        return await repo.get_subscription(session, user_id)


async def main() -> None:
    await init_db()
    bot = FakeBot(token="123456:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()
    panel = get_panel()
    username = subscriptions.vpn_username(USER.id)
    async with session_factory() as session:
        await runtime.load(session)
        plan = (await repo.list_plans(session))[0]
        plan_id, plan_days = plan.id, plan.days

    await dp.feed_update(bot, message("/start"))
    await dp.feed_update(bot, message("/start", ADMIN))

    print("Пробный период: два нажатия одновременно")
    counter = count_creates(panel)
    async with session_factory() as s1, session_factory() as s2:
        u1, u2 = await s1.get(User, USER.id), await s2.get(User, USER.id)
        results = await asyncio.gather(subscriptions.grant_trial(s1, u1), subscriptions.grant_trial(s2, u2))
    del panel.create_or_update
    check(sum(r is not None for r in results) == 1, "триал выдан один раз, второе нажатие получило отказ")
    check(counter["n"] == 1, "в панель ушла одна выдача")
    sub = await subscription_of(USER.id)
    check(abs((sub.expires_at - utcnow()).days - runtime.trial_days) <= 1, "срок триала не удвоен")

    print("\nПлатёж подтверждают дважды одновременно")
    before = (await subscription_of(USER.id)).expires_at
    async with session_factory() as session:
        payment = await repo.create_payment(session, USER.id, plan, provider="sbp", amount=149, days=plan_days)
        payment_id = payment.id
    counter = count_creates(panel)
    async with session_factory() as s1, session_factory() as s2:
        p1, p2 = await repo.get_payment(s1, payment_id), await repo.get_payment(s2, payment_id)
        await asyncio.gather(
            subscriptions.complete_payment(s1, bot, p1), subscriptions.complete_payment(s2, bot, p2)
        )
    del panel.create_or_update
    check(counter["n"] == 1, "платёж проведён один раз")
    sub = await subscription_of(USER.id)
    check(sub.expires_at.date() == (before + timedelta(days=plan_days)).date(), "дни прибавлены один раз")

    print("\n3x-ui: две выдачи одновременно")
    from bot.services.vpn.xui import XuiPanel

    xui = XuiPanel()
    store = {"inbounds": [{"id": 1, "protocol": "vless", "settings": {"clients": []}, "clientStats": []}]}

    async def fake_fetch():
        await asyncio.sleep(0.05)
        return copy.deepcopy(store["inbounds"])

    async def fake_save(inbound):
        await asyncio.sleep(0.05)  # пока пишем, соседняя выдача успела бы прочитать старое
        store["inbounds"] = [inbound if i["id"] == inbound["id"] else i for i in store["inbounds"]]

    xui._fetch_inbounds = fake_fetch
    xui._save_inbound = fake_save
    until = utcnow() + timedelta(days=30)
    await asyncio.gather(xui.create_or_update("tgA", until), xui.create_or_update("tgB", until))
    emails = {c["email"] for c in store["inbounds"][0]["settings"]["clients"]}
    check(emails == {"tgA", "tgB"}, "оба клиента в панели, никто не затёрт")

    print("\nОтключение просрочки сверяется с панелью")
    manual_until = utcnow() + timedelta(days=20)
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        sub.expires_at = utcnow() - timedelta(hours=1)  # в базе срок вышел…
        await session.commit()
    panel._store[username].expires_at = manual_until  # …а в панели админ продлил руками
    mark = len(CALLS)
    await tasks.deactivate_expired(bot, session_factory)
    check(panel._store[username].enabled, "продлённый в панели клиент не отключён")
    sub = await subscription_of(USER.id)
    check(sub.is_active and sub.expires_at == manual_until, "срок в боте подтянут из панели")
    check(not any(p.get("text") == ru.EXPIRED for p in calls_since(mark, "SendMessage")),
          "уведомления «подписка закончилась» не было")

    print("\n3x-ui сам отключил истёкшего — клиент всё равно узнаёт об окончании")
    past = utcnow() - timedelta(hours=2)
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        sub.expires_at = past
        await session.commit()
    panel._store[username].expires_at = past
    panel._store[username].enabled = False
    await tasks.sync_with_panel(session_factory)
    sub = await subscription_of(USER.id)
    check(sub.status == SubscriptionStatus.active, "часовая сверка не называет конец срока «приостановкой»")
    check("Подписка истекла" in render_access(sub), "в «Мой доступ» — «подписка истекла»")
    mark = len(CALLS)
    await tasks.deactivate_expired(bot, session_factory)
    sub = await subscription_of(USER.id)
    check(sub.status == SubscriptionStatus.expired and sub.notified_expired, "подписка помечена истёкшей")
    check(any(p.get("text") == ru.EXPIRED and p.get("chat_id") == USER.id for p in calls_since(mark, "SendMessage")),
          "клиент получил уведомление об окончании")

    print("\n«Обновить данные»")
    async with session_factory() as session:
        user = await session.get(User, USER.id)
        await subscriptions.issue_or_extend(session, user, days=30)
        sub = await repo.get_subscription(session, USER.id)
        sub.notified_3d = True
        await session.commit()
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.MenuCB(action="refresh").pack()))
    sub = await subscription_of(USER.id)
    check(sub.notified_3d, "обновление без продления не сбрасывает отметку о напоминании")

    panel._store[username].enabled = False  # админ отключил клиента в панели
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.MenuCB(action="refresh").pack()))
    check(not panel._store[username].enabled, "отключённого админом клиент сам не включит")
    sub = await subscription_of(USER.id)
    check(sub.status == SubscriptionStatus.disabled, "в боте видно, что доступ приостановлен")
    panel._store[username].enabled = True
    async with session_factory() as session:
        await subscriptions.sync_usage(session, await repo.get_subscription(session, USER.id))

    print("\nБан и разбан")
    until_before_ban = (await subscription_of(USER.id)).expires_at
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message(f"/ban {USER.id}", ADMIN))
    check(not panel._store[username].enabled, "бан отключил доступ в панели")
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message(f"/ban {USER.id}", ADMIN))
    check(panel._store[username].enabled, "разбан включил доступ обратно")
    sub = await subscription_of(USER.id)
    check(sub.is_active and sub.expires_at == until_before_ban, "оплаченный срок на месте")

    print("\nОшибка в обработчике не оставляет кнопку висеть")
    print("  (трассировка «тестовая поломка» в логе — ожидаемая)")
    real_list_plans = repo.list_plans

    async def broken(*args, **kwargs):
        raise RuntimeError("тестовая поломка")

    repo.list_plans = broken
    mark = len(CALLS)
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.MenuCB(action="plans").pack()))
    repo.list_plans = real_list_plans
    answers = calls_since(mark, "AnswerCallbackQuery")
    check(any(a.get("text") == ru.ERROR_GENERIC for a in answers), "пользователь увидел «что-то пошло не так»")

    print("\nПроверки перед оплатой")
    mark = len(CALLS)
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PayCB(method="cryptobot", plan_id=plan_id).pack()))
    check(any("недоступен" in a.get("text", "") for a in calls_since(mark, "AnswerCallbackQuery")),
          "выключенный способ оплаты не создаёт счёт")

    async def precheckout(pid: int) -> dict:
        query = PreCheckoutQuery(
            id=str(next(_ids)), from_user=USER, currency="XTR", total_amount=94, invoice_payload=f"sub:{pid}"
        )
        await dp.feed_update(bot, Update(update_id=next(_ids), pre_checkout_query=query))
        return [p for n, p in CALLS if n == "AnswerPreCheckoutQuery"][-1]

    check(not (await precheckout(payment_id)).get("ok"), "уже оплаченный счёт звёздами не списывается повторно")
    check(not (await precheckout(999999)).get("ok"), "чужой или несуществующий счёт отклонён")

    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, click(ikb.PayCB(method="stars", plan_id=plan_id).pack()))
    async with session_factory() as session:
        stars_id = (await repo.pending_payments(session, "stars"))[-1].id
    check((await precheckout(stars_id)).get("ok"), "свой неоплаченный счёт пропускается")

    # оплата пришла сразу после обычного сообщения — антифлуд не должен её съесть
    await dp.feed_update(bot, message("/profile"))
    sp = SuccessfulPayment(
        currency="XTR", total_amount=94, invoice_payload=f"sub:{stars_id}",
        telegram_payment_charge_id="charge_x", provider_payment_charge_id="prov_x",
    )
    await dp.feed_update(bot, Update(update_id=next(_ids), message=_msg(USER, successful_payment=sp)))
    async with session_factory() as session:
        check((await repo.get_payment(session, stars_id)).status == PaymentStatus.paid,
              "оплата звёздами сразу после сообщения не потерялась")

    print("\nТрафик закончился")
    async with session_factory() as session:
        sub = await repo.get_subscription(session, USER.id)
        sub.status = SubscriptionStatus.disabled
        sub.traffic_limit = 10 * GB
        sub.traffic_used = 10 * GB
        await session.commit()
        check("Трафик закончился" in render_access(sub), "клиенту сказано, что кончился трафик")
        check("Продление подписки" in plans_header(sub), "витрина предлагает продление от остатка")

    print("\n/give с неправильным числом дней")
    mark = len(CALLS)
    await asyncio.sleep(PAUSE)
    await dp.feed_update(bot, message(f"/give {USER.id} 99999999", ADMIN))
    check(any("от 1 до 3650" in p.get("text", "") for p in calls_since(mark, "SendMessage")),
          "огромный срок отклонён, а не уронил обработчик")

    print("\nНовый пользователь: первые апдейты одновременно")
    async with session_factory() as s1, session_factory() as s2:
        pair = await asyncio.gather(
            repo.get_or_create_user(s1, 9200, "twin", "Twin"), repo.get_or_create_user(s2, 9200, "twin", "Twin")
        )
    check(all(u.id == 9200 for u, _ in pair) and sum(new for _, new in pair) == 1, "запись создана один раз, без ошибки")

    await bot.session.close()
    print("\nГонки и защитные проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
