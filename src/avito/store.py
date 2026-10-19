from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .parse import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    url      TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    last_at  TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    search_id   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    price       INTEGER,
    price_text  TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    date_text   TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    sent        INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    PRIMARY KEY (search_id, item_id),
    FOREIGN KEY (search_id) REFERENCES searches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS seen_unsent ON seen(search_id, sent);
CREATE INDEX IF NOT EXISTS seen_fresh ON seen(first_seen DESC);
CREATE TABLE IF NOT EXISTS recipients (
    email    TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    email     TEXT NOT NULL,
    search_id TEXT NOT NULL,
    PRIMARY KEY (email, search_id),
    FOREIGN KEY (email) REFERENCES recipients(email) ON DELETE CASCADE,
    FOREIGN KEY (search_id) REFERENCES searches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS subscriptions_search ON subscriptions(search_id);
"""


@dataclass(slots=True)
class Search:
    id: str
    name: str
    url: str
    enabled: bool = True
    last_at: str = ""


@dataclass(slots=True)
class Recipient:
    email: str
    searches: list[str]


def _search(row: sqlite3.Row) -> Search:
    return Search(id=row["id"], name=row["name"], url=row["url"],
                  enabled=bool(row["enabled"]), last_at=row["last_at"])


def _listing(row: sqlite3.Row) -> Listing:
    return Listing(id=row["item_id"], url=row["url"], title=row["title"], price=row["price"],
                   price_text=row["price_text"], address=row["address"],
                   date_text=row["date_text"], description=row["description"])


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def searches(self) -> list[Search]:
        with self.connect() as conn:
            return [_search(row) for row in
                    conn.execute("SELECT * FROM searches ORDER BY added_at")]

    def active(self) -> list[Search]:
        return [search for search in self.searches() if search.enabled]

    def add(self, name: str, url: str) -> Search:
        search = Search(id=uuid.uuid4().hex[:8], name=name, url=url)
        with self.connect() as conn:
            conn.execute("INSERT INTO searches (id, name, url, added_at) VALUES (?,?,?,?)",
                         (search.id, search.name, search.url, _now()))
            conn.execute("""INSERT INTO subscriptions (email, search_id)
                            SELECT email, ? FROM recipients""", (search.id,))
        return search

    def toggle(self, search_id: str, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE searches SET enabled = ? WHERE id = ?",
                         (int(enabled), search_id))

    def delete(self, search_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM seen WHERE search_id = ?", (search_id,))
            conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))

    def visited(self, search_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE searches SET last_at = ? WHERE id = ?", (_now(), search_id))

    def known(self, search_id: str) -> set[str]:
        with self.connect() as conn:
            return {row[0] for row in
                    conn.execute("SELECT item_id FROM seen WHERE search_id = ?", (search_id,))}

    def remember(self, search_id: str, listings: Sequence[Listing]) -> list[Listing]:
        added = []
        first_seen = _now()
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM searches WHERE id = ?", (search_id,)).fetchone():
                return []
            for listing in listings:
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO seen (search_id, item_id, title, url, price,
                           price_text, address, date_text, description, first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (search_id, listing.id, listing.title, listing.url, listing.price,
                     listing.price_text, listing.address, listing.date_text,
                     listing.description, first_seen)).rowcount
                if inserted:
                    added.append(listing)
        return added

    def unsent(self, search_id: str) -> list[Listing]:
        with self.connect() as conn:
            return [_listing(row) for row in conn.execute(
                "SELECT * FROM seen WHERE search_id = ? AND sent = 0 ORDER BY rowid",
                (search_id,))]

    def mark_sent(self, search_id: str, item_ids: Sequence[str]) -> int:
        if not item_ids:
            return 0
        with self.connect() as conn:
            marks = ",".join("?" * len(item_ids))
            return conn.execute(
                f"UPDATE seen SET sent = 1 WHERE search_id = ? AND item_id IN ({marks})",
                (search_id, *item_ids)).rowcount

    def waiting(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM seen WHERE sent = 0").fetchone()[0]

    def recipients(self) -> list[Recipient]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT recipients.email AS email,
                          COALESCE(GROUP_CONCAT(subscriptions.search_id), '') AS searches
                   FROM recipients
                   LEFT JOIN subscriptions ON subscriptions.email = recipients.email
                   GROUP BY recipients.email
                   ORDER BY recipients.added_at, recipients.email""").fetchall()
        return [Recipient(email=row["email"],
                          searches=[part for part in row["searches"].split(",") if part])
                for row in rows]

    def subscribers(self, search_id: str) -> list[str]:
        with self.connect() as conn:
            return [row[0] for row in conn.execute(
                "SELECT email FROM subscriptions WHERE search_id = ? ORDER BY email",
                (search_id,))]

    def add_recipient(self, email: str) -> None:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM recipients WHERE email = ?", (email,)).fetchone():
                raise ValueError("такой адрес уже есть")
            _add_recipient(conn, email)

    def remove_recipient(self, email: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM recipients WHERE email = ?", (email,))

    def subscribe(self, email: str, search_id: str, on: bool) -> None:
        with self.connect() as conn:
            if not on:
                conn.execute("DELETE FROM subscriptions WHERE email = ? AND search_id = ?",
                             (email, search_id))
                return
            if not conn.execute("SELECT 1 FROM recipients WHERE email = ?", (email,)).fetchone():
                raise ValueError("такого адреса нет в рассылке")
            if not conn.execute("SELECT 1 FROM searches WHERE id = ?", (search_id,)).fetchone():
                raise ValueError("такого поиска нет")
            conn.execute("INSERT OR IGNORE INTO subscriptions (email, search_id) VALUES (?,?)",
                         (email, search_id))

    def seed_recipients(self, emails: Sequence[str]) -> None:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM recipients").fetchone():
                return
            for email in emails:
                _add_recipient(conn, email)

    def recent(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT seen.*, COALESCE(searches.name, '') AS search_name FROM seen
                   LEFT JOIN searches ON searches.id = seen.search_id
                   ORDER BY first_seen DESC, seen.rowid DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]


def _add_recipient(conn: sqlite3.Connection, email: str) -> None:
    conn.execute("INSERT OR IGNORE INTO recipients (email, added_at) VALUES (?,?)",
                 (email, _now()))
    conn.execute("""INSERT OR IGNORE INTO subscriptions (email, search_id)
                    SELECT ?, id FROM searches""", (email,))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
