"""Диагностика связи с VPN-панелью.

    python scripts/panel_check.py          # логин + список inbound'ов
    python scripts/panel_check.py --full   # плюс создание и удаление тестового клиента

Показывает ID inbound'ов — то самое число, которое нужно вписать в XUI_INBOUND_ID.
Пароли не выводятся.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot.config import settings  # noqa: E402
from bot.services.vpn.base import VpnPanelError  # noqa: E402
from bot.services.vpn.factory import close_panel, get_panel  # noqa: E402

TEST_USER = "tg-panelcheck"


async def show_xui_inbounds(panel) -> None:
    payload = await panel._api("GET", "/panel/api/inbounds/list")
    inbounds = payload.get("obj") or []
    if not inbounds:
        print("  Панель не вернула ни одного inbound — создайте его в веб-интерфейсе.")
        return

    print(f"  Найдено inbound'ов: {len(inbounds)}\n")
    print("   ID | порт  | протокол | безопасность | клиентов | название")
    print("  ----+-------+----------+--------------+----------+---------")
    for inbound in inbounds:
        stream = panel._as_dict(inbound.get("streamSettings"))
        clients = len(panel._as_dict(inbound.get("settings")).get("clients", []))
        mark = " " if inbound.get("enable", True) else "✗"
        print(
            f"  {mark}{inbound.get('id'):>3} | {inbound.get('port'):<5} | "
            f"{inbound.get('protocol', '?'):<8} | {stream.get('security', '-'):<12} | "
            f"{clients:<8} | {inbound.get('remark', '')}"
        )
    wanted = settings.xui_inbound_ids
    if wanted:
        print(f"\n  XUI_INBOUND_IDS={','.join(map(str, wanted))} — клиент попадёт только в эти inbound'ы")
        missing = [i for i in wanted if not any(x.get("id") == i for x in inbounds)]
        if missing:
            print(f"  ⚠️  В панели нет inbound'ов {missing} — проверьте список.")
    else:
        print("\n  XUI_INBOUND_IDS пусто — клиент заводится во всех inbound'ах (в подписке будут все протоколы)")
    print(f"  Ссылка-подписка: {settings.xui_sub_base or 'НЕ ЗАДАНА (XUI_SUB_BASE)'}")


async def full_check(panel) -> None:
    print("\n[3/3] Тестовая выдача ключа")
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    account = await panel.create_or_update(TEST_USER, expires, data_limit=0, note="проверка связи")
    print(f"  Клиент создан: {account.username}, uuid {account.uuid}")
    if account.subscription_url:
        print(f"  Ссылка-подписка: {account.subscription_url}")
    else:
        print("  Ссылки-подписки нет (XUI_SUB_BASE не задан) — клиентам уйдут ключи vless://")
    for link in account.links:
        print(f"  Ключ: {link[:110]}{'…' if len(link) > 110 else ''}")

    found = await panel.get(TEST_USER)
    print(f"  Чтение обратно: {'ок' if found else 'НЕ НАЙДЕН'}")
    await panel.delete(TEST_USER)
    print(f"  Тестовый клиент удалён: {'ок' if await panel.get(TEST_USER) is None else 'ОСТАЛСЯ'}")


async def main() -> None:
    print(f"Панель: {settings.vpn_provider}")
    if settings.vpn_provider == "mock":
        print("VPN_PROVIDER=mock — реальная панель не используется. Укажите marzban или xui.")
        return
    if settings.vpn_provider == "xui":
        print(f"Адрес:  {settings.xui_url}")
        if settings.xui_password in ("", "changeme"):
            print("\n❌ XUI_PASSWORD не заполнен в .env — заполните и запустите снова.")
            return

    panel = get_panel()
    try:
        print("\n[1/3] Авторизация")
        if settings.vpn_provider == "xui":
            await panel._login(force=True)
        else:
            await panel._auth(force=True)
        print("  Успешно")

        print("\n[2/3] Inbound'ы")
        if settings.vpn_provider == "xui":
            await show_xui_inbounds(panel)
        else:
            print("  Для Marzban список берётся из MARZBAN_INBOUNDS (пусто = все).")

        if "--full" in sys.argv:
            await full_check(panel)
        else:
            print("\nЗапустите с --full, чтобы проверить создание и удаление ключа.")

        print("\n✅ Связь с панелью работает.")
    except VpnPanelError as exc:
        print(f"\n❌ Ошибка: {exc}")
        print("\nЧто проверить:")
        print("  • XUI_USERNAME и XUI_PASSWORD — те же, что при входе в веб-панель")
        print("  • XUI_URL — без /panel в конце")
        print("  • панель доступна с этой машины (firewall, порт)")
    finally:
        await close_panel()


if __name__ == "__main__":
    asyncio.run(main())
