from __future__ import annotations

import pytest

from avito import url

SEARCH = "https://www.avito.ru/moskva/kvartiry/prodam/vtorichka-ASgB?f=xx&p=4&s=1"


def test_by_date_forces_fresh_sort_and_drops_page():
    result = url.by_date(SEARCH)
    assert "s=104" in result
    assert "p=4" not in result
    assert "f=xx" in result


def test_page_keeps_fresh_sort_and_adds_only_later_page_number():
    assert "p=" not in url.page(SEARCH, 1)
    assert "p=2" in url.page(SEARCH, 2)
    assert "s=104" in url.page(SEARCH, 2)
    with pytest.raises(ValueError, match="положительным"):
        url.page(SEARCH, 0)


def test_name_reads_city_category_deal_and_query():
    assert url.name(SEARCH) == "moskva, квартиры, продажа"
    query = url.name("https://www.avito.ru/moskva?q=садовый+инвентарь")
    assert query == "moskva, «садовый инвентарь»"


def test_phrase_prefers_query_then_category():
    assert url.phrase("https://www.avito.ru/moskva/kvartiry?q=студия у метро") == "студия у метро"
    assert url.phrase(SEARCH) == "квартиры"
    assert url.phrase("https://www.avito.ru/moskva") == ""


@pytest.mark.parametrize("link", [
    "https://avito.ru.evil.com/moskva",
    "ftp://www.avito.ru/moskva",
    "http://user:secret@www.avito.ru/moskva",
    "https://www.avito.ru/moskva/kvartiry/prodam/on-map",
    "",
])
def test_check_refuses_wrong_links(link):
    with pytest.raises(ValueError):
        url.check(link)


def test_check_allows_subdomains():
    assert url.check("https://m.avito.ru/moskva").hostname == "m.avito.ru"


@pytest.mark.parametrize("link", [
    "https://www.avito.ru/items/1234567890",
    "https://www.avito.ru/moskva/kvartiry/lot_1234567890",
    "https://www.avito.ru/user/abc/profile",
    "https://www.avito.ru/profile",
    "https://www.avito.ru/brands/example",
])
def test_check_search_refuses_item_and_profile_pages(link):
    with pytest.raises(ValueError, match="не ссылка на поиск"):
        url.check_search(link)


def test_check_search_allows_query_and_category_results():
    assert url.check_search("https://www.avito.ru/all?q=стол").path == "/all"
    assert url.check_search(SEARCH).path.startswith("/moskva/kvartiry")
