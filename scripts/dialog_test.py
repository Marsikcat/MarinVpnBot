"""Прогон реальных апдейтов Telegram через Dispatcher с подставным Bot.

Проверяет связку middleware -> фильтры -> хендлеры: /start, триал, «Мой доступ»,
витрина тарифов, выбор тарифа, оплата звёздами и обработка successful_payment.

Запуск:  python scripts/dialog_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = ROOT / "data" / "dialog.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"
# Тест не должен зависеть от боевого .env — фиксируем все влияющие настройки.
os.environ["VPN_PROVIDER"] = "mock"
os.environ["TRIAL_ENABLED"] = "true"
os.environ["TRIAL_DAYS"] = "3"
os.environ["TRIAL_TRAFFIC_GB"] = "10"
os.environ["STARS_RUB_RATE"] = "1.6"
os.environ["PAY_STARS_ENABLED"] = "true"
os.environ["PAY_SBP_ENABLED"] = "false"
os.environ["PAY_YOOKASSA_ENABLED"] = "false"
os.environ["PAY_CRYPTOBOT_ENABLED"] = "false"
os.environ["REQUIRED_CHANNEL_ID"] = ""
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_IDS"] = "555"
if DB_FILE.exists():
    DB_FILE.unlink()

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.methods import TelegramMethod  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    Message,
    SuccessfulPayment,
    Update,
    User as TgUser,
)

from bot.db import repo  # noqa: E402
from bot.db.session import init_db, session_factory  # noqa: E402
from bot.keyboards import inline as ikb  # noqa: E402
from bot.main import create_dispatcher  # noqa: E402

USER = TgUser(id=777001, is_bot=False, first_name="Тест", username="tester")
CHAT = Chat(id=777001, type="private")
CALLS: list[tuple[str, dict]] = []
_update_id = iter(range(1, 10_000))
_message_id = iter(range(1, 10_000))


class FakeBot(Bot):
    """Bot, который не ходит в сеть: запоминает вызовы API."""

    async def __call__(self, method: TelegramMethod, request_timeout=None):  # type: ignore[override]
        name = type(method).__name__
        CALLS.append((name, method.model_dump(exclude_none=True)))
        if name == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="VPN", username="vpn_test_bot")
        return True


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_message(text: str = None, successful_payment: SuccessfulPayment = None) -> Update:
    message = Message(
        message_id=next(_message_id),
        date=now(),
        chat=CHAT,
        from_user=USER,
        text=text,
        successful_payment=successful_payment,
    )
    return Update(update_id=next(_update_id), message=message)


def make_callback(data: str) -> Update:
    message = Message(message_id=next(_message_id), date=now(), chat=CHAT, from_user=USER, text="…")
    callback = CallbackQuery(
        id=str(next(_update_id)), from_user=USER, chat_instance="1", data=data, message=message
    )
    return Update(update_id=next(_update_id), callback_query=callback)


def last_call(name: str) -> dict:
    for call_name, payload in reversed(CALLS):
        if call_name == name:
            return payload
    return {}


def check(condition: bool, title: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + title)
    if not condition:
        print("     вызовы API:", [c[0] for c in CALLS[-6:]])
        raise SystemExit(1)


async def main() -> None:
    await init_db()
    bot = FakeBot(token="123456:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()
    pause = 0.45  # антифлуд-мидлварь: 0.4 c между событиями

    # --- /start
    await dp.feed_update(bot, make_message("/start"))
    check("SendMessage" in [c[0] for c in CALLS], "/start ответил приветствием")
    async with session_factory() as session:
        user = await repo.get_user(session, USER.id)
        check(user is not None, "пользователь зарегистрирован")

    # --- пробный период
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_message("🎁 Пробный период"))
    check("активирован" in last_call("SendPhoto").get("caption", ""), "триал выдан пакетом с QR")

    # --- мой доступ: приходит фото с QR-кодом и всем нужным для подключения
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_message("🔑 Мой доступ"))
    photo = last_call("SendPhoto")
    caption = photo.get("caption", "")
    check(bool(photo), "доступ пришёл картинкой с QR-кодом")
    check("Ссылка-подписка" in caption, "в подписи есть ссылка-подписка")
    check("Подключение за минуту" in caption, "в подписи есть инструкция")
    check(len(caption) <= 1024, f"подпись укладывается в лимит Telegram ({len(caption)})")
    buttons = [
        b["text"]
        for row in photo.get("reply_markup", {}).get("inline_keyboard", [])
        for b in row
    ]
    check("🔑 Ключи по одному" in buttons, "есть кнопка с отдельными ключами")

    # ключи по одному — запасной путь для приложений без поддержки подписки
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_callback(ikb.MenuCB(action="keys").pack()))
    check("vless://" in last_call("SendMessage").get("text", ""), "ключи отдаются по кнопке")

    # --- витрина тарифов
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_message("💳 Купить подписку"))
    # у пользователя уже есть активный триал, поэтому витрина открывается как продление
    shop_text = last_call("SendMessage").get("text", "")
    check("Продление подписки" in shop_text, "витрина открылась в режиме продления")
    check("не сгорит" in shop_text, "объяснено, что остаток дней сохранится")

    # --- карточка тарифа
    async with session_factory() as session:
        plan = (await repo.list_plans(session))[0]
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_callback(ikb.PlanCB(plan_id=plan.id).pack()))
    card = last_call("EditMessageText").get("text", "")
    check("оплаты" in card, "открыта карточка тарифа")
    check("Продление:" in card, "в карточке видно, до какой даты продлится доступ")

    # --- оплата звёздами
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_callback(ikb.PayCB(method="stars", plan_id=plan.id).pack()))
    invoice = last_call("SendInvoice")
    check(invoice.get("currency") == "XTR" and invoice.get("prices"), "выставлен счёт в Telegram Stars")

    async with session_factory() as session:
        payments = await repo.pending_payments(session, "stars")
        check(len(payments) == 1, "платёж сохранён в статусе pending")
        payment_id = payments[0].id
        before = (await repo.get_subscription(session, USER.id)).expires_at

    # --- Telegram сообщает об успешной оплате
    await asyncio.sleep(pause)
    sp = SuccessfulPayment(
        currency="XTR",
        total_amount=94,
        invoice_payload=f"sub:{payment_id}",
        telegram_payment_charge_id="charge_test_1",
        provider_payment_charge_id="prov_1",
    )
    await dp.feed_update(bot, make_message(successful_payment=sp))

    async with session_factory() as session:
        payment = await repo.get_payment(session, payment_id)
        subscription = await repo.get_subscription(session, USER.id)
        check(payment.status.value == "paid", "платёж помечен оплаченным")
        check(payment.external_id == "charge_test_1", "сохранён charge_id для возврата звёзд")
        check(subscription.expires_at > before, "срок подписки продлён")
        check(not subscription.is_trial, "подписка перестала быть пробной")

    # --- профиль
    await asyncio.sleep(pause)
    await dp.feed_update(bot, make_message("👤 Профиль"))
    check("Профиль" in last_call("SendMessage").get("text", ""), "профиль открывается")

    await bot.session.close()
    print("\nДиалоговые сценарии пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
