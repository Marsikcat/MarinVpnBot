"""Конфигурация бота: читается из .env через pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _split_ints(raw: str) -> List[int]:
    return [int(x) for x in str(raw).replace(";", ",").split(",") if x.strip().isdigit()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    bot_token: str = Field("", alias="BOT_TOKEN")
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")
    support_url: str = Field("https://t.me/", alias="SUPPORT_URL")
    channel_url: str = Field("", alias="CHANNEL_URL")
    required_channel_id: str = Field("", alias="REQUIRED_CHANNEL_ID")

    # --- DB ---
    database_url: str = Field("sqlite+aiosqlite:///./data/vpnbot.db", alias="DATABASE_URL")

    # --- VPN ---
    vpn_provider: str = Field("mock", alias="VPN_PROVIDER")

    marzban_url: str = Field("", alias="MARZBAN_URL")
    marzban_username: str = Field("", alias="MARZBAN_USERNAME")
    marzban_password: str = Field("", alias="MARZBAN_PASSWORD")
    marzban_inbounds_raw: str = Field("", alias="MARZBAN_INBOUNDS")
    marzban_proxies_raw: str = Field("vless", alias="MARZBAN_PROXIES")
    marzban_sub_domain: str = Field("", alias="MARZBAN_SUB_DOMAIN")

    xui_url: str = Field("", alias="XUI_URL")
    xui_username: str = Field("", alias="XUI_USERNAME")
    xui_password: str = Field("", alias="XUI_PASSWORD")
    # пусто = клиент заводится во всех inbound'ах панели
    xui_inbound_ids_raw: str = Field("", alias="XUI_INBOUND_IDS")
    xui_sub_base: str = Field("", alias="XUI_SUB_BASE")

    # --- Trial ---
    trial_enabled: bool = Field(True, alias="TRIAL_ENABLED")
    trial_days: int = Field(3, alias="TRIAL_DAYS")
    trial_traffic_gb: int = Field(10, alias="TRIAL_TRAFFIC_GB")
    # 0 = безлимит; применяется к новым тарифам и выдаче дней вручную
    default_traffic_gb: int = Field(0, alias="DEFAULT_TRAFFIC_GB")

    # --- Referrals ---
    referral_enabled: bool = Field(True, alias="REFERRAL_ENABLED")
    referral_percent: int = Field(20, alias="REFERRAL_PERCENT")
    referral_bonus_days: int = Field(0, alias="REFERRAL_BONUS_DAYS")

    # --- Payments ---
    pay_stars_enabled: bool = Field(True, alias="PAY_STARS_ENABLED")
    stars_rub_rate: float = Field(1.6, alias="STARS_RUB_RATE")

    pay_yookassa_enabled: bool = Field(False, alias="PAY_YOOKASSA_ENABLED")
    yookassa_shop_id: str = Field("", alias="YOOKASSA_SHOP_ID")
    yookassa_secret_key: str = Field("", alias="YOOKASSA_SECRET_KEY")
    yookassa_return_url: str = Field("https://t.me/", alias="YOOKASSA_RETURN_URL")

    # СБП «вручную»: перевод по номеру телефона + подтверждение админом.
    # Подходит для небольшого числа клиентов и не требует эквайринга.
    pay_sbp_enabled: bool = Field(False, alias="PAY_SBP_ENABLED")
    sbp_phone: str = Field("", alias="SBP_PHONE")
    sbp_bank: str = Field("", alias="SBP_BANK")
    sbp_receiver: str = Field("", alias="SBP_RECEIVER")

    pay_cryptobot_enabled: bool = Field(False, alias="PAY_CRYPTOBOT_ENABLED")
    cryptobot_token: str = Field("", alias="CRYPTOBOT_TOKEN")
    cryptobot_asset: str = Field("USDT", alias="CRYPTOBOT_ASSET")
    usd_rub_rate: float = Field(95.0, alias="USD_RUB_RATE")

    # --- Webhooks ---
    webhook_enabled: bool = Field(False, alias="WEBHOOK_ENABLED")
    webhook_host: str = Field("0.0.0.0", alias="WEBHOOK_HOST")
    webhook_port: int = Field(8080, alias="WEBHOOK_PORT")
    webhook_base_url: str = Field("", alias="WEBHOOK_BASE_URL")
    webhook_secret: str = Field("change-me", alias="WEBHOOK_SECRET")

    # --- Misc ---
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    tz: str = Field("Europe/Moscow", alias="TZ")

    @field_validator("vpn_provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"mock", "marzban", "xui"}:
            raise ValueError("VPN_PROVIDER должен быть mock, marzban или xui")
        return v

    # ---- производные значения ----
    @property
    def admin_ids(self) -> List[int]:
        return _split_ints(self.admin_ids_raw)

    @property
    def xui_inbound_ids(self) -> List[int]:
        return _split_ints(self.xui_inbound_ids_raw)

    @property
    def marzban_inbounds(self) -> List[str]:
        return [x.strip() for x in self.marzban_inbounds_raw.split(",") if x.strip()]

    @property
    def marzban_proxies(self) -> List[str]:
        return [x.strip().lower() for x in self.marzban_proxies_raw.split(",") if x.strip()] or ["vless"]

    @property
    def required_channel(self) -> Optional[str]:
        raw = self.required_channel_id.strip()
        return raw or None

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def webhook_url(self, path: str) -> str:
        base = self.webhook_base_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    @property
    def enabled_payment_methods(self) -> List[str]:
        methods = []
        if self.pay_stars_enabled:
            methods.append("stars")
        if self.pay_yookassa_enabled and self.yookassa_shop_id and self.yookassa_secret_key:
            methods.append("yookassa")
        if self.pay_sbp_enabled and self.sbp_phone and self.admin_ids:
            methods.append("sbp")
        if self.pay_cryptobot_enabled and self.cryptobot_token:
            methods.append("cryptobot")
        return methods


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
