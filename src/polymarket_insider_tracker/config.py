"""Configuration management service with Pydantic Settings.

This module provides centralized configuration management for the
Polymarket Insider Tracker application, loading and validating
environment variables at startup.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    model_config = SettingsConfigDict(env_prefix="")

    url: str = Field(
        alias="DATABASE_URL",
        description="PostgreSQL connection string",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate database URL format."""
        if not v.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return v


class RedisSettings(BaseSettings):
    """Redis connection settings."""

    model_config = SettingsConfigDict(env_prefix="")

    url: str = Field(
        default="redis://localhost:6379",
        alias="REDIS_URL",
        description="Redis connection string",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate Redis URL format."""
        if not v.startswith("redis://"):
            raise ValueError("REDIS_URL must start with redis://")
        return v


class PolygonSettings(BaseSettings):
    """Polygon blockchain RPC settings.

    RPC providers are discovered dynamically from environment variables
    matching the pattern POLYGON_RPC_URL_{PROVIDER_NAME}. For example:
        POLYGON_RPC_URL_INFURA=https://polygon-mainnet.infura.io/v3/KEY
        POLYGON_RPC_URL_ALCHEMY=https://polygon-mainnet.g.alchemy.com/v2/KEY
        POLYGON_RPC_URL_PUBLICNODE=https://polygon-bor.publicnode.com

    Provider names are derived from the suffix (lowercased).
    """

    model_config = SettingsConfigDict(env_prefix="POLYGON_", extra="ignore")

    rpc_providers: dict[str, str] = Field(
        default_factory=dict,
        description="Provider name -> RPC URL mapping (populated from POLYGON_RPC_URL_* env vars)",
    )

    @model_validator(mode="before")
    @classmethod
    def discover_rpc_providers(cls, data: dict) -> dict:
        """Scan environment for POLYGON_RPC_URL_* variables."""
        providers: dict[str, str] = {}
        prefix = "POLYGON_RPC_URL_"
        for key, value in os.environ.items():
            if key.startswith(prefix) and value:
                provider_name = key[len(prefix) :].lower()
                if not value.startswith(("http://", "https://")):
                    raise ValueError(
                        f"{key} must be an HTTP(S) endpoint, got: {value}"
                    )
                providers[provider_name] = value

        if providers:
            data["rpc_providers"] = providers
        return data

    @model_validator(mode="after")
    def validate_has_providers(self) -> "PolygonSettings":
        """Ensure at least one RPC provider is configured."""
        if not self.rpc_providers:
            raise ValueError(
                "At least one POLYGON_RPC_URL_* environment variable must be set "
                "(e.g. POLYGON_RPC_URL_INFURA=https://...)"
            )
        return self

    @property
    def all_rpc_urls(self) -> list[tuple[str, str]]:
        """Return list of (provider_name, url) tuples."""
        return list(self.rpc_providers.items())


class PolymarketSettings(BaseSettings):
    """Polymarket API settings."""

    model_config = SettingsConfigDict(env_prefix="POLYMARKET_")

    ws_url: str = Field(
        default="wss://ws-live-data.polymarket.com",
        alias="POLYMARKET_WS_URL",
        description="Polymarket WebSocket URL for live data",
    )
    api_key: SecretStr | None = Field(
        default=None,
        alias="POLYMARKET_API_KEY",
        description="Optional Polymarket API key",
    )

    @field_validator("ws_url")
    @classmethod
    def validate_ws_url(cls, v: str) -> str:
        """Validate WebSocket URL format."""
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("WebSocket URL must start with ws:// or wss://")
        return v


class DiscordSettings(BaseSettings):
    """Discord notification settings."""

    model_config = SettingsConfigDict(env_prefix="DISCORD_")

    webhook_url: SecretStr | None = Field(
        default=None,
        alias="DISCORD_WEBHOOK_URL",
        description="Discord webhook URL for alerts",
    )

    @property
    def enabled(self) -> bool:
        """Check if Discord notifications are enabled."""
        return self.webhook_url is not None and self.webhook_url.get_secret_value() != ""


class TelegramSettings(BaseSettings):
    """Telegram notification settings."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    bot_token: SecretStr | None = Field(
        default=None,
        alias="TELEGRAM_BOT_TOKEN",
        description="Telegram bot token",
    )
    chat_id: str | None = Field(
        default=None,
        alias="TELEGRAM_CHAT_ID",
        description="Telegram chat ID for alerts",
    )

    @property
    def enabled(self) -> bool:
        """Check if Telegram notifications are enabled."""
        return (
            self.bot_token is not None
            and self.bot_token.get_secret_value() != ""
            and bool(self.chat_id)
        )


class EmailSettings(BaseSettings):
    """SMTP email notification settings for error alerts."""

    model_config = SettingsConfigDict(env_prefix="EMAIL_", populate_by_name=True)

    smtp_host: str | None = Field(
        default=None,
        alias="EMAIL_SMTP_HOST",
        description="SMTP server hostname",
    )
    smtp_port: int = Field(
        default=587,
        alias="EMAIL_SMTP_PORT",
        description="SMTP server port",
    )
    username: str | None = Field(
        default=None,
        alias="EMAIL_USERNAME",
        description="SMTP authentication username",
    )
    password: SecretStr | None = Field(
        default=None,
        alias="EMAIL_PASSWORD",
        description="SMTP authentication password",
    )
    from_address: str | None = Field(
        default=None,
        alias="EMAIL_FROM",
        description="Sender email address",
    )
    to_addresses: str | None = Field(
        default=None,
        alias="EMAIL_TO",
        description="Comma-separated list of recipient email addresses",
    )
    use_tls: bool = Field(
        default=True,
        alias="EMAIL_USE_TLS",
        description="Use STARTTLS for SMTP connection",
    )
    cooldown_minutes: int = Field(
        default=30,
        alias="EMAIL_COOLDOWN_MINUTES",
        description="Minimum minutes between emails for the same error",
        ge=1,
    )

    @property
    def enabled(self) -> bool:
        """Check if email notifications are enabled."""
        return bool(self.smtp_host and self.from_address and self.to_addresses)

    @property
    def recipients(self) -> list[str]:
        """Parse comma-separated recipient addresses."""
        if not self.to_addresses:
            return []
        return [addr.strip() for addr in self.to_addresses.split(",") if addr.strip()]


