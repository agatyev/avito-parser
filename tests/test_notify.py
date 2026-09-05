from __future__ import annotations

import json
import logging

import apprise
import pytest

from avito.models import Listing
from avito.notify import Notifier, body, facts, plural, price, title


def listing(**kwargs) -> Listing:
    fields = {"id": "1", "url": "https://www.avito.ru/items/1", "title": "1-к. квартира"}
    return Listing(**{**fields, **kwargs})


class FakeClient:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    def add(self, target: str) -> bool:
        return True

    def notify(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return self.ok


def test_price_falls_back_to_the_text_from_the_page():
    assert price(listing(price=22500000)) == "22 500 000 ₽"
    assert price(listing(price_text="от 3 000 ₽")) == "от 3 000 ₽"
    assert price(listing()) == "цена не указана"


def test_facts_skip_empty_parts():
    assert facts(listing(price=100, address="Москва")) == "100 ₽ · Москва"


@pytest.mark.parametrize("count,word", [
    (1, "новое объявление"), (2, "новых объявления"), (5, "новых объявлений"),
    (11, "новых объявлений"), (21, "новое объявление"), (102, "новых объявления"),
])
def test_plural_counts_in_russian(count, word):
    assert plural(count) == word


def test_title_names_the_only_listing_and_counts_the_rest():
    assert title([listing()]) == "Avito: 1-к. квартира"
    assert title([listing(), listing()]) == "Avito: 2 новых объявления"


def test_body_escapes_user_text():
    html = body([listing(title="<script>alert(1)</script>", description="a & b")])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "a &amp; b" in html


def test_notifier_without_targets_delivers_to_the_log(caplog):
    caplog.set_level(logging.INFO)
    notifier = Notifier([])
    assert notifier.enabled is False
    assert notifier.send([listing(price=100)]) is True
    assert notifier.send([]) is True
    assert "НОВОЕ" in caplog.text


def test_notifier_skips_unparsable_target():
    notifier = Notifier(["мусор", "mailto://user:pass@example.com"])
    assert notifier.schemes == ["mailto"]


def test_notifier_delivers_html_through_apprise():
    client = FakeClient()
    notifier = Notifier(["mailtos://user:pass@example.com"], client=client)

    assert notifier.enabled is True
    assert notifier.send([listing(price=100)]) is True

    call = client.calls[0]
    assert call["title"] == "Avito: 1-к. квартира"
    assert call["body_format"] == apprise.NotifyFormat.HTML
    assert "<b>1-к. квартира</b>" in call["body"]
    assert "100 ₽" in call["body"]


def test_notifier_reports_failed_delivery(caplog):
    caplog.set_level(logging.INFO)
    notifier = Notifier(["mailto://user:pass@example.com"], client=FakeClient(ok=False))
    assert notifier.send([listing()]) is False
    assert "не удалась" in caplog.text


def test_recipients_are_read_from_url_and_rebuilt(tmp_path):
    store = tmp_path / "mail.json"
    notifier = Notifier(
        ["mailtos://user:pass@smtp.example.com:587//a@example.com?from=user@example.com&mode=starttls"],
        client=FakeClient(), store=store)

    assert notifier.recipients() == ["a@example.com"]

    saved = notifier.set_recipients(["B@Example.com ", "c@example.org"])

    assert saved == ["b@example.com", "c@example.org"]
    assert notifier.recipients() == saved
    assert json.loads(store.read_text()) == saved


def test_bad_recipient_is_rejected(tmp_path):
    notifier = Notifier(["mailtos://user:pass@smtp.example.com"],
                        client=FakeClient(), store=tmp_path / "mail.json")

    with pytest.raises(ValueError):
        notifier.set_recipients(["не почта"])
    with pytest.raises(ValueError):
        notifier.set_recipients([])


def test_stored_recipients_win_over_env_on_start(tmp_path):
    store = tmp_path / "mail.json"
    store.write_text('["new@example.com"]')
    notifier = Notifier(
        ["mailtos://user:pass@smtp.example.com//old@example.com"],
        client=FakeClient(), store=store)

    assert notifier.recipients() == ["new@example.com"]


def test_broken_store_does_not_stop_the_service(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    store = tmp_path / "mail.json"
    store.write_text('["a@examp')

    notifier = Notifier(["mailtos://u:p@smtp.example.com//from@example.com"],
                        client=FakeClient(), store=store)

    assert notifier.recipients() == ["from@example.com"]
    assert "испорчен" in caplog.text


def test_recipients_are_written_atomically(tmp_path):
    store = tmp_path / "mail.json"
    notifier = Notifier(["mailtos://u:p@smtp.example.com//a@example.com"],
                        client=FakeClient(), store=store)

    notifier.set_recipients(["b@example.com"])

    assert json.loads(store.read_text()) == ["b@example.com"]
    assert not list(tmp_path.glob("*.tmp"))
