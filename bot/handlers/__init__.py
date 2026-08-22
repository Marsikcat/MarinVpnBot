"""Сборка роутеров. Порядок важен: админский идёт первым."""
from __future__ import annotations

from aiogram import Router

from bot.handlers import access, admin, admin_config, common, payments, plans, profile, trial


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(common.cancel_router)
    router.include_router(admin.router)
    router.include_router(admin_config.router)
    router.include_router(common.router)
    router.include_router(trial.router)
    router.include_router(access.router)
    router.include_router(plans.router)
    router.include_router(payments.router)
    router.include_router(profile.router)
    return router
