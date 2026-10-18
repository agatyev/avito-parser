import pytest
from pages import listing_page

from avito import service as service_module
from avito.service import Service
from avito.store import Store
from avito.web import create_app


class FakeMailer:
    ready = True

    def __init__(self) -> None:
        self.to = ["owner@example.com"]

    def recipients(self):
        return list(self.to)

    def set_recipients(self, addresses):
        self.to = sorted({value.lower() for value in addresses})
        return list(self.to)

    def send(self, name, listings):
        return True


@pytest.fixture
def client(cfg, monkeypatch):
    store = Store(cfg.db)
    service = Service(cfg, store, FakeMailer())
    monkeypatch.setattr(service_module.browse, "fetch",
                        lambda *args, **kwargs: listing_page("1"))
    app = create_app(service)
    app.config["TESTING"] = True
    return app.test_client(), service


def test_page_opens(client):
    page = client[0]
    assert b"<title>" in page.get("/").data


def test_add_list_toggle_delete(client):
    page = client[0]
    added = page.post("/api/add", json={"url": "https://www.avito.ru/all?q=дом"})
    assert added.status_code == 200
    state = page.get("/api/state").get_json()
    assert state["searches"][0]["name"] == "«дом»"
    search_id = state["searches"][0]["id"]
    page.post("/api/toggle", json={"id": search_id, "enabled": False})
    assert page.get("/api/state").get_json()["searches"][0]["enabled"] is False
    page.post("/api/delete", json={"id": search_id})
    assert page.get("/api/state").get_json()["searches"] == []


def test_bad_link_is_explained(client):
    page = client[0]
    answer = page.post("/api/add", json={"url": "https://www.ozon.ru/all"})
    assert answer.status_code == 400
    assert "Avito" in answer.get_json()["error"]


def test_run_wakes_the_service(client):
    page, service = client
    service.wake.clear()
    page.post("/api/run", json={})
    assert service.wake.is_set()


def test_recipients_are_edited(client):
    page = client[0]
    added = page.post("/api/recipients", json={"action": "add", "email": "second@example.com"})
    assert added.get_json()["to"] == ["owner@example.com", "second@example.com"]
    removed = page.post("/api/recipients",
                        json={"action": "remove", "email": "owner@example.com"})
    assert removed.get_json()["to"] == ["second@example.com"]
    assert page.post("/api/recipients",
                     json={"action": "remove", "email": "second@example.com"}).status_code == 400


def test_outsiders_are_refused(client):
    page = client[0]
    assert page.get("/api/state", headers={"Host": "10.0.0.5:8080"}).status_code == 403
    assert page.post("/api/run", json={},
                     headers={"Origin": "https://evil.example"}).status_code == 403


def test_found_listings_are_shown(client):
    page, service = client
    page.post("/api/add", json={"url": "https://www.avito.ru/all?q=дом"})
    service.cycle()
    found = page.get("/api/state").get_json()["found"]
    assert found and found[0]["title"].startswith("Квартира")
