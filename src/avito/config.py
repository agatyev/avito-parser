from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILE = Path("config.yaml")
ENV_FILE = Path(".env")
ENV_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO")
QUIET = ("waitress", "urllib3", "asyncio")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(slots=True)
class Mail:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    to: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Config:
    db: Path = Path("data/avito.db")
    log_level: str = "INFO"
    every_minutes: int = 60
    max_pages: int = 3
    bind: str = "127.0.0.1"
    port: int = 8080
    mail: Mail = field(default_factory=Mail)


def _env() -> dict[str, str]:
    found: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key and not key.startswith("#"):
                found[key] = value.strip().strip("\"'")
    for key in ENV_KEYS:
        if os.environ.get(key):
            found[key] = os.environ[key]
    return found


def _addresses(raw: str) -> list[str]:
    parts = raw.replace(";", ",").split(",")
    return sorted({part.strip().lower() for part in parts if part.strip()})


def load() -> Config:
    raw = {}
    if CONFIG_FILE.exists():
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    env = _env()
    level = str(raw.get("log_level", "INFO")).upper()
    return Config(
        db=Path(raw.get("db") or "data/avito.db"),
        log_level=level if level in LEVELS else "INFO",
        every_minutes=max(5, int(raw.get("every_minutes") or 60)),
        max_pages=max(1, min(int(raw.get("max_pages") or 3), 10)),
        bind=str(raw.get("bind") or "127.0.0.1"),
        port=int(raw.get("port") or 8080),
        mail=Mail(
            host=env.get("SMTP_HOST", ""),
            port=int(env.get("SMTP_PORT") or 587),
            user=env.get("SMTP_USER", ""),
            password=env.get("SMTP_PASS", ""),
            sender=env.get("MAIL_FROM") or env.get("SMTP_USER", ""),
            to=_addresses(env.get("MAIL_TO", "")),
        ),
    )


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-14s %(message)s", datefmt="%d.%m %H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
