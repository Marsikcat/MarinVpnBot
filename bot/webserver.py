"""HTTP-сервер для вебхуков платёжных систем.

Запускается только при WEBHOOK_ENABLED=true. Любой вебхук перед выдачей подписки
перепроверяется через API платёжной системы — подделать колбэк невозможно.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import settings
from bot.db import repo
from bot.db.models import PaymentStatus
from bot.services import subscriptions
from bot.services.payments.base import PaymentError
from bot.services.payments.registry import get_provider
from bot.services.vpn.base import VpnPanelError

log = logging.getLogger(__name__)


def yookassa_path() -> str:
    return f"/pay/yookassa/{settings.webhook_secret}"


def cryptobot_path() -> str:
    return f"/pay/cryptobot/{settings.webhook_secret}"


async def _confirm(request: web.Request, provider_code: str, external_id: str) -> web.Response:
    bot: Bot = request.app["bot"]
    session_factory: async_sessionmaker = request.app["session_factory"]
    provider = get_provider(provider_code)
    if provider is None:
        return web.Response(text="provider disabled", status=200)

    async with session_factory() as session:
        payment = await repo.get_payment_by_external(session, provider_code, str(external_id))
        if payment is None:
            log.warning("Вебхук %s: платёж %s не найден", provider_code, external_id)
            return web.Response(text="unknown payment", status=200)
        if payment.status == PaymentStatus.paid:
            return web.Response(text="already paid", status=200)

        try:
            status = await provider.check(str(external_id))
        except PaymentError as exc:
            log.error("Вебхук %s: проверка не удалась: %s", provider_code, exc)
            return web.Response(text="retry later", status=500)

        if status != "paid":
            return web.Response(text="not paid", status=200)

        try:
            await subscriptions.complete_payment(session, bot, payment)
        except VpnPanelError as exc:
            log.error("Вебхук %s: панель недоступна: %s", provider_code, exc)
            return web.Response(text="panel unavailable", status=500)

    log.info("Вебхук %s: платёж %s подтверждён", provider_code, payment.id)
    return web.Response(text="ok")


async def yookassa_webhook(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.Response(text="bad json", status=400)

    obj = body.get("object") or {}
    external_id = obj.get("id")
    if not external_id:
        return web.Response(text="no id", status=200)
    return await _confirm(request, "yookassa", external_id)


async def cryptobot_webhook(request: web.Request) -> web.Response:
    raw = await request.read()
    signature = request.headers.get("crypto-pay-api-signature", "")
    secret = hashlib.sha256(settings.cryptobot_token.encode()).digest()
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        log.warning("CryptoBot: неверная подпись вебхука")
        return web.Response(text="bad signature", status=403)

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return web.Response(text="bad json", status=400)

    if body.get("update_type") != "invoice_paid":
        return web.Response(text="ignored")
    invoice_id = (body.get("payload") or {}).get("invoice_id")
    if not invoice_id:
        return web.Response(text="no invoice", status=200)
    return await _confirm(request, "cryptobot", invoice_id)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app(bot: Bot, session_factory: async_sessionmaker) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["session_factory"] = session_factory
    app.router.add_get("/health", health)
    if settings.pay_yookassa_enabled:
        app.router.add_post(yookassa_path(), yookassa_webhook)
    if settings.pay_cryptobot_enabled:
        app.router.add_post(cryptobot_path(), cryptobot_webhook)
    return app


async def start_webserver(bot: Bot, session_factory: async_sessionmaker) -> web.AppRunner:
    app = create_app(bot, session_factory)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    log.info("HTTP-сервер вебхуков слушает %s:%s", settings.webhook_host, settings.webhook_port)
    if settings.webhook_base_url:
        if settings.pay_yookassa_enabled:
            log.info("URL для ЮKassa: %s", settings.webhook_url(yookassa_path()))
        if settings.pay_cryptobot_enabled:
            log.info("URL для CryptoBot: %s", settings.webhook_url(cryptobot_path()))
    return runner
