"""SQLite persistence: last snapshot per race, batch baseline, and alert log.

Snapshots and new alert rows are written in one transaction before delivery is
attempted, so a crash or restart never re-detects (and re-sends) the same change.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .model import Snapshot
from .policy import Alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    race_id INTEGER PRIMARY KEY,
    snapshot TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
    race_id INTEGER PRIMARY KEY,
    snapshot TEXT NOT NULL,
    alerted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    race_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL,
    last_updated TEXT,
    critical INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed | dropped
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS alerts_race ON alerts(race_id, created_at);
CREATE TABLE IF NOT EXISTS sends (
    at REAL NOT NULL,
    kind TEXT NOT NULL            -- 'single' or 'digest'
);
CREATE TABLE IF NOT EXISTS fetch_errors (
    race_id INTEGER PRIMARY KEY,
    message TEXT,
    at REAL
);
"""

MAX_SEND_ATTEMPTS = 5
STALE_PENDING_SECONDS = 30 * 60


class State:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- snapshots
    def last_snapshot(self, race_id: int) -> Snapshot | None:
        row = self.db.execute("SELECT snapshot FROM snapshots WHERE race_id=?", (race_id,)).fetchone()
        return Snapshot.from_dict(json.loads(row["snapshot"])) if row else None

    def all_snapshots(self) -> dict[int, tuple[Snapshot, float]]:
        rows = self.db.execute("SELECT race_id, snapshot, fetched_at FROM snapshots").fetchall()
        return {r["race_id"]: (Snapshot.from_dict(json.loads(r["snapshot"])), r["fetched_at"]) for r in rows}

    def baseline(self, race_id: int) -> tuple[Snapshot | None, float | None]:
        row = self.db.execute("SELECT snapshot, alerted_at FROM baselines WHERE race_id=?",
                              (race_id,)).fetchone()
        if not row:
            return None, None
        return Snapshot.from_dict(json.loads(row["snapshot"])), row["alerted_at"]

    def commit_poll(self, snap: Snapshot, alerts: list[Alert], advance_baseline: bool,
                    now: float | None = None) -> list[Alert]:
        """Atomically store the snapshot and any *new* alerts. Returns alerts that were new."""
        now = time.time() if now is None else now
        fresh: list[Alert] = []
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
                            (snap.race_id, json.dumps(snap.to_dict()), now))
            if advance_baseline:
                self.db.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?)",
                                (snap.race_id, json.dumps(snap.to_dict()), now))
            for a in alerts:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO alerts (alert_id, race_id, kind, priority, title, body, tags,"
                    " last_updated, critical, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (a.alert_id, a.race_id, a.kind, a.priority, a.title, a.body, json.dumps(a.tags),
                     a.last_updated, int(a.critical), now))
                if cur.rowcount:
                    fresh.append(a)
            self.db.execute("DELETE FROM fetch_errors WHERE race_id=?", (snap.race_id,))
        return fresh

    def record_fetch_error(self, race_id: int, message: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO fetch_errors VALUES (?,?,?)",
                            (race_id, message, time.time()))

    def fetch_errors(self) -> dict[int, tuple[str, float]]:
        return {r["race_id"]: (r["message"], r["at"])
                for r in self.db.execute("SELECT * FROM fetch_errors")}

    # ---------------------------------------------------------------- alerts
    def pending_alerts(self, now: float | None = None) -> list[Alert]:
        """Alerts to (re)try. Old pending ones are dropped rather than sent late."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE alerts SET status='dropped' WHERE status IN ('pending','failed')"
                            " AND (created_at < ? OR attempts >= ?)",
                            (now - STALE_PENDING_SECONDS, MAX_SEND_ATTEMPTS))
        rows = self.db.execute("SELECT * FROM alerts WHERE status IN ('pending','failed')"
                               " ORDER BY priority DESC, created_at").fetchall()
        return [_row_to_alert(r) for r in rows]

    def mark(self, alert_id: str, ok: bool, status: str = "sent"):
        with self.db:
            if ok:
                self.db.execute("UPDATE alerts SET status=?, sent_at=?, attempts=attempts+1"
                                " WHERE alert_id=?", (status, time.time(), alert_id))
            else:
                self.db.execute("UPDATE alerts SET status='failed', attempts=attempts+1"
                                " WHERE alert_id=?", (alert_id,))

    def record_send(self, kind: str, at: float | None = None):
        with self.db:
            self.db.execute("INSERT INTO sends VALUES (?,?)", (time.time() if at is None else at, kind))

    def sends_since(self, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sends WHERE at >= ?", (since,)).fetchone()[0]

    def last_send(self, kind: str) -> float | None:
        return self.db.execute("SELECT MAX(at) FROM sends WHERE kind=?", (kind,)).fetchone()[0]

    def recent_alerts(self, limit: int = 50, race_id: int | None = None) -> list[dict]:
        q = "SELECT * FROM alerts"
        args: tuple = ()
        if race_id is not None:
            q += " WHERE race_id=?"
            args = (race_id,)
        q += " ORDER BY created_at DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(q, args + (limit,))]


def _row_to_alert(r) -> Alert:
    return Alert(alert_id=r["alert_id"], race_id=r["race_id"], kind=r["kind"], priority=r["priority"],
                 title=r["title"], body=r["body"], tags=json.loads(r["tags"]),
                 last_updated=r["last_updated"], critical=bool(r["critical"]))
