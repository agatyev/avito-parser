from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from avito.db import Db
from avito.models import Listing, Sub


@pytest.fixture
def db(tmp_path) -> Db:
    return Db(tmp_path / "avito.db")


def listing(item_id: str) -> Listing:
    return Listing(id=item_id, url=f"https://www.avito.ru/items/{item_id}",
                   title=f"Объявление {item_id}", price=100, address="Москва")


def test_save_gives_id_and_updates_without_touching_schedule(db):
    sub = db.save(Sub(id="", name="Квартиры", url="https://www.avito.ru/moskva"))
    assert len(sub.id) == 8
    db.plan(sub.id, next_at=555.0, last_at=111.0)

    db.save(Sub(id=sub.id, name="Комнаты", url=sub.url, enabled=False, period_minutes=180))
    stored = db.sub(sub.id)
    assert (stored.name, stored.enabled, stored.period_minutes) == ("Комнаты", False, 180)
    assert (stored.next_at, stored.last_at) == (555.0, 111.0)


def test_unseen_keeps_only_new_listings(db):
    sub = db.save(Sub(id="", name="a", url="https://www.avito.ru/moskva"))
    batch = [listing("1"), listing("2")]
    assert db.unseen(batch) == batch

    db.mark(batch[0], sub.id, sent=True)
    assert [li.id for li in db.unseen(batch)] == ["2"]
    assert db.unseen([]) == []


def test_recent_shows_subscription_name(db):
    sub = db.save(Sub(id="", name="Квартиры", url="https://www.avito.ru/moskva"))
    db.mark(listing("1"), sub.id, sent=False)
    row = db.recent()[0]
    assert row["sub_name"] == "Квартиры"
    assert row["sent"] == 0
    assert db.count() == 1


def test_delete_takes_findings_along(db):
    sub = db.save(Sub(id="", name="a", url="https://www.avito.ru/moskva"))
    db.mark(listing("1"), sub.id, sent=True)
    db.delete(sub.id)
    assert db.subs() == []
    assert db.count() == 0


def test_prune_keeps_seen_rows_for_active_subscriptions(db):
    sub = db.save(Sub(id="", name="a", url="https://www.avito.ru/moskva"))
    db.mark(listing("fresh"), sub.id, sent=False)
    stale = (datetime.now() - timedelta(days=91)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("INSERT INTO seen (id, sub_id, first_seen) VALUES ('old', ?, ?)",
                     (sub.id, stale))

    assert db.prune() == 0
    assert db.count() == 2
    assert {row["id"] for row in db.recent()} == {"fresh", "old"}


def test_listing_of_a_deleted_subscription_is_not_remembered(db):
    sub = db.save(Sub(id="", name="Квартиры", url="https://www.avito.ru/moskva"))
    db.delete(sub.id)

    db.mark(listing("999"), sub.id, True)

    assert db.count() == 0


def test_reconfiguration_keeps_original_pending_targets(db):
    sub = db.save(Sub(id="", name="a", url="https://www.avito.ru/moskva"))
    db.enqueue(sub.id, [listing("1")], ["smtp-old"])

    assert db.reconcile_targets(["log"]) == (0, 0)

    assert db.pending_targets(sub.id) == {"smtp-old"}
    assert db.pending(sub.id) == 1


def test_orphan_rows_are_swept_on_start(db):
    sub = db.save(Sub(id="", name="Квартиры", url="https://www.avito.ru/moskva"))
    db.mark(listing("999"), sub.id, True)
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE seen SET sub_id = 'сгинувшая'")

    db.prune()

    assert db.count() == 0
