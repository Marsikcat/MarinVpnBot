"""ORM-модели."""
from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Наивный UTC — одинаково ведёт себя в SQLite и Postgres."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    canceled = "canceled"
    failed = "failed"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    disabled = "disabled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # telegram id
    username: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(8), default="ru")
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    subscription: Mapped[Optional["Subscription"]] = relationship(
        back_populates="user", uselist=False, lazy="selectin"
    )

    @property
    def title(self) -> str:
        if self.username:
            return "@" + self.username
        return self.first_name or str(self.id)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    price_rub: Mapped[float] = mapped_column(Float)
    traffic_gb: Mapped[int] = mapped_column(Integer, default=0)  # 0 = безлимит
    devices: Mapped[int] = mapped_column(Integer, default=3)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)

    @property
    def traffic_bytes(self) -> int:
        return self.traffic_gb * 1024 ** 3

    @property
    def price_per_month(self) -> float:
        months = max(self.days / 30, 0.01)
        return self.price_rub / months


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False), default=SubscriptionStatus.active
    )
    vpn_username: Mapped[str] = mapped_column(String(64))
    vpn_uuid: Mapped[Optional[str]] = mapped_column(String(64))
    subscription_url: Mapped[Optional[str]] = mapped_column(Text)
    links: Mapped[Optional[str]] = mapped_column(Text)  # JSON-массив строк
    traffic_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_used: Mapped[int] = mapped_column(BigInteger, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="subscription", lazy="selectin")
    plan: Mapped[Optional["Plan"]] = relationship(lazy="selectin")

    @property
    def is_active(self) -> bool:
        return self.status == SubscriptionStatus.active and self.expires_at > utcnow()

    @property
    def days_left(self) -> int:
        delta = self.expires_at - utcnow()
        if delta <= timedelta(0):
            return 0
        return delta.days + (1 if delta.seconds else 0)

    @property
    def seconds_left(self) -> int:
        return max(int((self.expires_at - utcnow()).total_seconds()), 0)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"))
    provider: Mapped[str] = mapped_column(String(32))  # stars | yookassa | cryptobot | sbp
    external_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False), default=PaymentStatus.pending, index=True
    )
    days: Mapped[int] = mapped_column(Integer, default=0)
    pay_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    user: Mapped["User"] = relationship(lazy="selectin")
    plan: Mapped[Optional["Plan"]] = relationship(lazy="selectin")


class Setting(Base):
    """Key-value для настроек, которые админ меняет прямо в боте.

    Значения хранятся строками, разбор и валидация — в bot/services/runtime.py.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
