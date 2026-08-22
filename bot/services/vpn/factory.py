"""Выбор реализации панели по VPN_PROVIDER."""
from __future__ import annotations

import logging
from typing import Optional

from bot.config import settings
from bot.services.vpn.base import VpnPanel

log = logging.getLogger(__name__)

_panel: Optional[VpnPanel] = None


def get_panel() -> VpnPanel:
    global _panel
    if _panel is not None:
        return _panel

    provider = settings.vpn_provider
    if provider == "marzban":
        from bot.services.vpn.marzban import MarzbanPanel

        _panel = MarzbanPanel()
    elif provider == "xui":
        from bot.services.vpn.xui import XuiPanel

        _panel = XuiPanel()
    else:
        from bot.services.vpn.mock import MockPanel

        log.warning("VPN_PROVIDER=mock — ключи выдаются демонстрационные, реальный VPN не создаётся")
        _panel = MockPanel()

    log.info("Панель VPN: %s", _panel.name)
    return _panel


async def close_panel() -> None:
    global _panel
    if _panel is not None:
        await _panel.close()
        _panel = None
