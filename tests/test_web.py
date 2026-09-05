from __future__ import annotations

from datetime import datetime

import pytest

from avito.control.web import create_app
from avito.models import Sub

AVITO = "https://www.avito.ru/moskva/kvartiry/prodam?f=xx"


@pytest.fixture
def client(watcher):
    return create_app(watcher).test_client()


def test_page_opens(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"<title>" in response.data
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_state_tells_what_the_service_is_doing(client, watcher):
    watcher.db.save(Sub(id="", name="Квартиры", url=AVITO))
    data = client.get("/api/state").get_json()

    assert data["status"]["running"] is False
    assert data["notify"] == {"enabled": True, "targets": ["fake"],
                              "emails": ["fake@example.com"]}
    assert data["searches"][0]["name"] == "Квартиры"
    assert data["searches"][0]["enabled"] is True
    assert "queue" in data


def test_save_names_the_subscription_by_its_link(client, watcher):
    assert client.post("/api/save", json={"url": AVITO}).status_code == 200
    saved = watcher.db.subs()[0]
    assert saved.name == "moskva, квартиры, продажа"
    assert saved.enabled is True


def test_save_updates_instead_of_duplicating(client, watcher):
    client.post("/api/save", json={"url": AVITO})
    sub = watcher.db.subs()[0]

    client.post("/api/save", json={"id": sub.id, "url": AVITO, "name": sub.name,
                                   "enabled": False})

    assert len(watcher.db.subs()) == 1
    assert watcher.db.subs()[0].enabled is False
    assert watcher.db.subs()[0].period_minutes == watcher.cfg.watch.period_minutes


def test_save_always_uses_the_default_period(client, watcher):
    client.post("/api/save", json={"url": AVITO, "period_minutes": 720})
    assert watcher.db.subs()[0].period_minutes == watcher.cfg.watch.period_minutes


def test_save_refuses_a_link_that_is_not_a_search(client, watcher):
    response = client.post("/api/save", json={"url": "https://example.com/x"})
    assert response.status_code == 400
    assert "Avito" in response.get_json()["error"]
    assert watcher.db.subs() == []


def test_save_refuses_an_unknown_or_unsafe_id(client, watcher):
    for sub_id in ("../escape", "deadbeef"):
        response = client.post("/api/save", json={"id": sub_id, "url": AVITO})
        assert response.status_code == 400
    assert watcher.db.subs() == []


def test_delete_removes_subscription(client, watcher):
    client.post("/api/save", json={"url": AVITO})
    sub_id = watcher.db.subs()[0].id

    assert client.post("/api/delete", json={"id": sub_id}).status_code == 200
    assert watcher.db.subs() == []


def test_run_asks_for_a_pass_right_now(client, watcher):
    client.post("/api/save", json={"url": AVITO})
    watcher.sync()
    planned = watcher.sched.get_job(watcher.job_id(watcher.db.subs()[0].id)).next_run_time

    assert client.post("/api/run", json={}).status_code == 200
    assert watcher.sched.get_job(watcher.job_id(watcher.db.subs()[0].id)).next_run_time < planned


def test_page_is_closed_for_strangers(client):
    stranger = client.get("/api/state", headers={"Host": "avito.example.com"})
    forged = client.post("/api/run", json={}, headers={"Origin": "https://evil.com"})
    assert (stranger.status_code, forged.status_code) == (403, 403)


def test_post_requires_json(client):
    assert client.post("/api/run", data="run=1").status_code == 415


def test_unknown_path_answers_with_json(client):
    response = client.get("/api/whatever")
    assert response.status_code == 404
    assert "error" in response.get_json()


def test_health_goes_red_when_every_subscription_is_failing(client, watcher, monkeypatch):
    from avito import cli

    watcher.db.save(Sub(id="", name="Квартиры", url=AVITO))
    sub_id = watcher.db.subs()[0].id
    monkeypatch.setattr(cli.urllib.request, "urlopen",
                        lambda *a, **kw: _answer(client))

    assert cli.cmd_health(watcher.cfg) == 0

    watcher.db.set_errors(sub_id, cli.DEAD_ERRORS)
    assert cli.cmd_health(watcher.cfg) == 1


def test_health_goes_red_when_delivery_failed(client, watcher, monkeypatch):
    from conftest import FakeNotifier

    from avito import cli
    from avito.models import Listing

    sub = watcher.db.save(Sub(id="", name="Квартиры", url=AVITO))
    watcher.db.plan(sub.id, last_at=1)
    watcher.notifier = FakeNotifier(ok=False)
    watcher._collect(watcher.db.sub(sub.id), [Listing(
        id="1", url=f"{AVITO}/1", title="Лот", published_at=datetime.now())])
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **kw: _answer(client))

    assert cli.cmd_health(watcher.cfg) == 1


class _answer:
    def __init__(self, client) -> None:
        self.data = client.get("/api/state").data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def test_recipients_are_added_and_removed(client, watcher):
    added = client.post("/api/recipients", json={"action": "add", "email": "b@example.com"})
    assert added.status_code == 200

    removed = client.post("/api/recipients", json={"action": "remove",
                                                   "email": "b@example.com"})
    assert removed.status_code == 200

    assert client.post("/api/recipients",
                       json={"action": "remove", "email": "нет@example.com"}).status_code == 400
    assert client.post("/api/recipients", json={"action": "жечь"}).status_code == 400
