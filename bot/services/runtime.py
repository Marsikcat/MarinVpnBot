"""Настройки, которые администратор меняет прямо в боте.

Значение берётся из БД (таблица `settings`), а если его там нет — из .env.
Секреты (токены, доступы к панели) сюда не входят: их правят в .env и перезапускают бота.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo

log = logging.getLogger(__name__)

TRUE_WORDS = {"1", "true", "yes", "on", "да", "вкл", "включено"}
FALSE_WORDS = {"0", "false", "no", "off", "нет", "выкл", "выключено"}


@dataclass(frozen=True)
class Field:
    key: str
    title: str
    kind: str  # bool | int | float | str
    hint: str = ""
    minimum: float = 0
    maximum: float = 10 ** 9


FIELDS: List[Field] = [
    Field("trial_enabled", "Пробный период", "bool"),
    Field("trial_days", "Длительность триала, дней", "int", minimum=1, maximum=365),
    Field("trial_traffic_gb", "Трафик триала, ГБ", "int", "0 — безлимит", maximum=10000),
    Field("default_traffic_gb", "Лимит трафика по умолчанию, ГБ", "int",
          "0 — безлимит; применяется к новым тарифам и ручной выдаче дней", maximum=100000),
    Field("referral_enabled", "Реферальная программа", "bool"),
    Field("referral_percent", "Реферальный процент", "int", "% с каждой оплаты", maximum=100),
    Field("stars_rub_rate", "Рублей в одной звезде", "float", "курс пересчёта Stars", minimum=0.1, maximum=100),
    Field("support_url", "Ссылка на поддержку", "str", "например https://t.me/ваш_ник"),
    Field("channel_url", "Ссылка на канал", "str", "можно оставить пустой: минус — чтобы убрать"),
    Field("sbp_phone", "СБП: номер телефона", "str"),
    Field("sbp_bank", "СБП: банк получателя", "str"),
    Field("sbp_receiver", "СБП: имя получателя", "str"),
]
BY_KEY: Dict[str, Field] = {field.key: field for field in FIELDS}


class ValidationError(ValueError):
    """Введённое значение не подходит — текст ошибки показывается администратору."""


def parse(field: Field, raw: str) -> Any:
    raw = (raw or "").strip()
    if field.kind == "bool":
        low = raw.lower()
        if low in TRUE_WORDS:
            return True
        if low in FALSE_WORDS:
            return False
        raise ValidationError("Напишите «вкл» или «выкл».")

    if field.kind in ("int", "float"):
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            raise ValidationError("Нужно число.") from None
        if field.kind == "int":
            if value != int(value):
                raise ValidationError("Нужно целое число.")
            value = int(value)
        if not (field.minimum <= value <= field.maximum):
            raise ValidationError(f"Допустимо от {field.minimum:g} до {field.maximum:g}.")
        return value

    if raw == "-":
        return ""  # так администратор очищает необязательное поле
    if not raw:
        raise ValidationError("Пустое значение.")
    if len(raw) > 128:
        raise ValidationError("Слишком длинно, максимум 128 символов.")
    return raw


def _to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class Runtime:
    """Доступ к настройкам: runtime.trial_days, runtime.sbp_phone и т. д.

    Всё, чего нет в списке изменяемых полей, прозрачно берётся из .env.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}

    async def load(self, session: AsyncSession) -> None:
        loaded = 0
        for field in FIELDS:
            stored = await repo.get_setting(session, field.key, "")
            if stored == "":
                continue
            try:
                self._cache[field.key] = parse(field, stored)
                loaded += 1
            except ValidationError:
                log.warning("Настройка %s в БД повреждена (%r) — беру значение из .env", field.key, stored)
        if loaded:
            log.info("Загружено настроек из БД: %s", loaded)

    def __getattr__(self, name: str) -> Any:
        cache = self.__dict__.get("_cache", {})
        if name in cache:
            return cache[name]
        return getattr(settings, name)

    def get(self, key: str) -> Any:
        return getattr(self, key)

    async def set(self, session: AsyncSession, key: str, raw: str) -> Any:
        field = BY_KEY.get(key)
        if field is None:
            raise ValidationError("Неизвестная настройка.")
        value = parse(field, raw)
        await repo.set_setting(session, key, _to_str(value))
        self._cache[key] = value
        log.info("Настройка %s изменена на %r", key, value)
        return value

    async def toggle(self, session: AsyncSession, key: str) -> bool:
        current = bool(self.get(key))
        await self.set(session, key, "0" if current else "1")
        return not current

    def display(self, key: str) -> str:
        field = BY_KEY[key]
        value = self.get(key)
        if field.kind == "bool":
            return "включено" if value else "выключено"
        if field.kind == "float":
            return f"{value:g}"
        return str(value) if str(value) else "—"

    def field(self, key: str) -> Optional[Field]:
        return BY_KEY.get(key)


runtime = Runtime()
