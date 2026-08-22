"""ЮKassa: создание платежа и проверка статуса через REST API v3."""
from __future__ import annotations

import logging
import uuid as uuid_lib
from typing import Any, Dict, Optional

import aiohttp

from bot.config import settings
from bot.services.payments.base import Invoice, PaymentError, PaymentProvider

log = logging.getLogger(__name__)
API_URL = "https://api.yookassa.ru/v3"


class YooKassaProvider(PaymentProvider):
    code = "yookassa"
    title = "Банковская карта (ЮKassa)"
    currency = "RUB"

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            auth = aiohttp.BasicAuth(settings.yookassa_shop_id, settings.yookassa_secret_key)
            self._session = aiohttp.ClientSession(auth=auth, timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        session = await self._http()
        try:
            async with session.request(method, f"{API_URL}{path}", **kwargs) as resp:
                payload = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise PaymentError(f"ЮKassa {path} -> {resp.status}: {payload}")
                return payload
        except aiohttp.ClientError as exc:
            raise PaymentError(f"ЮKassa недоступна: {exc}") from exc

    async def create_invoice(self, payment_id: int, amount_rub: float, description: str) -> Invoice:
        body = {
            "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": settings.yookassa_return_url},
            "description": description[:128],
            "metadata": {"payment_id": str(payment_id)},
        }
        headers = {"Idempotence-Key": str(uuid_lib.uuid4())}
        data = await self._request("POST", "/payments", json=body, headers=headers)
        url = (data.get("confirmation") or {}).get("confirmation_url")
        if not url:
            raise PaymentError("ЮKassa не вернула ссылку на оплату")
        return Invoice(url=url, external_id=data.get("id"))

    async def check(self, external_id: str) -> str:
        data = await self._request("GET", f"/payments/{external_id}")
        status = data.get("status")
        if status == "succeeded" and data.get("paid"):
            return "paid"
        if status in ("canceled",):
            return "canceled"
        return "pending"

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
