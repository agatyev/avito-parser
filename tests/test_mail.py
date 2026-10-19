from avito.config import Mail
from avito.mail import Mailer, as_html, as_text, subject
from avito.parse import Listing

ITEMS = [
    Listing(id="1", url="https://www.avito.ru/items/1", title="Дом 100 м²",
            price=5000000, price_text="5 000 000 ₽", address="Москва",
            date_text="5 минут назад", description="тёплый дом у реки " * 20),
    Listing(id="2", url="https://www.avito.ru/items/2", title="Дача",
            price=None, price_text="", address="", date_text="вчера"),
]


def settings(**extra) -> Mail:
    return Mail(host="smtp.example.com", port=587, user="bot@example.com",
                password="secret", sender="bot@example.com",
                to=["owner@example.com"], **extra)


def test_subject_counts_in_russian():
    assert subject("дома", ITEMS[:1]) == "Avito: Дом 100 м²"
    assert subject("дома", ITEMS) == "Avito, дома: 2 новых объявления"
    assert subject("дома", ITEMS * 3).endswith("6 новых объявлений")


def test_body_has_links_prices_and_cut_description():
    html = as_html(ITEMS)
    assert 'href="https://www.avito.ru/items/1"' in html
    assert "5 000 000 ₽" in html and "цена не указана" in html
    assert "…" in html
    assert as_text(ITEMS).count("https://www.avito.ru/items/") == 2


def test_send_goes_only_to_the_given_addresses(monkeypatch):
    mailer = Mailer(settings())
    posted = []
    monkeypatch.setattr(mailer, "_deliver", posted.append)
    assert mailer.send("дома", ITEMS, ["one@example.com", "two@example.com"]) is True
    assert posted[0]["To"] == "one@example.com, two@example.com"
    assert posted[0]["From"] == "bot@example.com"
    assert posted[0].get_content_type() == "multipart/alternative"


def test_failed_delivery_is_reported_not_raised(monkeypatch):
    mailer = Mailer(settings())

    def explode(_message):
        raise OSError("сеть пропала")

    monkeypatch.setattr(mailer, "_deliver", explode)
    assert mailer.send("дома", ITEMS, ["owner@example.com"]) is False


def test_nothing_is_sent_without_settings_or_addresses():
    assert Mailer(Mail()).ready is False
    assert Mailer(Mail()).send("дома", ITEMS, ["owner@example.com"]) is False
    assert Mailer(settings()).send("дома", ITEMS, []) is False
