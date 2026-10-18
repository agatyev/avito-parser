from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

from .browse import HOME, HUMANIZE, LAUNCH_TIMEOUT, LOCALE, PLATFORM, identity, trim_cache
from .config import load, setup_logging

log = logging.getLogger(__name__)

READY = ".ready"
WAIT_MINUTES = 30
SETTLE = 6000
MESSENGER = "https://www.avito.ru/profile/messenger"


def profile(data: Path) -> Path:
    return data / "account"


async def open_browser(data: Path, headless: bool = True):
    from camoufox.async_api import AsyncCamoufox

    home = profile(data)
    home.mkdir(parents=True, exist_ok=True)
    trim_cache(home)
    browser = AsyncCamoufox(
        headless=headless, config=identity(home), os=PLATFORM, locale=LOCALE,
        humanize=HUMANIZE, block_webrtc=True, i_know_what_im_doing=True,
        persistent_context=True, user_data_dir=str(home))
    context = await asyncio.wait_for(browser.__aenter__(), LAUNCH_TIMEOUT)
    return browser, context


async def close_browser(browser) -> None:
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(browser.__aexit__(None, None, None), 20)


async def _login(data: Path) -> None:
    flag = profile(data) / READY
    flag.unlink(missing_ok=True)
    browser, context = await open_browser(data, headless=False)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(HOME, wait_until="domcontentloaded")
        log.warning("Окно открыто. Войдите в аккаунт руками. Жду файл %s", flag)
        deadline = time.monotonic() + WAIT_MINUTES * 60
        while not flag.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"вход не подтверждён за {WAIT_MINUTES} мин")
            await page.wait_for_timeout(2000)
        await page.goto(MESSENGER, wait_until="domcontentloaded")
        await page.wait_for_timeout(SETTLE)
        html = await page.content()
        (data / "messenger.html").write_text(html, encoding="utf-8")
        log.warning("Мессенджер снят: %s, %d символов", page.url, len(html))
    finally:
        await close_browser(browser)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cfg = load()
    setup_logging(cfg.log_level)
    if args[:1] == ["login"]:
        asyncio.run(_login(cfg.db.parent))
        return 0
    print("использование: python -m avito.account login")
    return 2


if __name__ == "__main__":
    sys.exit(main())
