"""Формирование текстов, зависящих от состояния подписки."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from bot.db.models import Subscription, SubscriptionStatus, utcnow
from bot.texts import ru


def time_left(subscription: Subscription) -> str:
    seconds = subscription.seconds_left
    if seconds <= 0:
        return "истекла"
    days = seconds // 86400
    if days >= 1:
        return f"осталось {ru.plural_days(days)}"
    hours = seconds // 3600
    if hours >= 1:
        return f"осталось {hours} ч"
    return "осталось меньше часа"


def traffic_line(subscription: Subscription) -> str:
    if not subscription.traffic_limit:
        return "Трафик: <b>безлимит</b>\n"
    used = ru.human_bytes(subscription.traffic_used)
    total = ru.human_bytes(subscription.traffic_limit)
    return f"Трафик: <b>{used}</b> из {total}\n"


def render_access(subscription: Optional[Subscription]) -> str:
    if subscription is None:
        return ru.NO_SUBSCRIPTION.format(trial_hint=ru.trial_hint())

    # доступ выключили в панели, хотя срок ещё не вышел
    if subscription.status == SubscriptionStatus.disabled and subscription.expires_at > utcnow():
        until = f"{subscription.expires_at:%d.%m.%Y}"
        limit = subscription.traffic_limit
        if limit and subscription.traffic_used >= limit:
            # панель отключает клиента, когда кончился трафик, — это лечится продлением
            return ru.SUBSCRIPTION_TRAFFIC_OUT.format(until=until, traffic=traffic_line(subscription))
        return ru.SUBSCRIPTION_DISABLED.format(until=until)

    if not subscription.is_active:
        return ru.SUBSCRIPTION_EXPIRED.format(until=f"{subscription.expires_at:%d.%m.%Y}")

    text = ru.SUBSCRIPTION_ACTIVE.format(
        trial=" (пробный период)" if subscription.is_trial else "",
        until=f"{subscription.expires_at:%d.%m.%Y %H:%M} UTC",
        left=time_left(subscription),
        traffic=traffic_line(subscription),
        sub_url=subscription.subscription_url or "—",
    )

    return text


def plans_header(subscription: Optional[Subscription]) -> str:
    """Заголовок витрины: покупка, продление или возобновление."""
    if subscription is None:
        return ru.PLANS_HEADER
    # Продление считается от оставшегося срока, даже если доступ сейчас приостановлен
    # (например, кончился трафик), — показываем то же, что потом и случится.
    if subscription.expires_at > utcnow():
        return ru.PLANS_HEADER_RENEW.format(
            until=f"{subscription.expires_at:%d.%m.%Y}",
            left=time_left(subscription),
        )
    return ru.PLANS_HEADER_EXPIRED.format(until=f"{subscription.expires_at:%d.%m.%Y}")


def render_plan(plan, subscription: Optional[Subscription] = None) -> str:
    per_month = ""
    if plan.days >= 60:
        per_month = f" (~{plan.price_per_month:.0f} ₽/мес)"
    traffic = "Трафик: безлимит\n" if not plan.traffic_gb else f"Трафик: {plan.traffic_gb} ГБ\n"
    text = ru.PLAN_CARD.format(
        title=plan.title,
        days=ru.plural_days(plan.days),
        price=plan.price_rub,
        per_month=per_month,
        traffic=traffic,
        devices=plan.devices,
    )

    # при продлении сразу показываем, до какой даты продлится доступ
    if subscription is not None:
        # та же база, что и при выдаче: остаток срока, а если он вышел — сегодня
        now = utcnow()
        running = subscription.expires_at > now
        base = subscription.expires_at if running else now
        tail = "\nВыберите способ оплаты:"
        renew_line = ru.PLAN_RENEW_LINE.format(
            until_now=f"{subscription.expires_at:%d.%m.%Y}" if running else "истекла",
            until_new=f"{base + timedelta(days=plan.days):%d.%m.%Y}",
        )
        text = text.replace(tail, renew_line + tail)
    return text


def render_profile(user, subscription: Optional[Subscription]) -> str:
    status = ru.subscription_status_line(
        subscription.expires_at if subscription else None,
        bool(subscription and subscription.is_active),
    )
    return ru.PROFILE.format(
        id=user.id,
        status=status,
        registered=f"{user.created_at:%d.%m.%Y}",
    )
