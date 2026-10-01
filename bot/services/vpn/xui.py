"""Клиент панелей семейства 3x-ui.

Клиентами управляет через обновление inbound целиком
(``/panel/api/inbounds/update/{id}``): читает inbound, подставляет запись клиента и
записывает обратно. Этот маршрут есть и в классических сборках, и в свежих, где
отдельных ручек ``addClient`` уже нет.

Клиент заводится сразу во всех inbound'ах панели (или в тех, что перечислены в
``XUI_INBOUND_IDS``) и получает единый ``subId`` — тогда ссылка-подписка отдаёт
пользователю все доступные протоколы разом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

import aiohttp

from bot.config import settings
from bot.services.vpn.base import VpnAccount, VpnPanel, VpnPanelError

log = logging.getLogger(__name__)
CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
# Cookie сессии 3x-ui живёт 6 часов; перелогиниваемся заметно раньше,
# иначе панель начинает отвечать 404 на любой запрос к API.
SESSION_TTL = timedelta(hours=1)

UUID_PROTOCOLS = {"vless", "vmess"}
PASSWORD_PROTOCOLS = {"trojan", "shadowsocks"}
AUTH_PROTOCOLS = {"hysteria", "hysteria2", "tuic"}


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _from_ms(ms: Optional[int]) -> Optional[datetime]:
    if not ms:
        return None
    ms = int(ms)
    if ms < 0:
        # «старт после первого подключения»: срок ещё не пошёл, в поле лежит длительность
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(milliseconds=-ms)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class XuiPanel(VpnPanel):
    name = "xui"

    def __init__(self) -> None:
        self.base_url = settings.xui_url.rstrip("/")
        self.sub_base = settings.xui_sub_base.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False
        self._logged_at: Optional[datetime] = None
        self._csrf: Optional[str] = None
        self._lock = asyncio.Lock()  # вход в панель
        # Клиенты пишутся перезаписью inbound'а целиком (прочитали — поменяли — записали).
        # Две записи вперемешку затёрли бы друг друга: один из клиентов пропал бы из панели.
        self._write_lock = asyncio.Lock()
        self._host = urlparse(self.base_url).hostname or "127.0.0.1"

    # ------------------------------------------------------------ HTTP
    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                connector=aiohttp.TCPConnector(ssl=False),
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
            self._logged_in = False
            self._logged_at = None
        return self._session

    async def _fetch_csrf(self) -> Optional[str]:
        """Свежие сборки защищены CSRF: токен лежит в <meta> страницы входа,
        оттуда же приходит cookie сессии. Без заголовка X-CSRF-Token — 403."""
        session = await self._http()
        try:
            async with session.get(f"{self.base_url}/") as resp:
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise VpnPanelError(f"3x-ui: сеть недоступна ({exc})") from exc
        match = CSRF_RE.search(html)
        return match.group(1) if match else None

    def _session_fresh(self) -> bool:
        """Cookie панели живёт 6 часов, поэтому обновляем вход заранее."""
        if not self._logged_in or self._logged_at is None:
            return False
        return (datetime.now(timezone.utc) - self._logged_at) < SESSION_TTL

    async def _login(self, force: bool = False) -> None:
        async with self._lock:
            if self._session_fresh() and not force:
                return
            session = await self._http()
            self._csrf = await self._fetch_csrf()
            payload_in = {"username": settings.xui_username, "password": settings.xui_password}
            headers = {"X-CSRF-Token": self._csrf} if self._csrf else {}
            try:
                async with session.post(f"{self.base_url}/login", json=payload_in, headers=headers) as resp:
                    text = await resp.text()
                    status = resp.status
            except aiohttp.ClientError as exc:
                raise VpnPanelError(f"3x-ui: сеть недоступна ({exc})") from exc

            try:
                payload = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                payload = {}

            if not payload.get("success"):
                if status == 403 and not payload:
                    raise VpnPanelError(
                        "3x-ui: вход отклонён (403). Проверьте XUI_URL — он должен быть "
                        "адресом панели без /panel в конце"
                    )
                raise VpnPanelError(f"3x-ui: вход не выполнен — {payload.get('msg') or f'HTTP {status}'}")
            self._logged_in = True
            self._logged_at = datetime.now(timezone.utc)

    async def _api(
        self, method: str, path: str, *, data: Any = None, retry_auth: bool = True, soft_404: bool = False
    ) -> Dict[str, Any]:
        await self._login()
        session = await self._http()
        headers = {"X-CSRF-Token": self._csrf} if self._csrf else {}
        try:
            async with session.request(method, f"{self.base_url}{path}", json=data, headers=headers) as resp:
                text = await resp.text()
                status = resp.status
        except aiohttp.ClientError as exc:
            raise VpnPanelError(f"3x-ui: сеть недоступна ({exc})") from exc

        # Панель отвечает 404 и на протухшую сессию, и на несуществующий маршрут,
        # поэтому один раз пробуем перелогиниться и повторить.
        if retry_auth and (status in (401, 403) or (status == 404 and not soft_404)):
            log.info("3x-ui: сессия недействительна (HTTP %s), вхожу заново", status)
            await self._login(force=True)
            return await self._api(method, path, data=data, retry_auth=False, soft_404=soft_404)
        if status == 404 and soft_404:
            return {}
        if status >= 400:
            raise VpnPanelError(f"3x-ui {method} {path} -> {status}: {text[:300]}")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            if retry_auth:  # обычно это HTML страницы входа
                await self._login(force=True)
                return await self._api(method, path, data=data, retry_auth=False, soft_404=soft_404)
            raise VpnPanelError("3x-ui: неожиданный ответ панели") from exc

        if not payload.get("success", False):
            raise VpnPanelError(f"3x-ui: {payload.get('msg', 'ошибка панели')}")
        return payload

    # ------------------------------------------------------------ разбор inbound'ов
    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        """Разные сборки отдают settings/streamSettings то строкой JSON, то объектом."""
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {}
        return {}

    def _clients(self, inbound: Dict[str, Any]) -> List[Dict[str, Any]]:
        return list(self._as_dict(inbound.get("settings")).get("clients", []) or [])

    def _set_clients(self, inbound: Dict[str, Any], clients: List[Dict[str, Any]]) -> None:
        """Кладёт клиентов обратно, сохраняя исходный формат поля settings."""
        current = self._as_dict(inbound.get("settings"))
        current["clients"] = clients
        inbound["settings"] = current if isinstance(inbound.get("settings"), dict) else json.dumps(current)

    async def _fetch_inbounds(self) -> List[Dict[str, Any]]:
        payload = await self._api("GET", "/panel/api/inbounds/list")
        inbounds = payload.get("obj") or []
        if not inbounds:
            raise VpnPanelError("3x-ui: в панели нет ни одного inbound — создайте его в веб-интерфейсе")
        wanted = settings.xui_inbound_ids
        if wanted:
            inbounds = [i for i in inbounds if i.get("id") in wanted]
            if not inbounds:
                raise VpnPanelError(
                    f"3x-ui: inbound'ы {wanted} не найдены — проверьте XUI_INBOUND_IDS"
                )
        return inbounds

    async def _save_inbound(self, inbound: Dict[str, Any]) -> None:
        await self._api("POST", f"/panel/api/inbounds/update/{inbound['id']}", data=inbound)

    # ------------------------------------------------------------ клиенты
    def _find_client(self, inbound: Dict[str, Any], email: str) -> Optional[Dict[str, Any]]:
        for client in self._clients(inbound):
            if client.get("email") == email:
                return client
        return None

    def _identity(self, inbounds: List[Dict[str, Any]], email: str) -> Dict[str, str]:
        """Собирает уже выданные пользователю идентификаторы, чтобы при продлении
        ключи не менялись и подписка осталась прежней."""
        found: Dict[str, str] = {}
        for inbound in inbounds:
            client = self._find_client(inbound, email)
            if not client:
                continue
            for key in ("id", "auth", "password", "subId"):
                if client.get(key) and key not in found:
                    found[key] = str(client[key])
        return found

    @staticmethod
    def _secret(length: int = 16) -> str:
        return secrets.token_hex(length // 2)

    def _build_client(
        self,
        inbound: Dict[str, Any],
        email: str,
        identity: Dict[str, str],
        expiry_ms: int,
        total_bytes: int,
        tg_id: int,
    ) -> Dict[str, Any]:
        """Формирует запись клиента. За образец берётся существующий клиент этого
        inbound'а — так наследуются его особенности (flow, method и прочее)."""
        protocol = (inbound.get("protocol") or "vless").lower()
        existing = self._find_client(inbound, email)
        template = existing or next(iter(self._clients(inbound)), None)

        client: Dict[str, Any] = dict(template) if template else {}
        if template and not existing:
            # чужая запись как образец: чистим всё, что относится к её владельцу
            for key in ("id", "auth", "password", "subId", "comment", "created_at", "updated_at"):
                client.pop(key, None)

        client.update(
            {
                "email": email,
                "enable": True,
                "expiryTime": expiry_ms,
                "totalGB": int(total_bytes),
                "subId": identity["subId"],
                "tgId": int(tg_id),
                "comment": client.get("comment", ""),
                "limitIp": client.get("limitIp", 0),
                "reset": client.get("reset", 0),
                "created_at": client.get("created_at", _now_ms()),
                "updated_at": _now_ms(),
            }
        )

        if protocol in UUID_PROTOCOLS:
            client["id"] = identity["uuid"]
            if protocol == "vless" and "flow" not in client:
                stream = self._as_dict(inbound.get("streamSettings"))
                is_vision = stream.get("network") == "tcp" and stream.get("security") == "reality"
                client["flow"] = "xtls-rprx-vision" if is_vision else ""
        elif protocol in PASSWORD_PROTOCOLS:
            client["password"] = identity["password"]
        elif protocol in AUTH_PROTOCOLS:
            client["auth"] = identity["password"]
        else:
            client.setdefault("id", identity["uuid"])
        return client

    # ------------------------------------------------------------ ссылки
    def _sub_url(self, sub_id: str) -> Optional[str]:
        return f"{self.sub_base}/{sub_id}" if self.sub_base and sub_id else None

    def _build_link(self, inbound: Dict[str, Any], client: Dict[str, Any]) -> Optional[str]:
        """Прямая ссылка для ручного добавления. Для протоколов, где формат ссылки
        неоднозначен, возвращает None — такие ключи пользователь получит подпиской."""
        protocol = (inbound.get("protocol") or "").lower()
        if protocol not in ("vless", "trojan"):
            return None

        port = inbound.get("port")
        remark = inbound.get("remark") or "VPN"
        stream = self._as_dict(inbound.get("streamSettings"))
        network = stream.get("network", "tcp")
        security = stream.get("security", "none")
        params: Dict[str, str] = {"type": network, "security": security}

        if security == "reality":
            reality = stream.get("realitySettings", {}) or {}
            inner = reality.get("settings", {}) or {}
            names = reality.get("serverNames") or []
            short_ids = reality.get("shortIds") or []
            params["pbk"] = inner.get("publicKey", "")
            params["fp"] = inner.get("fingerprint", "chrome")
            if names:
                params["sni"] = names[0]
            if short_ids:
                params["sid"] = short_ids[0]
            if inner.get("spiderX"):
                params["spx"] = inner["spiderX"]
        elif security in ("tls", "xtls"):
            tls = stream.get("tlsSettings", {}) or {}
            if tls.get("serverName"):
                params["sni"] = tls["serverName"]
            fingerprint = (tls.get("settings") or {}).get("fingerprint")
            if fingerprint:
                params["fp"] = fingerprint

        if network == "ws":
            ws = stream.get("wsSettings", {}) or {}
            params["path"] = ws.get("path", "/")
            host = (ws.get("headers") or {}).get("Host")
            if host:
                params["host"] = host
        elif network == "grpc":
            params["serviceName"] = (stream.get("grpcSettings", {}) or {}).get("serviceName", "")

        if client.get("flow"):
            params["flow"] = client["flow"]

        secret = client.get("id") or client.get("password") or ""
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v != "")
        tag = quote(f"{remark} · {client.get('email', '')}", safe="")
        return f"{protocol}://{secret}@{self._host}:{port}?{query}#{tag}"

    # ------------------------------------------------------------ трафик
    def _used_traffic(self, inbounds: List[Dict[str, Any]], email: str) -> int:
        # Статистика ведётся по email, а не по inbound'у: одна и та же запись приходит
        # в clientStats каждого inbound'а. Сумма умножила бы расход на их число.
        used = 0
        for inbound in inbounds:
            for stat in inbound.get("clientStats") or []:
                if stat.get("email") == email:
                    used = max(used, int(stat.get("up") or 0) + int(stat.get("down") or 0))
        return used

    def _expires_at(self, inbounds: List[Dict[str, Any]], email: str) -> Optional[datetime]:
        """Самый поздний срок клиента: по всем inbound'ам, из настроек и из статистики.
        Берём максимум, чтобы при продлении не потерять дни, выставленные только в одном месте."""
        latest: Optional[datetime] = None
        for inbound in inbounds:
            values = [c.get("expiryTime") for c in self._clients(inbound) if c.get("email") == email]
            values += [s.get("expiryTime") for s in inbound.get("clientStats") or [] if s.get("email") == email]
            for value in values:
                moment = _from_ms(value)
                if moment and (latest is None or moment > latest):
                    latest = moment
        return latest

    # ------------------------------------------------------------ публичный интерфейс
    async def create_or_update(
        self, username: str, expires_at: datetime, data_limit: int = 0, note: str = ""
    ) -> VpnAccount:
        tg_id = 0
        digits = re.search(r"\d+", note or "")
        if digits:
            tg_id = int(digits.group())
        expiry_ms = _to_ms(expires_at)
        links: List[str] = []

        async with self._write_lock:
            inbounds = await self._fetch_inbounds()
            identity = self._identity(inbounds, username)
            identity.setdefault("uuid", identity.get("id") or str(uuid_lib.uuid4()))
            identity.setdefault("password", identity.get("auth") or self._secret())
            identity.setdefault("subId", self._secret())

            for inbound in inbounds:
                clients = [c for c in self._clients(inbound) if c.get("email") != username]
                client = self._build_client(inbound, username, identity, expiry_ms, data_limit, tg_id)
                clients.append(client)
                self._set_clients(inbound, clients)
                await self._save_inbound(inbound)
                link = self._build_link(inbound, client)
                if link:
                    links.append(link)

        log.info("3x-ui: клиент %s выдан в %s inbound'ах", username, len(inbounds))
        return VpnAccount(
            username=username,
            uuid=identity["uuid"],
            subscription_url=self._sub_url(identity["subId"]),
            links=links,
            expires_at=expires_at,
            data_limit=data_limit,
            used_traffic=self._used_traffic(inbounds, username),
            enabled=True,
        )

    async def get(self, username: str) -> Optional[VpnAccount]:
        return self._account(await self._fetch_inbounds(), username)

    async def get_many(self, usernames: Sequence[str]) -> Dict[str, Optional[VpnAccount]]:
        inbounds = await self._fetch_inbounds()  # один запрос на всех
        return {username: self._account(inbounds, username) for username in usernames}

    def _account(self, inbounds: List[Dict[str, Any]], username: str) -> Optional[VpnAccount]:
        links: List[str] = []
        found: Optional[Dict[str, Any]] = None
        for inbound in inbounds:
            client = self._find_client(inbound, username)
            if not client:
                continue
            found = found or client
            link = self._build_link(inbound, client)
            if link:
                links.append(link)
        if found is None:
            return None

        return VpnAccount(
            username=username,
            uuid=found.get("id"),
            subscription_url=self._sub_url(str(found.get("subId") or "")),
            links=links,
            expires_at=self._expires_at(inbounds, username),
            data_limit=int(found.get("totalGB") or 0),
            used_traffic=self._used_traffic(inbounds, username),
            enabled=bool(found.get("enable", True)),
        )

    async def _update_clients(self, username: str, mutate) -> None:
        async with self._write_lock:
            for inbound in await self._fetch_inbounds():
                clients = self._clients(inbound)
                changed, new_clients = mutate(clients)
                if not changed:
                    continue
                self._set_clients(inbound, new_clients)
                await self._save_inbound(inbound)

    async def set_enabled(self, username: str, enabled: bool) -> None:
        def mutate(clients: List[Dict[str, Any]]) -> Tuple[bool, List[Dict[str, Any]]]:
            changed = False
            for client in clients:
                if client.get("email") == username and client.get("enable") != enabled:
                    client["enable"] = enabled
                    client["updated_at"] = _now_ms()
                    changed = True
            return changed, clients

        await self._update_clients(username, mutate)

    async def delete(self, username: str) -> None:
        def mutate(clients: List[Dict[str, Any]]) -> Tuple[bool, List[Dict[str, Any]]]:
            remaining = [c for c in clients if c.get("email") != username]
            return len(remaining) != len(clients), remaining

        await self._update_clients(username, mutate)

    async def reset_traffic(self, username: str) -> None:
        # Свежие сборки ведут клиентов отдельно от inbound'ов: сброс — одной ручкой по email.
        payload = await self._api(
            "POST", f"/panel/api/clients/resetTraffic/{quote(username, safe='')}", soft_404=True
        )
        if payload:
            log.info("3x-ui: трафик %s сброшен", username)
            return
        # классические сборки: сброс в каждом inbound'е, где есть клиент
        for inbound in await self._fetch_inbounds():
            if self._find_client(inbound, username):
                await self._api(
                    "POST", f"/panel/api/inbounds/{inbound['id']}/resetClientTraffic/{username}"
                )
        log.info("3x-ui: трафик %s сброшен", username)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
