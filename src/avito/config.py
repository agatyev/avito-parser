from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict, YamlConfigSettingsSource

CONFIG_FILE = "config.yaml"
QUIET = ("apscheduler", "waitress", "urllib3", "apprise", "asyncio")


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Browser(Section):
    headless: bool | Literal["virtual"] = "virtual"
    humanize: bool | Annotated[float, Field(ge=0, le=5)] = 1.5
    platform: Literal["windows", "macos", "linux"] = "windows"
    locale: str = Field(default="ru-RU", min_length=2, max_length=32)
    timezone: str = "Europe/Moscow"
    profile_dir: Path = Path("data/session")
    proxy: str = ""
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    deadline_seconds: int = Field(default=300, ge=30, le=1800)
    retire_after: int = Field(default=3, ge=1, le=20)
    max_pages: int = Field(default=3, ge=1, le=50)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("неизвестный часовой пояс") from None
        return value

    @field_validator("proxy")
    @classmethod
    def valid_proxy(cls, value: str) -> str:
        if not value:
            return value
        parts = urlparse(value)
        if parts.scheme not in ("http", "https", "socks4", "socks5") or not parts.hostname:
            raise ValueError("прокси должен быть URL http(s):// или socks4/5://")
        try:
            _ = parts.port
        except ValueError:
            raise ValueError("неверный порт прокси") from None
        return value

    def proxy_options(self) -> dict | None:
        if not self.proxy:
            return None
        parts = urlparse(self.proxy)
        server = f"{parts.scheme}://{parts.hostname}"
        if parts.port:
            server = f"{server}:{parts.port}"
        options = {"server": server}
        if parts.username:
            options["username"] = parts.username
            options["password"] = parts.password or ""
        return options


class Watch(Section):
    period_minutes: int = Field(default=60, ge=1, le=10080)
    jitter_seconds: int = Field(default=300, ge=0, le=3600)
    max_per_cycle: int = Field(default=50, ge=1, le=500)
    max_age_minutes: int = Field(default=1440, ge=1, le=525600)
    exclude: list[str] = Field(default_factory=list)
    backoff_seconds: int = Field(default=900, ge=30, le=86400)
    max_backoff_seconds: int = Field(default=14400, ge=30, le=604800)
    delivery_batch_size: int = Field(default=50, ge=1, le=500)
    delivery_lease_seconds: int = Field(default=600, ge=30, le=3600)
    delivery_retry_seconds: int = Field(default=300, ge=30, le=86400)

    @model_validator(mode="after")
    def valid_backoff(self):
        if self.max_backoff_seconds < self.backoff_seconds:
            raise ValueError("max_backoff_seconds не меньше backoff_seconds")
        return self


class Web(Section):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file=CONFIG_FILE, env_file=".env", env_nested_delimiter="__", extra="ignore")

    db: Path = Path("data/avito.db")
    log_level: str = "INFO"
    notify: Annotated[list[str], NoDecode] = Field(default_factory=list)
    control: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["web"])
    browser: Browser = Field(default_factory=Browser)
    watch: Watch = Field(default_factory=Watch)
    web: Web = Field(default_factory=Web)

    @field_validator("notify", "control", mode="before")
    @classmethod
    def split_words(cls, value):
        return value.split() if isinstance(value, str) else value

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("неизвестный уровень логов")
        return level

    @field_validator("control")
    @classmethod
    def valid_controls(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in value):
            raise ValueError("неверное имя модуля управления")
        return value

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings, env_settings, dotenv_settings,
                YamlConfigSettingsSource(settings_cls))


def load() -> Settings:
    return Settings()


def setup_logging(level: str, history: Path | None = None) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)-16s %(message)s"
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger().addHandler(console)
    logging.getLogger().setLevel(logging.DEBUG)
    if history:
        history.parent.mkdir(parents=True, exist_ok=True)
        keep = RotatingFileHandler(history, maxBytes=5_000_000,
                                   backupCount=3, encoding="utf-8")
        keep.setFormatter(logging.Formatter(fmt, datefmt="%d.%m %H:%M:%S"))
        keep.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(keep)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
