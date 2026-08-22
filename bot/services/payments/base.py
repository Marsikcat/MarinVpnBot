"""Общий интерфейс платёжных провайдеров."""
from __future__ import annotations

import abc
import math
from dataclasses import dataclass
from typing import Optional

from bot.services.runtime import runtime


class PaymentError(RuntimeError):
    """Платёжка вернула ошибку или недоступна."""


@dataclass(slots=True)
class Invoice:
    url: Optional[str]
    external_id: Optional[str] = None


class PaymentProvider(abc.ABC):
    code: str = "base"
    title: str = "Оплата"
    currency: str = "RUB"

    @abc.abstractmethod
    async def create_invoice(self, payment_id: int, amount_rub: float, description: str) -> Invoice:
        """Создаёт счёт и возвращает ссылку на оплату."""

    @abc.abstractmethod
    async def check(self, external_id: str) -> str:
        """Возвращает статус: paid | pending | canceled."""

    def convert(self, amount_rub: float) -> float:
        return round(amount_rub, 2)

    async def close(self) -> None:
        pass


def rub_to_stars(amount_rub: float) -> int:
    """Пересчёт рублей в Telegram Stars (округление вверх, минимум 1)."""
    rate = runtime.stars_rub_rate or 1.0
    return max(1, math.ceil(amount_rub / rate))


def rub_to_usd(amount_rub: float) -> float:
    rate = runtime.usd_rub_rate or 1.0
    return max(0.01, round(amount_rub / rate, 2))
