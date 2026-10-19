import pytest
from pages import listing_page

from avito import service as service_module
from avito.browse import Blocked
from avito.service import FIRST_RUN_SENDS, Service
from avito.store import Store


class FakeMailer:
    def __init__(self) -> None:
        self.ready = True
        self.works = True
        self.sent: list[list[str]] = []
        self.letters: list[tuple[str, list[str]]] = []

    def send(self, name, listings, to):
        if not self.works:
            return False
        self.sent.append([item.id for item in listings])
        self.letters.append((name, list(to)))
        return True


class Harness:
    def __init__(self, cfg, monkeypatch) -> None:
        self.store = Store(cfg.db)
        self.mailer = FakeMailer()
        self.service = Service(cfg, self.store, self.mailer)
        self.html = ""
        self.error: Exception | None = None
        monkeypatch.setattr(service_module.browse, "fetch", self.fetch)
        self.store.add_recipient("to@example.com")
        self.search = self.store.add("дома", "https://www.avito.ru/all/doma")

    def fetch(self, profile, url, known=(), max_pages=3):
        if self.error is not None:
            raise self.error
        return self.html

    def shows(self, *item_ids):
        self.html = listing_page(*item_ids)

    def hour(self):
        self.service.cycle()

    @property
    def sent(self):
        return self.mailer.sent

    @property
    def known(self):
        return self.store.known(self.search.id)


@pytest.fixture
def avito(cfg, monkeypatch) -> Harness:
    return Harness(cfg, monkeypatch)


def test_first_cycle_sends_only_newest_and_remembers_rest(avito):
    avito.shows(*(str(number) for number in range(10, 0, -1)))
    avito.hour()
    assert avito.sent == [["10", "9", "8", "7", "6"]]
    assert len(avito.known) == 10
    assert avito.store.waiting() == 0


def test_next_cycles_send_only_what_appeared(avito):
    avito.shows("1")
    avito.hour()
    avito.shows("3", "2", "1")
    avito.hour()
    avito.hour()
    assert avito.sent == [["1"], ["3", "2"]]


def test_unsent_listing_survives_failed_mail_and_disappearance(avito):
    avito.shows("1")
    avito.hour()
    avito.mailer.works = False
    avito.shows("2", "1")
    avito.hour()
    assert avito.store.waiting() == 1
    avito.mailer.works = True
    avito.shows("3")
    avito.hour()
    assert avito.sent[-1] == ["2", "3"]
    assert avito.store.waiting() == 0


def test_broken_fetch_still_delivers_the_queue(avito):
    avito.shows("1")
    avito.hour()
    avito.mailer.works = False
    avito.shows("2", "1")
    avito.hour()
    avito.mailer.works = True
    avito.error = Blocked("антибот Avito: HTTP 429")
    avito.hour()
    assert avito.sent[-1] == ["2"]
    assert "антибот" in avito.service.state["error"]


def test_nothing_new_means_no_letter(avito):
    avito.shows("1")
    avito.hour()
    before = len(avito.sent)
    avito.hour()
    assert len(avito.sent) == before


def test_half_broken_layout_is_an_error_not_an_empty_pass(avito):
    avito.shows("1")
    avito.hour()
    avito.html = '<div data-marker="item" data-item-id="9"></div>' + listing_page("8", "1")
    avito.hour()
    assert "разобрано" in avito.service.state["error"]
    assert "9" not in avito.known


def test_empty_results_is_quiet(avito):
    avito.html = "<html><body>Ничего не найдено</body></html>"
    avito.hour()
    assert avito.sent == []
    assert avito.service.state["error"] == ""
    assert avito.store.searches()[0].last_at


def test_paused_search_is_skipped(avito):
    avito.shows("1")
    avito.store.toggle(avito.search.id, False)
    avito.hour()
    assert avito.sent == []


def test_letters_go_only_to_subscribers(avito):
    other = avito.store.add("дачи", "https://www.avito.ru/all/dachi")
    avito.store.add_recipient("work@example.com")
    avito.store.subscribe("work@example.com", other.id, False)
    avito.shows("1")
    avito.hour()
    assert avito.mailer.letters == [("дома", ["to@example.com", "work@example.com"]),
                                    ("дачи", ["to@example.com"])]


def test_nobody_subscribed_keeps_the_queue(avito):
    avito.store.remove_recipient("to@example.com")
    avito.shows("1")
    avito.hour()
    assert avito.sent == []
    assert avito.store.waiting() == 1
    avito.store.add_recipient("to@example.com")
    avito.hour()
    assert avito.sent == [["1"]]
    assert avito.store.waiting() == 0


def test_first_run_constant_is_small():
    assert 1 <= FIRST_RUN_SENDS <= 10
