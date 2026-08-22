"""Реестр включённых способов оплаты."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from bot.config import settings
from bot.services.payments.base import PaymentProvider

METHOD_TITLES: Dict[str, str] = {
    "stars": "⭐ Telegram Stars",
    "yookassa": "💳 Банковская карта",
    "sbp": "🏦 СБП (перевод по номеру)",
    "cryptobot": "🪙 Криптовалюта",
}

_providers: Dict[str, PaymentProvider] = {}


def get_provider(code: str) -> Optional[PaymentProvider]:
    """Возвращает провайдера с внешним API (stars и СБП обрабатываются в хендлерах)."""
    if code in _providers:
        return _providers[code]

    if code == "yookassa" and settings.pay_yookassa_enabled:
        from bot.services.payments.yookassa import YooKassaProvider

        _providers[code] = YooKassaProvider()
    elif code == "cryptobot" and settings.pay_cryptobot_enabled:
        from bot.services.payments.cryptobot import CryptoBotProvider

        _providers[code] = CryptoBotProvider()
    else:
        return None
    return _providers[code]


def available_methods() -> List[Tuple[str, str]]:
    return [(code, METHOD_TITLES.get(code, code)) for code in settings.enabled_payment_methods]


async def close_providers() -> None:
    for provider in list(_providers.values()):
        await provider.close()
    _providers.clear()
