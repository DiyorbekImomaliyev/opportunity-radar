"""SQLite persistence (SPEC §6)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import Item, Kind

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    starts_at     TEXT,
    ends_at       TEXT,
    published_at  TEXT,
    location      TEXT,
    is_online     INTEGER,
    organizer     TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    score         REAL,
    reasons       TEXT,
    about         TEXT,
    duplicate_of  TEXT,
    sent_at       TEXT,
    ai_version    TEXT
);
CREATE INDEX IF NOT EXISTS items_unsent ON items(sent_at) WHERE sent_at IS NULL;
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,
    totals       TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    source       TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    item_count   INTEGER NOT NULL,
    error        TEXT,
    duration_ms  INTEGER NOT NULL,
    PRIMARY KEY (run_id, source)
);
"""


_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS marks (
    uid               TEXT PRIMARY KEY,   -- an item's uid, or a find the owner sent
    source            TEXT NOT NULL,
    kind              TEXT,
    title             TEXT NOT NULL,
    url               TEXT,
    status            TEXT NOT NULL,      -- saved | registered | dismissed | ineligible | done | removed
    deadline          TEXT,               -- deadline or start, when known
    remind_every_days INTEGER,            -- repeat period for saved items
    next_remind_at    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS marks_due ON marks(next_remind_at) WHERE next_remind_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

ACTIVE = ("saved", "registered")  # marks that can still have reminders


@dataclass
class Mark:
    uid: str
    source: str
    kind: str | None
    title: str
    url: str | None
    status: str
    deadline: datetime | None
    remind_every_days: int | None
    next_remind_at: datetime | None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass
class StoredItem:
    item: Item
    uid: str
    first_seen: datetime
    score: float | None
    reasons: list[str]
    about: str | None
    ai_version: str | None
    duplicate_of: str | None
    sent_at: datetime | None


@dataclass
class SourceHealth:
    source: str
    last_ok: bool
    last_error: str | None
    last_success: datetime | None
    consecutive_failures: int
    avg_items_7: float
    went_silent: bool  # returned items before, 0 now


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.db.executescript(_SCHEMA)
        if version == 1:  # v2: remember which AI instructions + profile judged each item
            self.db.execute("ALTER TABLE items ADD COLUMN ai_version TEXT")
        if version < 3:  # v3: the owner's marks and reminders, bot state
            self.db.executescript(_SCHEMA_V3)
        if version < SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()

    # ------------------------------------------------------------ items

    def upsert(self, item: Item, now: datetime) -> bool:
        """Insert or refresh an item. Returns True if it is new."""
        uid = item.uid
        row = self.db.execute("SELECT uid, summary, tags FROM items WHERE uid = ?", (uid,)).fetchone()
        summary, tags = item.summary, item.tags
        if row is not None:  # keep what a source read once from the item's own page (visa, eligibility)
            if row["summary"].startswith("Eligibility: ") and not summary.startswith("Eligibility: "):
                summary = row["summary"]
            tags = [*tags, *(t for t in json.loads(row["tags"]) if t.startswith("visa:") and t not in tags)]
        fields = (
            item.title, item.url, summary, _iso(item.starts_at), _iso(item.ends_at),
            _iso(item.published_at), item.location,
            None if item.is_online is None else int(item.is_online),
            item.organizer, json.dumps(tags),
        )
        if row is None:
            self.db.execute(
                """INSERT INTO items (uid, source, kind, title, url, summary, starts_at, ends_at,
                   published_at, location, is_online, organizer, tags, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, item.source, item.kind.value, *fields, _iso(now), _iso(now)),
            )
            return True
        self.db.execute(
            """UPDATE items SET title=?, url=?, summary=?, starts_at=?, ends_at=?, published_at=?,
               location=?, is_online=?, organizer=?, tags=?, last_seen=? WHERE uid=?""",
            (*fields, _iso(now), uid),
        )
        return False

    def set_score(
        self, uid: str, score: float, reasons: list[str], about: str | None = None, ai_version: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE items SET score=?, reasons=?, about=COALESCE(?, about), "
            "ai_version=COALESCE(?, ai_version) WHERE uid=?",
            (score, json.dumps(reasons), about, ai_version, uid),
        )

    def mark_duplicate(self, uid: str, of_uid: str) -> None:
        self.db.execute("UPDATE items SET duplicate_of=? WHERE uid=?", (of_uid, uid))

    def prune(self, now: datetime, keep_days: int = 60) -> int:
        """Forget unsent items that can no longer matter: over, or not seen for `keep_days`.

        Sent items are kept (they guard against re-sending near-duplicates)."""
        from datetime import timedelta

        cutoff = _iso(now - timedelta(days=keep_days))
        cur = self.db.execute(
            """DELETE FROM items WHERE sent_at IS NULL AND (
                   last_seen < ?
                   OR COALESCE(ends_at, starts_at) < ?
                   OR (starts_at IS NULL AND ends_at IS NULL AND published_at < ?))""",
            (cutoff, cutoff, cutoff),
        )
        self.db.commit()
        return cur.rowcount

    def mark_sent(self, uids: list[str], now: datetime) -> None:
        self.db.executemany("UPDATE items SET sent_at=? WHERE uid=?", [(_iso(now), u) for u in uids])
        self.db.commit()

    def commit(self) -> None:
        self.db.commit()

    def _row_to_stored(self, r: sqlite3.Row) -> StoredItem:
        item = Item(
            source=r["source"], kind=Kind(r["kind"]), title=r["title"], url=r["url"],
            summary=r["summary"], starts_at=_dt(r["starts_at"]), ends_at=_dt(r["ends_at"]),
            published_at=_dt(r["published_at"]), location=r["location"],
            is_online=None if r["is_online"] is None else bool(r["is_online"]),
            organizer=r["organizer"], tags=json.loads(r["tags"] or "[]"),
        )
        return StoredItem(
            item=item, uid=r["uid"], first_seen=_dt(r["first_seen"]), score=r["score"],
            reasons=json.loads(r["reasons"] or "[]"), about=r["about"], ai_version=r["ai_version"],
            duplicate_of=r["duplicate_of"], sent_at=_dt(r["sent_at"]),
        )

    def unsent(self) -> list[StoredItem]:
        rows = self.db.execute(
            "SELECT * FROM items WHERE sent_at IS NULL AND duplicate_of IS NULL"
            " AND uid NOT IN (SELECT uid FROM marks) ORDER BY first_seen"
        ).fetchall()
        return [self._row_to_stored(r) for r in rows]

    def get(self, uid: str) -> StoredItem | None:
        r = self.db.execute("SELECT * FROM items WHERE uid=?", (uid,)).fetchone()
        return self._row_to_stored(r) if r else None

    def active_items(self) -> list[StoredItem]:
        """Every item not already marked as a duplicate."""
        rows = self.db.execute("SELECT * FROM items WHERE duplicate_of IS NULL").fetchall()
        return [self._row_to_stored(r) for r in rows]

    # ------------------------------------------------------------ runs / health

    def start_run(self, now: datetime) -> int:
        cur = self.db.execute("INSERT INTO runs (started_at) VALUES (?)", (_iso(now),))
        self.db.commit()
        return int(cur.lastrowid)

    def record_source(self, run_id: int, source: str, ok: bool, count: int, error: str | None, ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO source_runs VALUES (?,?,?,?,?,?)",
            (run_id, source, int(ok), count, error, ms),
        )
        self.db.commit()

    def finish_run(self, run_id: int, now: datetime, status: str, totals: dict) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, totals=? WHERE id=?",
            (_iso(now), status, json.dumps(totals), run_id),
        )
        self.db.commit()

    def delivered_on(self, day, tz=None, scheduled_only: bool = False) -> bool:
        """True if a run delivered a digest on this local calendar day (owner's timezone).
        With scheduled_only, manual `radar run`s don't count (they never cancel the daily digest)."""
        from .textutil import local_tz

        tz = tz or local_tz()
        for row in self.db.execute("SELECT started_at, totals FROM runs WHERE totals IS NOT NULL ORDER BY id DESC LIMIT 200"):
            totals = json.loads(row["totals"])
            if scheduled_only and not totals.get("scheduled"):
                continue
            if totals.get("delivered") and _dt(row["started_at"]).astimezone(tz).date() == day:
                return True
        return False

    def last_delivered(self) -> datetime | None:
        """Start time of the most recent run that delivered a digest."""
        for row in self.db.execute("SELECT started_at, totals FROM runs WHERE totals IS NOT NULL ORDER BY id DESC"):
            if json.loads(row["totals"]).get("delivered"):
                return _dt(row["started_at"])
        return None

    def source_health(self, source: str) -> SourceHealth | None:
        rows = self.db.execute(
            """SELECT sr.ok, sr.item_count, sr.error, r.started_at FROM source_runs sr
               JOIN runs r ON r.id = sr.run_id WHERE sr.source=? ORDER BY sr.run_id DESC LIMIT 30""",
            (source,),
        ).fetchall()
        if not rows:
            return None
        consecutive = 0
        for r in rows:
            if r["ok"]:
                break
            consecutive += 1
        last_success = next((_dt(r["started_at"]) for r in rows if r["ok"]), None)
        recent = rows[:7]
        avg = sum(r["item_count"] for r in recent) / len(recent)
        prior_counts = [r["item_count"] for r in rows[1:8] if r["ok"]]
        went_silent = bool(rows[0]["ok"]) and rows[0]["item_count"] == 0 and any(c > 0 for c in prior_counts)
        return SourceHealth(
            source=source, last_ok=bool(rows[0]["ok"]), last_error=rows[0]["error"],
            last_success=last_success, consecutive_failures=consecutive, avg_items_7=avg,
            went_silent=went_silent,
        )

    # ------------------------------------------------------------ marks, reminders, bot state

    def set_mark(
        self, uid: str, *, source: str, title: str, status: str, now: datetime, kind: str | None = None,
        url: str | None = None, deadline: datetime | None = None, remind_every_days: int | None = None,
        next_remind_at: datetime | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO marks (uid, source, kind, title, url, status, deadline, remind_every_days,
                                  next_remind_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET status=excluded.status, deadline=COALESCE(excluded.deadline, deadline),
                 remind_every_days=excluded.remind_every_days, next_remind_at=excluded.next_remind_at,
                 updated_at=excluded.updated_at""",
            (uid, source, kind, title, url, status, _iso(deadline), remind_every_days, _iso(next_remind_at),
             _iso(now), _iso(now)),
        )
        self.db.commit()

    def update_mark(self, uid: str, now: datetime, **fields) -> None:
        allowed = {"status", "next_remind_at", "remind_every_days"}
        sets = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        self.db.execute(f"UPDATE marks SET {cols}, updated_at=? WHERE uid=?", (*sets.values(), _iso(now), uid))
        self.db.commit()

    def _mark(self, r: sqlite3.Row) -> Mark:
        return Mark(
            uid=r["uid"], source=r["source"], kind=r["kind"], title=r["title"], url=r["url"], status=r["status"],
            deadline=_dt(r["deadline"]), remind_every_days=r["remind_every_days"], next_remind_at=_dt(r["next_remind_at"]),
        )

    def get_mark(self, uid: str) -> Mark | None:
        r = self.db.execute("SELECT * FROM marks WHERE uid=?", (uid,)).fetchone()
        return self._mark(r) if r else None

    def due_reminders(self, now: datetime) -> list[Mark]:
        rows = self.db.execute(
            "SELECT * FROM marks WHERE next_remind_at IS NOT NULL AND next_remind_at <= ? AND status IN (?, ?)"
            " ORDER BY next_remind_at", (_iso(now), *ACTIVE),
        ).fetchall()
        return [self._mark(r) for r in rows]

    def marks_with(self, *statuses: str) -> list[Mark]:
        q = f"SELECT * FROM marks WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY updated_at DESC"
        return [self._mark(r) for r in self.db.execute(q, statuses).fetchall()]

    def source_rejection(self) -> dict[str, tuple[int, int]]:
        """source → (rejected, total) over the owner's marks on digest items."""
        rows = self.db.execute(
            "SELECT source, SUM(status IN ('dismissed','ineligible')), COUNT(*) FROM marks"
            " WHERE source != 'you' GROUP BY source").fetchall()
        return {r[0]: (int(r[1] or 0), int(r[2])) for r in rows}

    def kv_get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))
        self.db.commit()
