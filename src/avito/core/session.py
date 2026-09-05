from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
from collections.abc import Iterable

from .. import url as urls
from ..config import Browser as BrowserConfig
from ..parser import CARD, blocked, item_ids
from . import Blocked, FetchResult, identity

log = logging.getLogger(__name__)

HOME = "https://www.avito.ru/"
REFERERS = (None, "https://ya.ru/", "https://www.google.com/", "https://dzen.ru/")
SEARCH_INPUT = ('[data-marker="search-form/suggest"] input, '
                'form[data-marker="search-form"] input[type="text"], input[name="query"]')
CARD_TIMEOUT = 15000
MARKS = ("u", "sx")
LAUNCH_TIMEOUT = 180
SOFT_TIMEOUT = 20
HARD_MAX_PAGES = 50


async def _step(name: str, coro, timeout: float):
    try:
        return await asyncio.wait_for(coro, timeout)
    except TimeoutError:
        raise TimeoutError(f"{name}: браузер не ответил за {timeout:.0f} с") from None


async def _trace(ctx) -> None:
    cookies = await ctx.cookies()
    names = [c["name"] for c in cookies if "avito" in c.get("domain", "")]
    marks = [name for name in MARKS if name in names] or ["новых"]
    log.info("Захожу с главной: куки Avito %d, метка %s", len(names), "+".join(marks))


async def _pause(page, lo: float, hi: float) -> None:
    await page.wait_for_timeout(random.uniform(lo, hi) * 1000)


async def _scroll(page, screens: tuple[int, int] = (2, 5)) -> None:
    for _ in range(random.randint(*screens)):
        await page.mouse.wheel(0, random.randint(300, 900))
        await _pause(page, 0.4, 1.6)
        if random.random() < 0.25:
            await page.mouse.wheel(0, -random.randint(100, 400))
            await _pause(page, 0.5, 1.5)


async def _type(page, text: str) -> None:
    await page.keyboard.type(text, delay=random.randint(70, 240))


async def _search(page, phrase: str, soft) -> bool:
    if not phrase:
        return False
    field = page.locator(SEARCH_INPUT).first
    for name, make in (
            ("поиск: ожидание строки", lambda: field.wait_for(state="visible", timeout=5000)),
            ("поиск: фокус", lambda: field.focus()),
            ("поиск: пауза", lambda: _pause(page, 0.3, 0.9)),
            ("поиск: ввод слов", lambda: _type(page, phrase)),
            ("поиск: Enter", lambda: page.keyboard.press("Enter")),
            ("поиск: загрузка", lambda: page.wait_for_load_state("domcontentloaded")),
            ("поиск: пауза", lambda: _pause(page, 1.5, 3.5))):
        if not await soft(name, make):
            return False
    return True


async def _walk(cfg: BrowserConfig, page, link: str, end: float,
                known_ids: Iterable[str] = (), max_pages: int = 1) -> FetchResult:
    def budget(reserve: float = 0.0) -> float:
        left = end - time.monotonic() - reserve
        if left <= 0:
            raise TimeoutError("вышел бюджет времени на заход")
        return min(left, cfg.timeout_seconds)

    async def step(name: str, make, reserve: float = 0.0):
        timeout = budget(reserve)
        return await _step(name, make(), timeout)

    async def soft(name: str, make) -> bool:
        try:
            timeout = min(budget(), SOFT_TIMEOUT)
            await _step(name, make(), timeout)
            return True
        except Exception as exc:
            log.info("Пропускаю необязательный шаг, %s", exc)
            return False

    started = time.monotonic()
    await step("главная: переход",
               lambda: page.goto(HOME, referer=random.choice(REFERERS),
                                 wait_until="domcontentloaded"), reserve=CARD_TIMEOUT / 1000)
    log.info("Главная открылась за %d с", time.monotonic() - started)
    await soft("главная: пауза", lambda: _pause(page, 1.5, 4.0))
    await soft("главная: скролл", lambda: _scroll(page, (1, 3)))
    if await _search(page, urls.phrase(link), soft):
        log.info("Поиск словами: %s", page.url)

    known = {str(item_id) for item_id in known_ids}
    signatures: set[frozenset[str]] = set()
    chunks = []
    limit = max(1, min(int(max_pages), HARD_MAX_PAGES))

    for number in range(1, limit + 1):
        label = "выдача" if number == 1 else f"выдача {number}"
        listing_at = time.monotonic()
        target = urls.page(link, number)
        referer = page.url
        response = await step(
            f"{label}: переход",
            lambda target=target, referer=referer: page.goto(
                target, referer=referer, wait_until="domcontentloaded"))
        status = response.status if response else 0
        await soft(f"{label}: пауза", lambda: _pause(page, 1.0, 2.5))
        await soft(f"{label}: карточки",
                   lambda: page.wait_for_selector(CARD, timeout=CARD_TIMEOUT))
        await soft(f"{label}: скролл", lambda: _scroll(page))

        html = await step(f"{label}: снятие HTML", lambda: page.content())
        log.info("Выдача %d снята: HTTP %s, %d символов, за %d с",
                 number, status, len(html), time.monotonic() - listing_at)
        if blocked(html, status):
            title = ""
            with contextlib.suppress(Exception):
                timeout = min(budget(), 5)
                title = await _step("заголовок страницы", page.title(), timeout)
            raise Blocked(f"антибот Avito: HTTP {status}, заголовок {title!r}")
        if status >= 400:
            raise RuntimeError(f"Avito вернул HTTP {status} на странице {number}")

        ids = item_ids(html)
        if not ids:
            if not chunks:
                chunks.append(html)
            return FetchResult("\n".join(chunks), number, "empty")

        signature = frozenset(ids)
        if signature in signatures:
            return FetchResult("\n".join(chunks), number, "repeated")
        signatures.add(signature)
        chunks.append(html)
        if known and signature <= known:
            return FetchResult("\n".join(chunks), number, "known_page")

    return FetchResult("\n".join(chunks), limit, "page_limit", limit_reached=True)


async def _grab(cfg: BrowserConfig, link: str, known_ids: Iterable[str] = (),
                max_pages: int = 1) -> FetchResult:
    identity.trim(cfg)
    options = {"headless": cfg.headless, "config": identity.config(cfg),
               "os": cfg.platform, "locale": cfg.locale, "humanize": cfg.humanize,
               "block_webrtc": True, "i_know_what_im_doing": True,
               "persistent_context": True, "user_data_dir": str(identity.profile(cfg))}
    if display := os.environ.get("DISPLAY"):
        options["headless"] = False
        options["virtual_display"] = display
    if proxy := cfg.proxy_options():
        options["proxy"] = proxy
        log.info("Выходим через прокси %s", proxy["server"])

    from camoufox.async_api import AsyncCamoufox

    browser = AsyncCamoufox(**options)
    try:
        ctx = await _step("запуск браузера", browser.__aenter__(), LAUNCH_TIMEOUT)
        ctx.set_default_timeout(cfg.timeout_seconds * 1000)
        ctx.set_default_navigation_timeout(cfg.timeout_seconds * 1000)
        await _step("куки", _trace(ctx), cfg.timeout_seconds)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await _walk(cfg, page, link, time.monotonic() + cfg.deadline_seconds,
                           known_ids, max_pages)
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(browser.__aexit__(None, None, None), 20)


def grab(cfg: BrowserConfig, link: str, known_ids: Iterable[str] = (), max_pages: int = 1,
         detailed: bool = False) -> str | FetchResult:
    result = asyncio.run(_grab(cfg, link, known_ids, max_pages))
    return result if detailed else result.html
