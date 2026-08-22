"""QR-код ссылки-подписки: клиенту достаточно навести камеру приложения."""
from __future__ import annotations

import io
import logging
from typing import Optional

from aiogram.types import BufferedInputFile

log = logging.getLogger(__name__)


def make_qr(data: str, filename: str = "vpn-subscription.png") -> Optional[BufferedInputFile]:
    """Возвращает PNG с QR-кодом или None, если библиотека недоступна."""
    if not data:
        return None
    try:
        import qrcode
    except ImportError:  # pragma: no cover - qrcode есть в requirements
        log.warning("Пакет qrcode не установлен — QR-код не отправляется")
        return None

    try:
        qr = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return BufferedInputFile(buffer.getvalue(), filename=filename)
    except Exception as exc:  # noqa: BLE001 - QR не должен ломать выдачу доступа
        log.warning("Не удалось построить QR-код: %s", exc)
        return None
