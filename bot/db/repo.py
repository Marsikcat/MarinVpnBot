"""Запросы к БД. Тонкий слой поверх SQLAlchemy, чтобы хендлеры не знали про ORM."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import and_, func, not_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Payment,
    PaymentStatus,
    Plan,
    PromoCode,
    PromoUse,
    Setting,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)


# ---------------------------------------------------------------- users
async def get_user(session: AsyncSession, user_id: int) -> Optional[User]:
    return await session.get(User, user_id)


async def get_or_create_user(
    session: AsyncSession,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    referrer_id: Optional[int] = None,
) -> tuple[User, bool]:
    user = await session.get(User, user_id)
    if user:
        changed = False
        if username != user.username:
            user.username, changed = username, True
        if first_name and first_name != user.first_name:
            user.first_name, changed = first_name, True
        user.last_seen_at = utcnow()
        if changed:
            await session.commit()
        return user, False

    if referrer_id == user_id:
        referrer_id = None
    if referrer_id is not None and not await session.get(User, referrer_id):
        referrer_id = None

    user = User(id=user_id, username=username, first_name=first_name, referrer_id=referrer_id)
    session.add(user)
    await session.commit()
    return user, True


async def count_users(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def count_new_users(session: AsyncSession, since: datetime) -> int:
    return int(await session.scalar(select(func.count()).select_from(User).where(User.created_at >= since)) or 0)


async def count_referrals(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(User).where(User.referrer_id == user_id)) or 0)


async def all_user_ids(session: AsyncSession) -> Sequence[int]:
    rows = await session.scalars(select(User.id).where(User.is_banned.is_(False)))
    return list(rows)


def _users_stmt(scope: str):
    """Выборка пользователей по фильтру: все / с активной подпиской / без неё."""
    stmt = select(User)
    if scope == "active":
        stmt = stmt.join(Subscription, Subscription.user_id == User.id).where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.expires_at > utcnow(),
        )
    elif scope == "inactive":
        active_ids = (
            select(Subscription.user_id)
            .where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.expires_at > utcnow(),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(User.id.not_in(active_ids))
    return stmt


async def count_users_by_scope(session: AsyncSession, scope: str = "all") -> int:
    stmt = _users_stmt(scope).with_only_columns(func.count(User.id)).order_by(None)
    return int(await session.scalar(stmt) or 0)


async def list_users_page(
    session: AsyncSession, page: int = 0, per_page: int = 10, scope: str = "all"
) -> Sequence[User]:
    stmt = (
        _users_stmt(scope)
        .order_by(User.created_at.desc())
        .offset(max(page, 0) * per_page)
        .limit(per_page)
    )
    return list(await session.scalars(stmt))


async def all_users_for_export(session: AsyncSession) -> Sequence[User]:
    return list(await session.scalars(select(User).order_by(User.created_at)))


async def find_users(session: AsyncSession, query: str, limit: int = 10) -> Sequence[User]:
    query = query.strip().lstrip("@")
    stmt = select(User)
    if query.isdigit():
        stmt = stmt.where(User.id == int(query))
    else:
        stmt = stmt.where(User.username.ilike(f"%{query}%"))
    return list(await session.scalars(stmt.limit(limit)))


# ---------------------------------------------------------------- plans
async def list_plans(session: AsyncSession, only_active: bool = True) -> Sequence[Plan]:
    stmt = select(Plan).order_by(Plan.sort_order, Plan.days)
    if only_active:
        stmt = stmt.where(Plan.is_active.is_(True))
    return list(await session.scalars(stmt))


async def get_plan(session: AsyncSession, plan_id: int) -> Optional[Plan]:
    return await session.get(Plan, plan_id)


async def create_plan(
    session: AsyncSession, title: str, days: int, price_rub: float, traffic_gb: int = 0
) -> Plan:
    """Создаёт тариф с автоматическим кодом и порядком в конце списка."""
    max_sort = await session.scalar(select(func.max(Plan.sort_order)))
    codes = set(await session.scalars(select(Plan.code)))
    index = 1
    while f"plan{index}" in codes:
        index += 1

    plan = Plan(
        code=f"plan{index}",
        title=title,
        days=days,
        price_rub=price_rub,
        traffic_gb=int(traffic_gb),
        devices=3,
        is_active=True,
        sort_order=int(max_sort or 0) + 10,
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


async def count_plan_sales(session: AsyncSession, plan_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(Payment)
        .where(Payment.plan_id == plan_id, Payment.status == PaymentStatus.paid)
    )
    return int(await session.scalar(stmt) or 0)


async def plan_payments_exist(session: AsyncSession, plan_id: int) -> bool:
    stmt = select(func.count()).select_from(Payment).where(Payment.plan_id == plan_id)
    return bool(await session.scalar(stmt))


async def delete_plan(session: AsyncSession, plan: Plan) -> None:
    """Удаляет тариф, отвязав его от подписок, чтобы не осталось битых ссылок."""
    await session.execute(
        update(Subscription).where(Subscription.plan_id == plan.id).values(plan_id=None)
    )
    await session.delete(plan)
    await session.commit()


# ---------------------------------------------------------------- subscriptions
async def get_subscription(session: AsyncSession, user_id: int) -> Optional[Subscription]:
    return await session.scalar(select(Subscription).where(Subscription.user_id == user_id))


async def count_active_subscriptions(session: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.active, Subscription.expires_at > utcnow())
    )
    return int(await session.scalar(stmt) or 0)


async def subscriptions_expiring_within(session: AsyncSession, days: int) -> Sequence[Subscription]:
    now = utcnow()
    stmt = select(Subscription).where(
        Subscription.status == SubscriptionStatus.active,
        Subscription.expires_at > now,
        Subscription.expires_at <= now + timedelta(days=days),
    )
    return list(await session.scalars(stmt))


async def subscriptions_expired(session: AsyncSession) -> Sequence[Subscription]:
    stmt = select(Subscription).where(
        Subscription.status == SubscriptionStatus.active,
        Subscription.expires_at <= utcnow(),
    )
    return list(await session.scalars(stmt))


async def active_subscriptions(session: AsyncSession) -> Sequence[Subscription]:
    stmt = select(Subscription).where(Subscription.status == SubscriptionStatus.active)
    return list(await session.scalars(stmt))


# ---------------------------------------------------------------- payments
async def create_payment(
    session: AsyncSession,
    user_id: int,
    plan: Optional[Plan],
    provider: str,
    amount: float,
    currency: str = "RUB",
    days: int = 0,
    promo_code: Optional[str] = None,
) -> Payment:
    payment = Payment(
        user_id=user_id,
        plan_id=plan.id if plan else None,
        provider=provider,
        amount=round(amount, 2),
        currency=currency,
        days=days or (plan.days if plan else 0),
        promo_code=promo_code,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    return payment


async def get_payment(session: AsyncSession, payment_id: int) -> Optional[Payment]:
    return await session.get(Payment, payment_id)


async def get_payment_by_external(session: AsyncSession, provider: str, external_id: str) -> Optional[Payment]:
    stmt = select(Payment).where(Payment.provider == provider, Payment.external_id == external_id)
    return await session.scalar(stmt)


async def pending_payments(session: AsyncSession, provider: str, older_than_minutes: int = 0) -> Sequence[Payment]:
    stmt = select(Payment).where(Payment.provider == provider, Payment.status == PaymentStatus.pending)
    if older_than_minutes:
        stmt = stmt.where(Payment.created_at <= utcnow() - timedelta(minutes=older_than_minutes))
    return list(await session.scalars(stmt))


async def revenue_since(session: AsyncSession, since: datetime) -> float:
    stmt = select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
        Payment.status == PaymentStatus.paid,
        Payment.paid_at >= since,
        Payment.provider != "balance",
    )
    return float(await session.scalar(stmt) or 0.0)


async def total_revenue(session: AsyncSession) -> float:
    stmt = select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
        Payment.status == PaymentStatus.paid, Payment.provider != "balance"
    )
    return float(await session.scalar(stmt) or 0.0)


async def expire_stale_payments(session: AsyncSession, hours: int = 24) -> int:
    stmt = (
        update(Payment)
        .where(
            Payment.status == PaymentStatus.pending,
            Payment.created_at < utcnow() - timedelta(hours=hours),
            # оплату звёздами с charge_id Telegram уже подтвердил — её дожимает retry-задача
            not_(and_(Payment.provider == "stars", Payment.external_id.isnot(None))),
        )
        .values(status=PaymentStatus.canceled)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------- promo
async def get_promo(session: AsyncSession, code: str) -> Optional[PromoCode]:
    return await session.scalar(select(PromoCode).where(PromoCode.code == code.strip().upper()))


async def promo_used_by(session: AsyncSession, promo_id: int, user_id: int) -> bool:
    stmt = select(func.count()).select_from(PromoUse).where(
        PromoUse.promo_id == promo_id, PromoUse.user_id == user_id
    )
    return bool(await session.scalar(stmt))


async def register_promo_use(session: AsyncSession, promo: PromoCode, user_id: int) -> None:
    session.add(PromoUse(promo_id=promo.id, user_id=user_id))
    promo.used_count += 1
    await session.commit()


# ---------------------------------------------------------------- runtime-настройки
async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(Setting, key)
    return row.value if row else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()
