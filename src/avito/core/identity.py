from __future__ import annotations

import fcntl
import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path

from ..config import Browser as BrowserConfig

log = logging.getLogger(__name__)

FILE = "identity.json"
LOCK = "identity.lock"
JUNK = ("cache2", "startupCache", "thumbnails", "shader-cache")


@contextmanager
def guard(cfg: BrowserConfig):
    folder = home(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK).open("w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield


def home(cfg: BrowserConfig) -> Path:
    return cfg.profile_dir.expanduser().resolve()


def profile(cfg: BrowserConfig) -> Path:
    return home(cfg) / "profile"


def config(cfg: BrowserConfig) -> dict:
    with guard(cfg):
        return _current(cfg)


def _read(card: Path) -> dict:
    try:
        saved = json.loads(card.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _current(cfg: BrowserConfig) -> dict:
    from camoufox.pkgman import installed_verstr

    card = home(cfg) / FILE
    version = installed_verstr()
    saved = _read(card)
    if saved.get("firefox") == version and saved.get("config"):
        return saved["config"]
    if card.exists():
        log.warning("Личность не годится (Firefox %s вместо %s) — заводим новую",
                    version, saved.get("firefox") or "неизвестно")
        retire(cfg)
    made = {"firefox": version, "config": _make(cfg)}
    home(cfg).mkdir(parents=True, exist_ok=True)
    tmp = card.with_suffix(".tmp")
    tmp.write_text(json.dumps(made, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(card)
    log.info("Личность заведена: %s", made["config"].get("navigator.userAgent"))
    return made["config"]


def decode(env: dict) -> dict:
    chunks = sorted((key for key in env if key.startswith("CAMOU_CONFIG_")),
                    key=lambda key: int(key.rsplit("_", 1)[1]))
    return json.loads("".join(env[key] for key in chunks))


def _make(cfg: BrowserConfig) -> dict:
    from camoufox.fingerprints import clamp_window_dimensions
    from camoufox.utils import launch_options

    base = {"os": cfg.platform, "locale": cfg.locale, "humanize": cfg.humanize,
            "block_webrtc": True, "headless": True, "proxy": cfg.proxy_options()}
    made = decode(launch_options(fingerprint_preset=True, **base)["env"])
    made["timezone"] = cfg.timezone
    made = decode(launch_options(config=made, i_know_what_im_doing=True, **base)["env"])
    clamp_window_dimensions(made)
    return made


def retire(cfg: BrowserConfig) -> None:
    # profile_dir can be configured by the operator.  Never recursively remove
    # that directory itself: it may also contain the database, logs, or even be
    # the application directory.  Only remove files that this module owns.
    shutil.rmtree(profile(cfg), ignore_errors=True)
    (home(cfg) / FILE).unlink(missing_ok=True)
    log.warning("Личность и профиль стёрты — следующий заход сделает новый человек")


def trim(cfg: BrowserConfig) -> None:
    freed = 0
    for name in JUNK:
        target = profile(cfg) / name
        if target.is_dir():
            freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target, ignore_errors=True)
    if freed:
        log.info("Кэш профиля вычищен: %d МБ", freed // (1024 * 1024))
