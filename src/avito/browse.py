from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .parse import CARD, blocked, item_ids

log = logging.getLogger(__name__)

HOME = "https://www.avito.ru/"
HOST = "avito.ru"
BY_DATE = "104"
REFERERS = (None, "https://ya.ru/", "https://www.google.com/", "https://dzen.ru/")
PLATFORM = "windows"
LOCALE = "ru-RU"
TIMEZONE = "Europe/Moscow"
HUMANIZE = 1.5
LAUNCH_TIMEOUT = 180
STEP_TIMEOUT = 60
SOFT_TIMEOUT = 20
PASS_DEADLINE = 300
CARD_TIMEOUT = 15000
ITEM_SLUG = re.compile(r"_\d{8,}$")
NON_SEARCH_ROOTS = {"items", "user", "profile", "brands"}
CACHE_JUNK = ("cache2", "startupCache", "thumbnails", "shader-cache")


class Blocked(RuntimeError):
    pass


def check_search(url: str):
    parts = urlparse((url or "").strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or (host != HOST and not host.endswith("." + HOST)):
        raise ValueError("Это не ссылка на Avito")
    if parts.username or parts.password:
        raise ValueError("В ссылке не должно быть логина и пароля — "
                         "скопируйте адрес из строки браузера заново")
    segments = [segment.casefold() for segment in parts.path.split("/") if segment]
    if any(segment.startswith("on-map") for segment in segments):
        raise ValueError("Это ссылка на карту — с неё объявления не читаются. "
                         "Переключите вид на «Списком» и скопируйте адрес заново")
    root = segments[0] if segments else ""
    last = segments[-1] if segments else ""
    if root in NON_SEARCH_ROOTS or "profile" in segments or ITEM_SLUG.search(last):
        raise ValueError("Это не ссылка на поиск Avito — скопируйте адрес страницы с выдачей")
    return parts


def page_url(url: str, number: int) -> str:
    parts = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in ("p", "s")]
    query.append(("s", BY_DATE))
    if number > 1:
        query.append(("p", str(number)))
    return urlunparse(parts._replace(query=urlencode(query)))


def title_from_url(url: str) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query)).get("q", "").strip()
    segments = [segment.split("-")[0] for segment in parts.path.split("/") if segment][:3]
    bits = [segment.replace("_", " ") for segment in segments if segment != "all"]
    if query:
        bits.append(f"«{query}»")
    return ", ".join(bits) or url


def _decode(env: dict) -> dict:
    chunks = sorted((key for key in env if key.startswith("CAMOU_CONFIG_")),
                    key=lambda key: int(key.rsplit("_", 1)[1]))
    return json.loads("".join(env[key] for key in chunks))


def identity(profile: Path) -> dict:
    from camoufox.pkgman import installed_verstr
    from camoufox.utils import launch_options

    card = profile / "identity.json"
    version = installed_verstr()
    with contextlib.suppress(ValueError, OSError):
        saved = json.loads(card.read_text(encoding="utf-8"))
        if saved.get("firefox") == version:
            return saved["config"]
    base = {"os": PLATFORM, "locale": LOCALE, "humanize": HUMANIZE, "headless": True}
    made = _decode(launch_options(fingerprint_preset=True, **base)["env"])
    made["timezone"] = TIMEZONE
    made = _decode(launch_options(config=made, i_know_what_im_doing=True, **base)["env"])
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(json.dumps({"firefox": version, "config": made}), encoding="utf-8")
    log.info("Личность заведена: %s", made.get("navigator.userAgent"))
    return made


def trim_cache(profile: Path) -> None:
    freed = 0
    for name in CACHE_JUNK:
        target = profile / name
        if target.is_dir():
            freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target, ignore_errors=True)
    if freed:
        log.info("Кэш профиля вычищен: %d МБ", freed // (1024 * 1024))


async def _soft(what: str, coro) -> None:
    try:
        await asyncio.wait_for(coro, SOFT_TIMEOUT)
    except Exception as exc:
        log.info("Пропускаю необязательный шаг «%s»: %s", what, exc)


async def _pause(page, low: float, high: float) -> None:
    await page.wait_for_timeout(random.uniform(low, high) * 1000)


async def _scroll(page, screens: tuple[int, int] = (2, 5)) -> None:
    for _ in range(random.randint(*screens)):
        await page.mouse.wheel(0, random.randint(300, 900))
        await _pause(page, 0.4, 1.6)
        if random.random() < 0.25:
            await page.mouse.wheel(0, -random.randint(100, 400))
            await _pause(page, 0.5, 1.5)


async def _walk(page, link: str, known: set[str], max_pages: int) -> str:
    await page.goto(HOME, referer=random.choice(REFERERS), wait_until="domcontentloaded")
    await _soft("пауза на главной", _pause(page, 1.5, 4.0))
    await _soft("скролл главной", _scroll(page, (1, 3)))

    chunks: list[str] = []
    signatures: set[frozenset[str]] = set()
    for number in range(1, max_pages + 1):
        label = f"выдача {number}"
        started = time.monotonic()
        target, referer = page_url(link, number), page.url
        response = await page.goto(target, referer=referer, wait_until="domcontentloaded")
        status = response.status if response else 0
        await _soft(f"{label}: пауза", _pause(page, 1.0, 2.5))
        await _soft(f"{label}: карточки", page.wait_for_selector(CARD, timeout=CARD_TIMEOUT))
        await _soft(f"{label}: скролл", _scroll(page))
        html = await page.content()
        if blocked(html, status):
            raise Blocked(f"антибот Avito: HTTP {status}")
        if status >= 400:
            raise RuntimeError(f"Avito вернул HTTP {status} на странице {number}")
        ids = item_ids(html)
        log.info("%s: HTTP %s, карточек %d, за %d с",
                 label, status, len(ids), time.monotonic() - started)
        signature = frozenset(ids)
        if not ids or signature in signatures:
            if not chunks:
                chunks.append(html)
            break
        signatures.add(signature)
        chunks.append(html)
        if known.intersection(ids):
            break
        if number < max_pages:
            log.info("%s: все карточки новые — иду на следующую страницу", label)
    return "\n".join(chunks)


async def _grab(profile: Path, link: str, known: set[str], max_pages: int) -> str:
    from camoufox.async_api import AsyncCamoufox

    trim_cache(profile)
    browser = AsyncCamoufox(
        headless=True, config=identity(profile), os=PLATFORM, locale=LOCALE,
        humanize=HUMANIZE, block_webrtc=True, i_know_what_im_doing=True,
        persistent_context=True, user_data_dir=str(profile))
    try:
        context = await asyncio.wait_for(browser.__aenter__(), LAUNCH_TIMEOUT)
        context.set_default_timeout(STEP_TIMEOUT * 1000)
        context.set_default_navigation_timeout(STEP_TIMEOUT * 1000)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            return await asyncio.wait_for(_walk(page, link, known, max_pages), PASS_DEADLINE)
        except TimeoutError:
            raise TimeoutError(f"заход не уложился в {PASS_DEADLINE} с") from None
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(browser.__aexit__(None, None, None), 20)


def fetch(profile: Path, link: str, known: Iterable[str] = (), max_pages: int = 3) -> str:
    started = time.monotonic()
    html = asyncio.run(_grab(Path(profile), link, set(known), max_pages))
    log.info("Заход занял %d с", time.monotonic() - started)
    return html
