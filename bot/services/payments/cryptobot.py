"""CryptoBot (Crypto Pay API) — оплата криптовалютой внутри Telegram."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

from bot.config import settings
from bot.services.payments.base import Invoice, PaymentError, PaymentProvider, rub_to_usd

log = logging.getLogger(__name__)
API_URL = "https://pay.crypt.bot/api"


class CryptoBotProvider(PaymentProvider):
    code = "cryptobot"
    title = "Криптовалюта (CryptoBot)"
    currency = "USD"

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Crypto-Pay-API-Token": settings.cryptobot_token},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def _call(self, method: str, **params: Any) -> Dict[str, Any]:
        session = await self._http()
        try:
            async with session.post(f"{API_URL}/{method}", json=params) as resp:
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise PaymentError(f"CryptoBot недоступен: {exc}") from exc
        if not payload.get("ok"):
            raise PaymentError(f"CryptoBot {method}: {payload.get('error')}")
        return payload["result"]

    def convert(self, amount_rub: float) -> float:
        return rub_to_usd(amount_rub)

    async def create_invoice(self, payment_id: int, amount_rub: float, description: str) -> Invoice:
        result = await self._call(
            "createInvoice",
            currency_type="fiat" if settings.cryptobot_asset.upper() in ("USD", "EUR", "RUB") else "crypto",
            asset=settings.cryptobot_asset.upper(),
            amount=str(self.convert(amount_rub)),
            description=description[:1024],
            payload=str(payment_id),
            allow_comments=False,
            allow_anonymous=False,
            expires_in=3600,
        )
        url = result.get("bot_invoice_url") or result.get("pay_url")
        if not url:
            raise PaymentError("CryptoBot не вернул ссылку на оплату")
        return Invoice(url=url, external_id=str(result.get("invoice_id")))

    async def check(self, external_id: str) -> str:
        result = await self._call("getInvoices", invoice_ids=external_id)
        items = result.get("items") or []
        if not items:
            return "pending"
        status = items[0].get("status")
        if status == "paid":
            return "paid"
        if status == "expired":
            return "canceled"
        return "pending"

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
