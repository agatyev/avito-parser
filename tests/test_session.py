from __future__ import annotations

import asyncio
import gc
import time
import warnings
from urllib.parse import parse_qs, urlparse

import pytest

from avito.config import Browser
from avito.core import Blocked, FetchResult
from avito.core.session import _walk
from avito.parser import parse

LINK = "https://www.avito.ru/moskva/kvartiry/prodam"


def cards(*item_ids: str) -> str:
    return "".join(
        f'<div data-marker="item" data-item-id="{item_id}">'
        f'<a data-marker="item-title" href="/{item_id}">Лот {item_id}</a></div>'
        for item_id in item_ids)


CARDS = cards("1")


def cfg(**kwargs) -> Browser:
    return Browser.model_construct(timeout_seconds=1, deadline_seconds=30, **kwargs)


class Response:
    def __init__(self, status: int) -> None:
        self.status = status


class Locator:
    def __init__(self, page, name: str) -> None:
        self.page = page
        self.name = name

    @property
    def first(self):
        return self

    async def wait_for(self, state: str = "", timeout: float = 0) -> None:
        await self.page._act("поиск: ожидание строки")

    async def focus(self) -> None:
        await self.page._act("поиск: фокус")


class Mouse:
    def __init__(self, page) -> None:
        self.page = page

    async def wheel(self, dx: int, dy: int) -> None:
        await self.page._act("скролл")


class Keyboard:
    def __init__(self, page) -> None:
        self.page = page

    async def type(self, text: str, delay: int = 0) -> None:
        await self.page._act("ввод")

    async def press(self, key: str) -> None:
        await self.page._act("enter")


class FakePage:
    def __init__(self, hang: set[str] | None = None, status: int = 200,
                 html: str | dict[int, str] = CARDS) -> None:
        self.hang = hang or set()
        self.status = status
        self.pages = html if isinstance(html, dict) else {1: html}
        self.current_page = 1
        self.url = "https://www.avito.ru/"
        self.done: list[str] = []
        self.visited: list[str] = []
        self.mouse = Mouse(self)
        self.keyboard = Keyboard(self)

    async def _act(self, name: str) -> None:
        if name in self.hang:
            await asyncio.sleep(30)
        self.done.append(name)

    def locator(self, selector: str) -> Locator:
        return Locator(self, "поле")

    async def goto(self, url: str, **kwargs) -> Response:
        await self._act("переход на главную" if url.endswith(".ru/") else "переход на выдачу")
        self.url = url
        if not url.endswith(".ru/"):
            self.visited.append(url)
            self.current_page = int(parse_qs(urlparse(url).query).get("p", ["1"])[0])
        return Response(self.status)

    async def content(self) -> str:
        await self._act("снятие html")
        return self.pages.get(self.current_page, "<html></html>")

    async def title(self) -> str:
        return "Доступ ограничен"

    async def wait_for_timeout(self, ms: float) -> None:
        await self._act("пауза")

    async def wait_for_selector(self, selector: str, timeout: float = 0) -> None:
        await self._act("карточки")

    async def wait_for_load_state(self, state: str) -> None:
        await self._act("загрузка")


def walk(page, seconds: float = 30, browser: Browser | None = None,
         known_ids=(), max_pages: int = 1) -> FetchResult:
    return asyncio.run(
        _walk(browser or cfg(), page, LINK, time.monotonic() + seconds,
              known_ids, max_pages))


def test_walk_brings_back_the_listing_html():
    page = FakePage()
    assert walk(page).html == CARDS
    assert "снятие html" in page.done


def test_hung_gesture_does_not_break_the_pass():
    page = FakePage(hang={"скролл", "поиск: фокус"})
    assert walk(page).html == CARDS
    assert "снятие html" in page.done


def test_hung_mandatory_step_stops_the_pass():
    page = FakePage(hang={"переход на выдачу"})
    with pytest.raises(TimeoutError, match="выдача: переход"):
        walk(page)


def test_antibot_answer_is_reported_as_blocked():
    with pytest.raises(Blocked, match="403"):
        walk(FakePage(status=403))
    with pytest.raises(Blocked, match="антибот"):
        walk(FakePage(html="<html>Доступ ограничен</html>"))
    with pytest.raises(RuntimeError, match="503"):
        walk(FakePage(status=503))
    with pytest.raises(RuntimeError, match="407"):
        walk(FakePage(status=407))


def test_spent_budget_stops_the_pass_without_stray_coroutines():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(TimeoutError, match="бюджет"):
            walk(FakePage(), seconds=-1)
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]


