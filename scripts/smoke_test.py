"""Сквозная проверка логики без Telegram: БД, тарифы, оплата, продление, истечение.

Запуск:  python scripts/smoke_test.py
Использует отдельную БД data/smoke.db и mock-панель — реальные сервисы не трогает.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(ROOT / 'data' / 'smoke.db').as_posix()}")
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
os.environ.setdefault("BOT_TOKEN", "0:smoke")

DB_FILE = ROOT / "data" / "smoke.db"
if DB_FILE.exists():
    DB_FILE.unlink()

from bot.db import repo  # noqa: E402
from bot.db.models import PaymentStatus, SubscriptionStatus, utcnow  # noqa: E402
from bot.db.session import init_db, session_factory  # noqa: E402
from bot.services import subscriptions  # noqa: E402
from bot.services.payments.base import rub_to_stars  # noqa: E402


class DummyBot:
    """Заглушка Bot: собирает отправленные сообщения."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return None

    async def send_photo(self, chat_id, photo=None, caption="", **kwargs):
        # доступ приходит картинкой с QR-кодом, текст лежит в подписи
        self.sent.append((chat_id, caption))
        return None


def check(condition: bool, title: str) -> None:
    print(("  OK   " if condition else "  FAIL ") + title)
    if not condition:
        raise SystemExit(1)


async def main() -> None:
    await init_db()
    bot = DummyBot()

    async with session_factory() as session:
        plans = await repo.list_plans(session)
        check(len(plans) == 4, f"тарифы засеяны ({len(plans)} шт.)")
        plan = plans[0]

        buyer, created = await repo.get_or_create_user(session, 1002, "buyer", "Покупатель")
        check(created, "пользователь создан")

        # --- пробный период
        trial = await subscriptions.grant_trial(session, buyer)
        check(trial.is_active and trial.is_trial, "триал выдан и активен")
        check(buyer.trial_used, "повторный триал заблокирован флагом")
        check(bool(subscriptions.parse_links(trial)), "ключи получены из панели")
        trial_until = trial.expires_at

        # --- покупка тарифа
        payment = await repo.create_payment(session, buyer.id, plan, provider="yookassa", amount=plan.price_rub)
        subscription = await subscriptions.complete_payment(session, bot, payment)
        check(payment.status == PaymentStatus.paid, "платёж помечен оплаченным")
        check(
            subscription.expires_at == trial_until + timedelta(days=plan.days),
            "срок продлён поверх остатка триала",
        )
        check(not subscription.is_trial, "подписка больше не помечена как пробная")

        # --- повторное подтверждение того же платежа ничего не ломает
        again = await subscriptions.complete_payment(session, bot, payment)
        check(again.expires_at == subscription.expires_at, "повторное подтверждение идемпотентно")

        revenue = await repo.total_revenue(session)
        check(revenue == plan.price_rub, "выручка учтена один раз")

        # --- истечение подписки
        subscription.expires_at = utcnow() - timedelta(hours=1)
        await session.commit()
        expired = await repo.subscriptions_expired(session)
        check(len(expired) == 1, "просроченная подписка найдена планировщиком")
        await subscriptions.disable(session, expired[0])
        check(expired[0].status == SubscriptionStatus.expired, "доступ отключён")

        # --- пересчёт в звёзды
        check(rub_to_stars(149) == 94, f"149 ₽ -> {rub_to_stars(149)} звёзд при курсе 1.6")

        # единственная подписка только что отключена
        active = await repo.count_active_subscriptions(session)
        check(active == 0, f"активных подписок после отключения: {active}")

    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
