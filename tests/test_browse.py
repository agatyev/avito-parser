import asyncio
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import pytest
from pages import listing_page

from avito.browse import HOME, _walk, check_search, page_url, title_from_url
from avito.parse import parse


def test_sorting_by_date_is_forced():
    assert page_url("https://www.avito.ru/moskva/kvartiry", 1).endswith("?s=104")


def test_page_number_replaces_old_paging():
    assert page_url("https://www.avito.ru/moskva/kvartiry?p=7&s=1", 2) == \
        "https://www.avito.ru/moskva/kvartiry?s=104&p=2"


def test_filters_are_kept():
    built = page_url("https://www.avito.ru/moskva/kvartiry?pmax=1000&q=студия", 1)
    assert "pmax=1000" in built and "q=%D1%81%D1%82%D1%83%D0%B4%D0%B8%D1%8F" in built


@pytest.mark.parametrize("url", [
    "https://www.ozon.ru/all",
    "ftp://www.avito.ru/all",
    "https://www.avito.ru/items/1234567890",
    "https://www.avito.ru/user/abc/profile",
    "https://www.avito.ru/moskva/kvartiry/prodam-ASgBAgICAUSSA8YQ_kvartira_37_m_1234567890",
    "https://user:pass@www.avito.ru/all",
    "https://www.avito.ru/moskva/kvartiry/on-map",
])
def test_bad_links_are_refused(url):
    with pytest.raises(ValueError):
        check_search(url)


@pytest.mark.parametrize("url", [
    "https://www.avito.ru/moskva/kvartiry/prodam?pmax=1000",
    "https://avito.ru/all?q=greenwax",
    "https://www.avito.ru/moskva/doma_dachi_kottedzhi/prodam-ASgBAgICAUSGFMjN",
])
def test_search_links_pass(url):
    assert check_search(url).hostname.endswith("avito.ru")


def test_name_from_url():
    assert title_from_url("https://www.avito.ru/all?q=красители greenwax") == "«красители greenwax»"
    assert title_from_url(
        "https://www.avito.ru/moskva/kvartiry/prodam") == "moskva, kvartiry, prodam"


class FakeMouse:
    async def wheel(self, dx, dy):
        return None


class FakePage:
    def __init__(self, pages):
        self.pages = pages
        self.url = "https://ya.ru/"
        self.mouse = FakeMouse()
        self.asked = []

    async def goto(self, url, referer=None, wait_until=None):
        self.url = url
        if url != HOME:
            self.asked.append(url)
        return SimpleNamespace(status=200)

    async def content(self):
        number = int(dict(parse_qsl(urlparse(self.url).query)).get("p", 1))
        return self.pages[number - 1] if number <= len(self.pages) else ""

    async def wait_for_timeout(self, ms):
        return None

    async def wait_for_selector(self, selector, timeout=None):
        return None


def walk(pages, known=(), max_pages=3):
    page = FakePage(pages)
    html = asyncio.run(_walk(page, "https://www.avito.ru/all?q=дом", set(known),
                             max_pages, time.monotonic() + 60))
    return page.asked, html


def test_walk_stops_on_page_with_known_card():
    asked, html = walk([listing_page("3", "2"), listing_page("1")], known=["2"])
    assert len(asked) == 1
    assert [item.id for item in parse(html)] == ["3", "2"]


def test_walk_goes_deeper_while_everything_is_new():
    asked, html = walk([listing_page("5", "4"), listing_page("3", "2"), listing_page("1")])
    assert len(asked) == 3
    assert len(parse(html)) == 5


def test_walk_stops_when_avito_repeats_the_same_page():
    same = listing_page("2", "1")
    asked, html = walk([same, same, same])
    assert len(asked) == 2
    assert len(parse(html)) == 2


def test_walk_stops_on_empty_page():
    asked, html = walk([listing_page("1"), "<html><body>Ничего не найдено</body></html>"])
    assert len(asked) == 2
    assert len(parse(html)) == 1


def test_walk_respects_the_page_limit():
    pages = [listing_page(str(number)) for number in range(9, 0, -1)]
    asked, _ = walk(pages, max_pages=2)
    assert len(asked) == 2
