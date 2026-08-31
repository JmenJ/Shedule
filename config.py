from __future__ import annotations

import os
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_owner_ids(raw_value: str) -> frozenset[int]:
    values = {value.strip() for value in raw_value.split(",") if value.strip()}
    try:
        return frozenset(int(value) for value in values)
    except ValueError as exc:
        raise RuntimeError(
            "BOT_OWNER_IDS должен содержать Telegram ID через запятую"
        ) from exc


def _normalize_database_url(database_url: str) -> str:
    # Render historically provided postgres://, while SQLAlchemy expects postgresql://.
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    database_url: str
    webhook_url: str | None
    webhook_secret: str | None
    default_timezone: str
    setup_code_ttl_hours: int

    @classmethod
    def from_environment(cls) -> Settings:
        bot_token = os.environ.get("BOT_TOKEN", "").strip()
        if not bot_token:
            raise RuntimeError("Не задана обязательная переменная BOT_TOKEN")

        owner_ids = _parse_owner_ids(os.environ.get("BOT_OWNER_IDS", ""))
        if not owner_ids:
            raise RuntimeError("Не задан BOT_OWNER_IDS — Telegram ID владельца бота")

        default_timezone = os.environ.get("DEFAULT_TIMEZONE", "Europe/Moscow").strip()
        try:
            ZoneInfo(default_timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(
                f"Неизвестный DEFAULT_TIMEZONE: {default_timezone}"
            ) from exc

        webhook_url = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/") or None
        webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip() or None
        if webhook_url and not webhook_secret:
            raise RuntimeError("При WEBHOOK_URL необходимо задать WEBHOOK_SECRET")
        if webhook_secret and not re.fullmatch(
            r"[A-Za-z0-9_-]{16,256}", webhook_secret
        ):
            raise RuntimeError(
                "WEBHOOK_SECRET должен содержать 16–256 букв, цифр, знаков _ или -"
            )

        try:
            ttl_hours = int(os.environ.get("SETUP_CODE_TTL_HOURS", "24"))
        except ValueError as exc:
            raise RuntimeError("SETUP_CODE_TTL_HOURS должен быть целым числом") from exc
        if ttl_hours < 1:
            raise RuntimeError("SETUP_CODE_TTL_HOURS должен быть больше нуля")

        database_url = _normalize_database_url(
            os.environ.get("DATABASE_URL", "sqlite:///schedule.db").strip()
        )

        return cls(
            bot_token=bot_token,
            owner_ids=owner_ids,
            database_url=database_url,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            default_timezone=default_timezone,
            setup_code_ttl_hours=ttl_hours,
        )
