"""SQLite persistence. All times are epoch seconds.

``commit_check`` writes the observation, the new date state and any alerts in one
transaction, so an alert can't be lost or duplicated by a crash between them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jetbridge.alerts import DateState
from jetbridge.models import Cabin, CheckResult, CheckStatus, Flight, Watch

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  carrier TEXT NOT NULL, number TEXT NOT NULL,
  origin TEXT NOT NULL, destination TEXT NOT NULL,
  dates TEXT NOT NULL, cabin TEXT NOT NULL, program TEXT NOT NULL,
  pax INTEGER NOT NULL, checks_per_day INTEGER NOT NULL, max_miles INTEGER,
  status TEXT NOT NULL, created_at REAL NOT NULL, next_check_at REAL NOT NULL,
  recipients TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS date_state (
  watch_id INTEGER NOT NULL, day TEXT NOT NULL,
  status TEXT, miles INTEGER, seats_exact INTEGER,
  last_alert_at REAL, last_check_at REAL, last_conclusive_at REAL,
  conclusive_count INTEGER NOT NULL DEFAULT 0, ever_listed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (watch_id, day)
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER NOT NULL, day TEXT NOT NULL, observed_at REAL NOT NULL,
  status TEXT NOT NULL, miles INTEGER, taxes TEXT, currency TEXT,
  seats TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS obs_lookup ON observations (watch_id, day, observed_at);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, dedupe_key TEXT UNIQUE,
  subject TEXT NOT NULL, body TEXT NOT NULL,
  created_at REAL NOT NULL, sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  recipient TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS site_starts (site TEXT NOT NULL, started_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS site_starts_lookup ON site_starts (site, started_at);
"""


@dataclass(frozen=True)
class OutboxItem:
    id: int
    kind: str
    subject: str
    body: str
    created_at: float
    attempts: int
    recipient: str | None = None  # None means the owner's address


@dataclass(frozen=True)
class NewAlert:
    kind: str
    subject: str
    body: str
    dedupe_key: str | None = None
    recipient: str | None = None  # None means the owner's address


