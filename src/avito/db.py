from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .models import Listing, Sub

KEEP_DAYS = 90
SUB_ID = re.compile(r"^[0-9a-f]{8}$")

SUBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subs (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    url            TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    period_minutes INTEGER NOT NULL DEFAULT 60,
    last_at        REAL NOT NULL DEFAULT 0,
    next_at        REAL NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0
)
"""

SEEN_SCHEMA = """
CREATE TABLE seen (
    sub_id     TEXT NOT NULL,
    id         TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    price      INTEGER,
    address    TEXT NOT NULL DEFAULT '',
    sent       INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (sub_id, id),
    FOREIGN KEY (sub_id) REFERENCES subs(id) ON DELETE CASCADE
)
"""

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    sub_id      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (sub_id, listing_id),
    FOREIGN KEY (sub_id, listing_id) REFERENCES seen(sub_id, id) ON DELETE CASCADE
)
"""

DELIVERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_targets (
    sub_id       TEXT NOT NULL,
    listing_id   TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    delivered_at REAL,
    lease_token  TEXT NOT NULL DEFAULT '',
    lease_until  REAL NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (sub_id, listing_id, target_key),
    FOREIGN KEY (sub_id, listing_id)
        REFERENCES outbox(sub_id, listing_id) ON DELETE CASCADE
)
"""


@dataclass(slots=True)
class DeliveryClaim:
    token: str
    sub_id: str
    target_key: str
    listings: list[Listing]
    attempts: int


def _sub(row: sqlite3.Row) -> Sub:
    return Sub(id=row["id"], name=row["name"], url=row["url"],
               enabled=bool(row["enabled"]), period_minutes=row["period_minutes"],
               last_at=row["last_at"], next_at=row["next_at"], errors=row["errors"])


def _dump(listing: Listing) -> str:
    payload = {
        "id": listing.id,
        "url": listing.url,
        "title": listing.title,
        "price": listing.price,
        "price_text": listing.price_text,
        "address": listing.address,
        "date_text": listing.date_text,
        "published_at": listing.published_at.isoformat() if listing.published_at else None,
        "description": listing.description,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _load(payload: str) -> Listing:
    data = json.loads(payload)
    published = data.get("published_at")
    data["published_at"] = datetime.fromisoformat(published) if published else None
    return Listing(**data)


class Db:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        with self.connect(immediate=True) as conn:
            conn.execute(SUBS_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(subs)")}
            if "errors" not in columns:
                conn.execute("ALTER TABLE subs ADD COLUMN errors INTEGER NOT NULL DEFAULT 0")
            self._migrate_seen(conn)
            conn.execute(OUTBOX_SCHEMA)
            conn.execute(DELIVERIES_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS seen_by_time ON seen(first_seen DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS outbox_pending "
                "ON outbox_targets(target_key, delivered_at, lease_until)")
            conn.execute("PRAGMA user_version = 2")

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_seen(self, conn: sqlite3.Connection) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seen'").fetchone()
        if not exists:
            conn.execute(SEEN_SCHEMA)
            return
        info = conn.execute("PRAGMA table_info(seen)").fetchall()
        primary = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
        if primary == ["sub_id", "id"]:
            return
        legacy = "seen_legacy_v1"
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (legacy,)).fetchone():
            raise RuntimeError("не закончена предыдущая миграция seen")
        conn.execute("DROP INDEX IF EXISTS seen_by_time")
        conn.execute(f"ALTER TABLE seen RENAME TO {legacy}")
        conn.execute(SEEN_SCHEMA)
        columns = {row["name"] for row in info}
        if {"sub_id", "id"}.issubset(columns):
            values = {
                "title": "title" if "title" in columns else "''",
                "url": "url" if "url" in columns else "''",
                "price": "price" if "price" in columns else "NULL",
                "address": "address" if "address" in columns else "''",
                "sent": "sent" if "sent" in columns else "0",
                "first_seen": "first_seen" if "first_seen" in columns else "datetime('now')",
            }
            conn.execute(
                f"""INSERT OR IGNORE INTO seen
                    (sub_id, id, title, url, price, address, sent, first_seen)
                    SELECT sub_id, id, {values['title']}, {values['url']}, {values['price']},
                           {values['address']}, {values['sent']}, {values['first_seen']}
                    FROM {legacy}
                    WHERE EXISTS (SELECT 1 FROM subs WHERE subs.id = {legacy}.sub_id)""")
        conn.execute(f"DROP TABLE {legacy}")

    def subs(self) -> list[Sub]:
        with self.connect() as conn:
            return [_sub(row) for row in conn.execute("SELECT * FROM subs ORDER BY rowid")]

    def sub(self, sub_id: str) -> Sub | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM subs WHERE id = ?", (sub_id,)).fetchone()
        return _sub(row) if row else None

    def save(self, sub: Sub) -> Sub:
        if sub.id and not SUB_ID.fullmatch(sub.id):
            raise ValueError("некорректный id подписки")
        with self.connect(immediate=True) as conn:
            if not sub.id:
                for _ in range(100):
                    candidate = uuid.uuid4().hex[:8]
                    try:
                        conn.execute(
                            "INSERT INTO subs (id, name, url, enabled, period_minutes) "
                            "VALUES (?,?,?,?,?)",
                            (candidate, sub.name, sub.url, int(sub.enabled), sub.period_minutes))
                    except sqlite3.IntegrityError:
                        continue
                    sub.id = candidate
                    break
                else:
                    raise RuntimeError("не удалось создать уникальный id подписки")
            else:
                conn.execute(
                    """INSERT INTO subs (id, name, url, enabled, period_minutes)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name = excluded.name,
                           url = excluded.url, enabled = excluded.enabled,
                           period_minutes = excluded.period_minutes""",
                    (sub.id, sub.name, sub.url, int(sub.enabled), sub.period_minutes))
        return sub

    def delete(self, sub_id: str) -> None:
        with self.connect(immediate=True) as conn:
            conn.execute("DELETE FROM seen WHERE sub_id = ?", (sub_id,))
            conn.execute("DELETE FROM subs WHERE id = ?", (sub_id,))

    def reset_sub(self, sub_id: str) -> bool:
        with self.connect(immediate=True) as conn:
            conn.execute("DELETE FROM seen WHERE sub_id = ?", (sub_id,))
            changed = conn.execute(
                "UPDATE subs SET last_at = 0, next_at = 0, errors = 0 WHERE id = ?",
                (sub_id,)).rowcount
        return bool(changed)

    def plan(self, sub_id: str, next_at: float = 0.0, last_at: float | None = None) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE subs SET next_at = ?, last_at = COALESCE(?, last_at) "
                         "WHERE id = ?", (next_at, last_at, sub_id))

    def error_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id, errors FROM subs WHERE errors > 0").fetchall()
        return {row["id"]: row["errors"] for row in rows}

    def set_errors(self, sub_id: str, count: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE subs SET errors = ? WHERE id = ?", (count, sub_id))

    def unseen(self, listings: Sequence[Listing], sub_id: str | None = None) -> list[Listing]:
        unique = list({listing.id: listing for listing in reversed(listings)}.values())[::-1]
        if not unique:
            return []
        ids = [listing.id for listing in unique]
        known: set[str] = set()
        with self.connect() as conn:
            for start in range(0, len(ids), 900):
                part = ids[start:start + 900]
                marks = ",".join("?" * len(part))
                if sub_id is None:
                    query = f"SELECT id FROM seen WHERE id IN ({marks})"
                    params: Sequence[object] = part
                else:
                    query = f"SELECT id FROM seen WHERE sub_id = ? AND id IN ({marks})"
                    params = [sub_id, *part]
                known.update(row[0] for row in conn.execute(query, params))
        return [listing for listing in unique if listing.id not in known]

    def seen_ids(self, sub_id: str, limit: int | None = None) -> set[str]:
        query = "SELECT id FROM seen WHERE sub_id = ? ORDER BY first_seen DESC, rowid DESC"
        params: list[object] = [sub_id]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit не может быть отрицательным")
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return {row[0] for row in conn.execute(query, params)}

    def mark(self, listing: Listing, sub_id: str, sent: bool) -> None:
        with self.connect(immediate=True) as conn:
            first_seen = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """INSERT OR IGNORE INTO seen
                   (sub_id, id, title, url, price, address, sent, first_seen)
                   SELECT ?,?,?,?,?,?,?,?
                   WHERE EXISTS (SELECT 1 FROM subs WHERE id = ?)""",
                (sub_id, listing.id, listing.title, listing.url, listing.price,
                 listing.address, int(sent), first_seen, sub_id))
            if sent:
                conn.execute("UPDATE seen SET sent = 1 WHERE sub_id = ? AND id = ?",
                             (sub_id, listing.id))
                conn.execute(
                    """UPDATE outbox_targets
                       SET delivered_at = COALESCE(delivered_at, ?), lease_token = '',
                           lease_until = 0, last_error = ''
                       WHERE sub_id = ? AND listing_id = ?""",
                    (time.time(), sub_id, listing.id))

    def enqueue(self, sub_id: str, listings: Sequence[Listing],
                target_keys: Sequence[str]) -> list[Listing]:
        keys = tuple(dict.fromkeys(key for key in target_keys if key))
        if not keys:
            raise ValueError("нужен хотя бы один target доставки")
        unique = list({listing.id: listing for listing in reversed(listings)}.values())[::-1]
        queued: list[Listing] = []
        created_at = time.time()
        first_seen = datetime.now().isoformat(timespec="seconds")
        with self.connect(immediate=True) as conn:
            if not conn.execute("SELECT 1 FROM subs WHERE id = ?", (sub_id,)).fetchone():
                return []
            for listing in unique:
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO seen
                       (sub_id, id, title, url, price, address, sent, first_seen)
                       VALUES (?,?,?,?,?,?,0,?)""",
                    (sub_id, listing.id, listing.title, listing.url, listing.price,
                     listing.address, first_seen)).rowcount
                if not inserted:
                    continue
                conn.execute(
                    "INSERT INTO outbox (sub_id, listing_id, payload, created_at) "
                    "VALUES (?,?,?,?)", (sub_id, listing.id, _dump(listing), created_at))
                conn.executemany(
                    "INSERT INTO outbox_targets (sub_id, listing_id, target_key) "
                    "VALUES (?,?,?)", ((sub_id, listing.id, key) for key in keys))
                queued.append(listing)
        return queued

    def claim(self, sub_id: str, target_key: str, limit: int = 50,
              lease_seconds: float = 300, now: float | None = None) -> DeliveryClaim | None:
        if limit <= 0:
            raise ValueError("limit должен быть положительным")
        if lease_seconds <= 0:
            raise ValueError("lease должен быть положительным")
        current = time.time() if now is None else now
        token = uuid.uuid4().hex
        with self.connect(immediate=True) as conn:
            rows = conn.execute(
                """SELECT target.listing_id, target.attempts, outbox.payload
                   FROM outbox_targets AS target
                   JOIN outbox USING (sub_id, listing_id)
                   WHERE target.sub_id = ? AND target.target_key = ?
                     AND target.delivered_at IS NULL AND target.lease_until <= ?
                   ORDER BY outbox.created_at, outbox.rowid LIMIT ?""",
                (sub_id, target_key, current, limit)).fetchall()
            if not rows:
                return None
            conn.executemany(
                """UPDATE outbox_targets
                   SET lease_token = ?, lease_until = ?, attempts = attempts + 1,
                       last_error = ''
                   WHERE sub_id = ? AND listing_id = ? AND target_key = ?
                     AND delivered_at IS NULL AND lease_until <= ?""",
                ((token, current + lease_seconds, sub_id, row["listing_id"], target_key,
                  current) for row in rows))
        return DeliveryClaim(token=token, sub_id=sub_id, target_key=target_key,
                             listings=[_load(row["payload"]) for row in rows],
                             attempts=max(row["attempts"] for row in rows) + 1)

    def complete(self, token: str, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self.connect(immediate=True) as conn:
            rows = conn.execute(
                """SELECT DISTINCT sub_id, listing_id FROM outbox_targets
                   WHERE lease_token = ? AND delivered_at IS NULL""", (token,)).fetchall()
            if not rows:
                return 0
            conn.execute(
                """UPDATE outbox_targets
                   SET delivered_at = ?, lease_token = '', lease_until = 0, last_error = ''
                   WHERE lease_token = ? AND delivered_at IS NULL""", (current, token))
            finalized = 0
            for row in rows:
                finalized += conn.execute(
                    """UPDATE seen SET sent = 1
                       WHERE sub_id = ? AND id = ? AND sent = 0
                         AND NOT EXISTS (
                             SELECT 1 FROM outbox_targets
                             WHERE sub_id = ? AND listing_id = ? AND delivered_at IS NULL)""",
                    (row["sub_id"], row["listing_id"],
                     row["sub_id"], row["listing_id"])).rowcount
        return finalized

    def mark_delivered(self, token: str, now: float | None = None) -> int:
        return self.complete(token, now)

    def release(self, token: str, error: str = "", retry_after: float = 0,
                now: float | None = None) -> int:
        if retry_after < 0:
            raise ValueError("retry_after не может быть отрицательным")
        current = time.time() if now is None else now
        with self.connect(immediate=True) as conn:
            return conn.execute(
                """UPDATE outbox_targets
                   SET lease_token = '', lease_until = ?, last_error = ?
                   WHERE lease_token = ? AND delivered_at IS NULL""",
                (current + retry_after, error, token)).rowcount

    def pending(self, sub_id: str | None = None, target_key: str | None = None) -> int:
        conditions = ["delivered_at IS NULL"]
        params: list[object] = []
        if sub_id is not None:
            conditions.append("sub_id = ?")
            params.append(sub_id)
        if target_key is not None:
            conditions.append("target_key = ?")
            params.append(target_key)
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM outbox_targets WHERE " + " AND ".join(conditions),
                params).fetchone()[0]

    def pending_targets(self, sub_id: str | None = None) -> set[str]:
        query = "SELECT DISTINCT target_key FROM outbox_targets WHERE delivered_at IS NULL"
        params: tuple[object, ...] = ()
        if sub_id is not None:
            query += " AND sub_id = ?"
            params = (sub_id,)
        with self.connect() as conn:
            return {row[0] for row in conn.execute(query, params)}

    def reconcile_targets(self, active_keys: Sequence[str],
                          sub_id: str | None = None) -> tuple[int, int]:
        """Keep delivery obligations immutable.

        Targets are captured when an outbox row is created.  Reconfiguration
        must neither add recipients to old messages nor erase pending delivery.
        """
        keys = tuple(dict.fromkeys(key for key in active_keys if key))
        if not keys:
            raise ValueError("нужен хотя бы один активный target")
        return 0, 0

    def delivery_status(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS pending,
                          COALESCE(SUM(target.last_error <> ''), 0) AS failed,
                          COALESCE(MIN(outbox.created_at), 0) AS oldest_at
                   FROM outbox_targets AS target
                   JOIN outbox USING (sub_id, listing_id)
                   WHERE target.delivered_at IS NULL""").fetchone()
        return {"pending": row["pending"], "failed": row["failed"],
                "oldest_at": row["oldest_at"]}

    def recent(self, limit: int = 60) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT seen.*, COALESCE(subs.name, '') AS sub_name FROM seen
                   LEFT JOIN subs ON subs.id = seen.sub_id
                   ORDER BY first_seen DESC, seen.rowid DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def prune(self, days: int = KEEP_DAYS) -> int:
        edge = time.time() - timedelta(days=days).total_seconds()
        with self.connect(immediate=True) as conn:
            orphaned = conn.execute(
                "DELETE FROM seen WHERE sub_id NOT IN (SELECT id FROM subs)").rowcount
            conn.execute(
                """DELETE FROM outbox WHERE created_at < ? AND NOT EXISTS (
                       SELECT 1 FROM outbox_targets
                       WHERE outbox_targets.sub_id = outbox.sub_id
                         AND outbox_targets.listing_id = outbox.listing_id
                         AND outbox_targets.delivered_at IS NULL)""", (edge,))
        return orphaned

    def backup(self, destination: Path) -> Path:
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("backup должен отличаться от основной БД")
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as source, sqlite3.connect(target) as saved:
            source.backup(saved)
        return target

    def integrity_check(self) -> bool:
        with self.connect() as conn:
            rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        return rows == ["ok"]
