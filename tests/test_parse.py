from pages import listing_page

from avito.parse import blocked, empty_results, item_ids, parse


def test_real_page_is_parsed_whole(real_page):
    listings = parse(real_page)
    assert len(listings) == len(item_ids(real_page))
    assert all(item.id and item.title and item.url.startswith("https://") for item in listings)


def test_card_fields(real_page):
    first = parse(real_page)[0]
    assert first.price is None or first.price > 0
    assert first.date_text


def test_duplicates_collapse():
    listings = parse(listing_page("1", "1", "2"))
    assert [item.id for item in listings] == ["1", "2"]


def test_pages_glued_together_keep_order():
    html = listing_page("3", "2") + "\n" + listing_page("2", "1")
    assert [item.id for item in parse(html)] == ["3", "2", "1"]


def test_blocked_by_status_and_markers():
    assert blocked("<html><body>всё хорошо</body></html>", 429)
    assert blocked("<html><title>Доступ ограничен</title><body>Проблема с IP</body></html>")
    assert blocked('<html><body><div data-sitekey="x"></div></body></html>')
    assert not blocked(listing_page("1"), 200)


def test_marker_without_cards_is_not_a_block():
    assert not blocked(listing_page("1") + "слишком много запросов", 200)


def test_empty_results():
    assert empty_results("<html><body>Ничего не найдено</body></html>")
    assert not empty_results(listing_page("1"))