def test_search_runs_only_when_the_link_gives_words():
    words = FakePage()
    asyncio.run(_walk(cfg(), words, LINK, time.monotonic() + 30))
    assert "поиск: фокус" in words.done

    wordless = FakePage()
    asyncio.run(_walk(cfg(), wordless, "https://www.avito.ru/moskva",
                      time.monotonic() + 30))
    assert "поиск: фокус" not in wordless.done


def test_walk_paginates_until_every_id_on_a_page_is_known():
    page = FakePage(html={
        1: cards("new", "known-1"),
        2: cards("known-2", "known-3"),
        3: cards("must-not-be-read"),
    })

    result = walk(page, known_ids={"known-1", "known-2", "known-3"}, max_pages=5)

    assert (result.pages, result.stop_reason, result.limit_reached) == (2, "known_page", False)
    assert [listing.id for listing in parse(result.html)] == ["new", "known-1",
                                                               "known-2", "known-3"]
    assert len(page.visited) == 2
    assert "p=" not in page.visited[0] and "p=2" in page.visited[1]


def test_walk_stops_on_an_empty_page():
    page = FakePage(html={1: cards("new"), 2: "<html>Ничего не найдено</html>"})

    result = walk(page, max_pages=5)

    assert (result.pages, result.stop_reason, result.limit_reached) == (2, "empty", False)
    assert [listing.id for listing in parse(result.html)] == ["new"]


def test_block_on_a_later_page_rejects_the_whole_pass():
    page = FakePage(html={1: cards("new"), 2: "<html>Verify you are human</html>"})

    with pytest.raises(Blocked, match="антибот"):
        walk(page, max_pages=5)


def test_walk_stops_on_a_repeated_page_without_adding_it_again():
    page = FakePage(html={1: cards("1"), 2: cards("2"), 3: cards("2")})

    result = walk(page, max_pages=5)

    assert (result.pages, result.stop_reason, result.limit_reached) == (3, "repeated", False)
    assert [listing.id for listing in parse(result.html)] == ["1", "2"]


def test_walk_reports_when_the_page_cap_was_reached():
    page = FakePage(html={1: cards("1"), 2: cards("2"), 3: cards("3")})

    result = walk(page, max_pages=2)

    assert (result.pages, result.stop_reason, result.limit_reached) == (2, "page_limit", True)
    assert [listing.id for listing in parse(result.html)] == ["1", "2"]


class FakeContext:
    def __init__(self, page) -> None:
        self.pages = [page]

    def set_default_timeout(self, ms: float) -> None:
        pass

    def set_default_navigation_timeout(self, ms: float) -> None:
        pass

    async def cookies(self) -> list[dict]:
        return [{"name": "u", "domain": ".avito.ru"}]


def fake_camoufox(monkeypatch, page, closed: list, fail: Exception | None = None):
    import sys
    import types

    class FakeBrowser:
        def __init__(self, **options) -> None:
            pass

        async def __aenter__(self):
            if fail is not None:
                raise fail
            return FakeContext(page)

        async def __aexit__(self, *exc) -> None:
            closed.append(True)

    module = types.ModuleType("camoufox.async_api")
    module.AsyncCamoufox = FakeBrowser
    monkeypatch.setitem(sys.modules, "camoufox.async_api", module)
    monkeypatch.setattr("avito.core.identity.trim", lambda cfg: None)
    monkeypatch.setattr("avito.core.identity.config", lambda cfg: {})
    monkeypatch.setattr("avito.core.identity.profile", lambda cfg: "/tmp/профиль")


def test_browser_is_closed_after_a_good_pass(monkeypatch):
    from avito.core.session import grab

    closed: list = []
    fake_camoufox(monkeypatch, FakePage(), closed)
    assert grab(cfg(), LINK) == CARDS
    assert closed == [True]


def test_browser_is_closed_when_the_pass_fails(monkeypatch):
    from avito.core.session import grab

    closed: list = []
    fake_camoufox(monkeypatch, FakePage(hang={"переход на выдачу"}), closed)
    with pytest.raises(TimeoutError):
        grab(cfg(), LINK)
    assert closed == [True]


def test_browser_is_closed_when_it_fails_to_start(monkeypatch):
    from avito.core.session import grab

    closed: list = []
    fake_camoufox(monkeypatch, FakePage(), closed, fail=RuntimeError("не поднялся"))
    with pytest.raises(RuntimeError, match="не поднялся"):
        grab(cfg(), LINK)
    assert closed == [True]