def _watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=row["id"],
        flight=Flight(row["carrier"], row["number"]),
        origin=row["origin"],
        destination=row["destination"],
        dates=tuple(date.fromisoformat(d) for d in json.loads(row["dates"])),
        cabin=Cabin(row["cabin"]),
        program=row["program"],
        pax=row["pax"],
        checks_per_day=row["checks_per_day"],
        max_miles=row["max_miles"],
        recipients=tuple(json.loads(row["recipients"])),
        status=row["status"],
        created_at=row["created_at"],
        next_check_at=row["next_check_at"],
    )


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that databases created by earlier versions don't have yet."""
        for table, column, definition in (
            ("watches", "recipients", "TEXT NOT NULL DEFAULT '[]'"),
            ("outbox", "recipient", "TEXT"),
        ):
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ---- watches --------------------------------------------------------------------
    def add_watch(
        self,
        flight: Flight,
        origin: str,
        destination: str,
        dates: tuple[date, ...],
        cabin: Cabin,
        *,
        now: float,
        program: str = "american",
        pax: int = 1,
        checks_per_day: int = 6,
        max_miles: int | None = None,
        recipients: tuple[str, ...] = (),
    ) -> Watch:
        cur = self.conn.execute(
            "INSERT INTO watches (carrier, number, origin, destination, dates, cabin, program,"
            " pax, checks_per_day, max_miles, status, created_at, next_check_at, recipients)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                flight.carrier,
                flight.number,
                origin,
                destination,
                json.dumps([d.isoformat() for d in dates]),
                cabin.value,
                program,
                pax,
                checks_per_day,
                max_miles,
                "active",
                now,
                now,
                json.dumps(list(recipients)),
            ),
        )
        return self.get_watch(int(cur.lastrowid))  # type: ignore[arg-type,return-value]

    def get_watch(self, watch_id: int) -> Watch | None:
        row = self.conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return _watch(row) if row else None

    def list_watches(self, status: str | None = None) -> list[Watch]:
        if status:
            rows = self.conn.execute("SELECT * FROM watches WHERE status=? ORDER BY id", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM watches ORDER BY id")
        return [_watch(r) for r in rows]

    def set_watch_status(self, watch_id: int, status: str) -> None:
        self.conn.execute("UPDATE watches SET status=? WHERE id=?", (status, watch_id))

    def set_recipients(self, watch_id: int, recipients: tuple[str, ...]) -> None:
        self.conn.execute(
            "UPDATE watches SET recipients=? WHERE id=?", (json.dumps(list(recipients)), watch_id)
        )

    def set_next_check(self, watch_id: int, at: float) -> None:
        self.conn.execute("UPDATE watches SET next_check_at=? WHERE id=?", (at, watch_id))

    def delete_watch(self, watch_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            c.execute("DELETE FROM date_state WHERE watch_id=?", (watch_id,))
            c.execute("DELETE FROM observations WHERE watch_id=?", (watch_id,))

    # ---- check state ----------------------------------------------------------------
    def date_state(self, watch_id: int, day: date) -> DateState:
        row = self.conn.execute(
            "SELECT * FROM date_state WHERE watch_id=? AND day=?", (watch_id, day.isoformat())
        ).fetchone()
        if not row:
            return DateState()
        return DateState(
            status=CheckStatus(row["status"]) if row["status"] else None,
            miles=row["miles"],
            seats_exact=row["seats_exact"],
            last_alert_at=row["last_alert_at"],
            last_check_at=row["last_check_at"],
            last_conclusive_at=row["last_conclusive_at"],
            conclusive_count=row["conclusive_count"],
            ever_listed=bool(row["ever_listed"]),
        )

    def commit_check(
        self,
        watch_id: int,
        day: date,
        result: CheckResult,
        state: DateState,
        alerts: list[NewAlert],
        now: float,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO observations (watch_id, day, observed_at, status, miles, taxes,"
                " currency, seats, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    watch_id,
                    day.isoformat(),
                    now,
                    result.status.value,
                    result.miles,
                    str(result.taxes) if result.taxes is not None else None,
                    result.currency,
                    result.seats_text if result.status is CheckStatus.AVAILABLE else None,
                    result.detail,
                ),
            )
            c.execute(
                "INSERT OR REPLACE INTO date_state (watch_id, day, status, miles, seats_exact,"
                " last_alert_at, last_check_at, last_conclusive_at, conclusive_count, ever_listed)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    watch_id,
                    day.isoformat(),
                    state.status.value if state.status else None,
                    state.miles,
                    state.seats_exact,
                    state.last_alert_at,
                    state.last_check_at,
                    state.last_conclusive_at,
                    state.conclusive_count,
                    int(state.ever_listed),
                ),
            )
            for alert in alerts:
                self._enqueue(c, alert, now)

    def history(self, watch_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM observations WHERE watch_id=? ORDER BY observed_at DESC, id DESC"
                " LIMIT ?",
                (watch_id, limit),
            )
        )

    def count_recent(self, watch_id: int, status: CheckStatus, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE watch_id=? AND status=?"
            " AND observed_at>=?",
            (watch_id, status.value, since),
        ).fetchone()
        return int(row["n"])

    # ---- outbox ---------------------------------------------------------------------
    @staticmethod
    def _enqueue(c: sqlite3.Connection, alert: NewAlert, now: float) -> None:
        c.execute(
            "INSERT INTO outbox (kind, dedupe_key, subject, body, created_at, recipient)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (dedupe_key) DO NOTHING",
            (alert.kind, alert.dedupe_key, alert.subject, alert.body, now, alert.recipient),
        )

    def enqueue(self, alert: NewAlert, now: float) -> None:
        with self._tx() as c:
            self._enqueue(c, alert, now)

    def pending_alerts(self) -> list[OutboxItem]:
        rows = self.conn.execute(
            "SELECT id, kind, subject, body, created_at, attempts, recipient FROM outbox"
            " WHERE sent_at IS NULL ORDER BY id"
        )
        return [OutboxItem(**dict(r)) for r in rows]

    def mark_sent(self, item_id: int, now: float) -> None:
        self.conn.execute(
            "UPDATE outbox SET sent_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
            (now, item_id),
        )

    def mark_failed(self, item_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (error, item_id)
        )

    def drop_alert(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM outbox WHERE id=?", (item_id,))

    def sent_alerts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM outbox WHERE sent_at IS NOT NULL ORDER BY id"))

    # ---- key/value ------------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key=?", (key,))

    # ---- request log for the governor -----------------------------------------------
    def record_start(self, site: str, at: float) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO site_starts (site, started_at) VALUES (?,?)", (site, at))
            c.execute("DELETE FROM site_starts WHERE started_at<?", (at - 86400,))

    def window_starts(self, site: str, since: float) -> tuple[int, float | None]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(started_at) AS oldest FROM site_starts"
            " WHERE site=? AND started_at>=?",
            (site, since),
        ).fetchone()
        return int(row["n"]), row["oldest"]
