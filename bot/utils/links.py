"""Проверка ссылок из настроек.

Кнопка с нерабочим адресом хуже, чем её отсутствие: пользователь жмёт и попадает
в пустоту. Поэтому значения-заглушки из .env.example считаем незаполненными.
"""
from __future__ import annotations

from typing import Optional

PLACEHOLDER_MARKERS = (
    "your_support",
    "your_channel",
    "your_bot",
    "ваш_ник",
    "example.com",
    "panel.example",
    "bot.example",
)


def clean_url(value: Optional[str]) -> Optional[str]:
    """Возвращает ссылку, если она похожа на настоящую, иначе None."""
    url = (value or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    if url.rstrip("/") in ("https://t.me", "http://t.me"):
        return None  # адрес из шаблона, ник не подставили
    low = url.lower()
    if any(marker in low for marker in PLACEHOLDER_MARKERS):
        return None
    return url