class AlertFilterSettings(BaseSettings):
    """Market filtering settings for alert suppression."""

    model_config = SettingsConfigDict(env_prefix="ALERT_", populate_by_name=True)

    include_categories: str | None = Field(
        default=None,
        alias="ALERT_INCLUDE_CATEGORIES",
        description="Comma-separated list of market categories to alert on (e.g. politics,finance,tech,science)",
    )
    exclude_keywords: str | None = Field(
        default=None,
        alias="ALERT_EXCLUDE_KEYWORDS",
        description="Comma-separated keywords — markets containing any are suppressed",
    )

    @property
    def category_set(self) -> set[str]:
        """Parse include_categories into a set."""
        if not self.include_categories:
            return set()
        return {c.strip().lower() for c in self.include_categories.split(",") if c.strip()}

    @property
    def keyword_set(self) -> set[str]:
        """Parse exclude_keywords into a lowercase set."""
        if not self.exclude_keywords:
            return set()
        return {k.strip().lower() for k in self.exclude_keywords.split(",") if k.strip()}

    @property
    def enabled(self) -> bool:
        """Check if any filtering is configured."""
        return bool(self.category_set or self.keyword_set)

    def should_alert(self, category: str, market_title: str) -> bool:
        """Check if a market passes the filter.

        Args:
            category: Market category (e.g. "crypto", "politics").
            market_title: Market title/question text.

        Returns:
            True if the market should generate alerts.
        """
        # Category include filter
        cats = self.category_set
        if cats and category.lower() not in cats:
            return False

        # Keyword exclude filter
        title_lower = market_title.lower()
        for keyword in self.keyword_set:
            if keyword in title_lower:
                return False

        return True


class Settings(BaseSettings):
    """Main application settings.

    Loads configuration from environment variables with support for
    .env files via python-dotenv.

    Example:
        ```python
        from polymarket_insider_tracker.config import get_settings

        settings = get_settings()
        print(settings.database.url)
        print(settings.log_level)
        ```
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Nested configuration groups
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    polygon: PolygonSettings = Field(default_factory=PolygonSettings)
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    alert_filter: AlertFilterSettings = Field(default_factory=AlertFilterSettings)

    # Application settings
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        alias="LOG_LEVEL",
        description="Logging level",
    )
    health_port: int = Field(
        default=8080,
        alias="HEALTH_PORT",
        description="HTTP port for health check endpoints",
        ge=1,
        le=65535,
    )
    dry_run: bool = Field(
        default=False,
        alias="DRY_RUN",
        description="Run without sending actual alerts",
    )
    heartbeat_interval_minutes: int = Field(
        default=240,
        alias="HEARTBEAT_INTERVAL_MINUTES",
        description="Minutes between heartbeat notifications (0 to disable)",
        ge=0,
    )
    heartbeat_start_hour: int = Field(
        default=9,
        alias="HEARTBEAT_START_HOUR",
        description="Hour (server time) to start sending heartbeats",
        ge=0,
        le=23,
    )
    heartbeat_end_hour: int = Field(
        default=21,
        alias="HEARTBEAT_END_HOUR",
        description="Hour (server time) to stop sending heartbeats",
        ge=0,
        le=23,
    )

    def get_logging_level(self) -> int:
        """Get the numeric logging level."""
        level: int = getattr(logging, self.log_level)
        return level

    def redacted_summary(self) -> dict[str, str | dict[str, str]]:
        """Get a summary of settings with secrets redacted.

        Returns:
            Dictionary of settings with sensitive values masked.
        """
        return {
            "database_url": self._redact_url(self.database.url),
            "redis_url": self._redact_url(self.redis.url),
            "polygon": {
                "providers": list(self.polygon.rpc_providers.keys()),
            },
            "polymarket": {
                "ws_url": self.polymarket.ws_url,
                "api_key": "(set)" if self.polymarket.api_key else "(not set)",
            },
            "discord_enabled": str(self.discord.enabled),
            "telegram_enabled": str(self.telegram.enabled),
            "email_enabled": str(self.email.enabled),
            "log_level": self.log_level,
            "health_port": str(self.health_port),
            "dry_run": str(self.dry_run),
        }

    @staticmethod
    def _redact_url(url: str) -> str:
        """Redact password from URL if present."""
        if "@" in url and "://" in url:
            # URL has credentials - redact the password
            protocol_end = url.index("://") + 3
            at_pos = url.index("@")
            creds_part = url[protocol_end:at_pos]
            if ":" in creds_part:
                username = creds_part.split(":")[0]
                return f"{url[:protocol_end]}{username}:***@{url[at_pos + 1 :]}"
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get the application settings singleton.

    Uses LRU cache to ensure settings are loaded only once and
    reused across the application.

    Returns:
        The Settings instance.

    Raises:
        ValidationError: If required environment variables are missing
            or have invalid values.
    """
    return Settings()


def clear_settings_cache() -> None:
    """Clear the settings cache.

    Useful for testing when you need to reload settings with
    different environment variables.
    """
    get_settings.cache_clear()
