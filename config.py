"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # python-dotenv is optional at runtime (Railway injects real env vars)
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    admin_ids: frozenset
    database_path: str
    timezone: str
    group_ids: frozenset
    log_level: str


def _parse_ints(raw: str, name: str) -> frozenset:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-numeric value: {part!r}") from exc
    return frozenset(out)


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    if env is None:
        if load_dotenv:
            load_dotenv()
        env = os.environ
    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is required")
    try:
        owner_id = int((env.get("OWNER_ID") or "").strip())
    except ValueError as exc:
        raise ConfigError("OWNER_ID must be your numeric Telegram user ID") from exc
    tz = (env.get("TIMEZONE") or "Asia/Kolkata").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown TIMEZONE: {tz}") from exc
    return Config(
        bot_token=token,
        owner_id=owner_id,
        admin_ids=_parse_ints(env.get("ADMIN_IDS", ""), "ADMIN_IDS"),
        database_path=(env.get("DATABASE_PATH") or "./data/freefire.db").strip(),
        timezone=tz,
        group_ids=_parse_ints(env.get("GROUP_IDS", ""), "GROUP_IDS"),
        log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
    )
