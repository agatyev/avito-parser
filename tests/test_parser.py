from __future__ import annotations

from datetime import datetime

import pytest

from avito.parser import blocked, parse, parse_date

NOW = datetime(2026, 8, 19, 12, 0)


def test_parses_every_card_of_a_real_page(html):
    listings = parse(html, NOW)
    assert len(listings) == 6
    assert all(li.id and li.url.startswith("https://") and li.title for li in listings)


def test_duplicate_dom_cards_are_returned_once_in_the_original_order():
    html = """
        <div data-marker="item" data-item-id="2">
          <a data-marker="item-title" href="/2">Первый вариант</a>
        </div>
        <div data-marker="item" data-item-id="1">
          <a data-marker="item-title" href="/1">Другой лот</a>
        </div>
        <div data-marker="item" data-item-id="2">
          <a data-marker="item-title" href="/2-copy">Дубль</a>
        </div>
    """

    listings = parse(html, NOW)

    assert [listing.id for listing in listings] == ["2", "1"]
    assert listings[0].title == "Первый вариант"


def test_reads_fields_of_a_card(html):
    listing = next(li for li in parse(html, NOW) if li.id == "4724347290")
    assert listing.price == 22500000
    assert listing.price_text == "22 500 000 ₽"
    assert "Шелепихинская наб." in listing.address
    assert "м. Шелепиха" in listing.address
    assert listing.published_at == datetime(2026, 8, 16, 12, 0)
    assert listing.description


def test_address_falls_back_to_the_current_item_location_marker():
    page = """
      <div data-marker="item" data-item-id="1">
        <a data-marker="item-title" href="/1">Лот</a>
        <div data-marker="item-location">Санкт-Петербург</div>
        <p data-marker="item-date">5 минут назад</p>
      </div>
    """

    assert parse(page, NOW)[0].address == "Санкт-Петербург"


@pytest.mark.parametrize("text,expected", [
    ("5 минут назад", datetime(2026, 8, 19, 11, 55)),
    ("2 часа назад", datetime(2026, 8, 19, 10, 0)),
    ("Сегодня, 14:30", datetime(2026, 8, 19, 14, 30)),
    ("вчера в 21:03", datetime(2026, 8, 18, 21, 3)),
    ("19 августа 10:22", datetime(2026, 8, 19, 10, 22)),
    ("", None),
    ("совсем не дата", None),
])
def test_parse_date_understands_avito_wording(text, expected):
    assert parse_date(text, NOW) == expected


def test_blocked_recognises_case_status_and_captcha_without_rejecting_results(html):
    assert blocked(html) is False
    assert blocked("<html>дОсТуП ОгРаНиЧеН: ПРОБЛЕМА С ip</html>") is True
    assert blocked("<html>" + "x" * 21000 + " CaPtChA</html>") is True
    assert blocked("<html><iframe src='/captcha/check'></iframe></html>") is True
    assert blocked('<div data-marker="item">Лот</div>', status=503) is False
    assert blocked('<div data-marker="item">Лот</div>Подтвердите, что вы не робот') is True
    assert blocked("<html>всё хорошо</html>") is False
