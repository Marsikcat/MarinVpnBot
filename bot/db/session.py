"""Инициализация движка БД и фабрики сессий."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import BASE_DIR, settings
from bot.db.models import Base

log = logging.getLogger(__name__)

_url = settings.database_url
if _url.startswith("sqlite"):
    # относительный путь -> абсолютный, чтобы бот запускался из любой директории
    raw_path = _url.split("///", 1)[-1]
    db_path = Path(raw_path)
    if not db_path.is_absolute():
        db_path = (BASE_DIR / raw_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

engine = create_async_engine(_url, echo=False, pool_pre_ping=True, future=True)
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы и наполняет справочник тарифов при первом запуске."""
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    await seed_plans()


async def seed_plans() -> None:
    from sqlalchemy import func, select

    from bot.db.models import Plan

    defaults = [
        dict(code="m1", title="1 месяц", days=30, price_rub=149, traffic_gb=0, devices=3, sort_order=10),
        dict(code="m3", title="3 месяца", days=90, price_rub=399, traffic_gb=0, devices=3, sort_order=20),
        dict(code="m6", title="6 месяцев", days=180, price_rub=699, traffic_gb=0, devices=5, sort_order=30),
        dict(code="m12", title="1 год", days=365, price_rub=1190, traffic_gb=0, devices=5, sort_order=40),
    ]
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Plan))
        if count:
            return
        session.add_all([Plan(**item) for item in defaults])
        await session.commit()
        log.info("Созданы тарифы по умолчанию: %s", len(defaults))


async def dispose_db() -> None:
    await engine.dispose()
