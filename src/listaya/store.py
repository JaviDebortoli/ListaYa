"""SQLite persistence: confirmed aliases, per-supplier VAT answers, runs and model usage.

The catalog itself stays in the merchant's CSV (the source of truth); this database
keeps what the assistant learns and the metrics of every processed list (RF-12).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from listaya.token_factory import UsageRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS aliases (
    supplier_id TEXT NOT NULL,
    key TEXT NOT NULL,
    sku TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (supplier_id, key)
);
CREATE TABLE IF NOT EXISTS supplier_vat (
    supplier_id TEXT PRIMARY KEY,
    includes_vat INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    supplier TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    rows INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    priced INTEGER NOT NULL DEFAULT 0,
    alerts INTEGER NOT NULL DEFAULT 0,
    unmatched INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    latency_seconds REAL NOT NULL,
    attempts INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class RunRecord:
    id: int
    chat_id: str
    supplier: str
    source: str
    status: str
    rows: int
    matched: int
    priced: int
    alerts: int
    unmatched: int
    updated: int
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    duration_seconds: float


class SqliteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # One short-lived connection per operation: simple and safe across asyncio tasks.
        # "with connection" only commits, so close it explicitly (Windows keeps open files locked).
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # AliasStore protocol (listaya.matching)
    def get(self, supplier_id: str, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT sku FROM aliases WHERE supplier_id = ? AND key = ?", (supplier_id, key)
            ).fetchone()
        return row[0] if row else None

    def save(self, supplier_id: str, key: str, sku: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO aliases (supplier_id, key, sku, created_at) VALUES (?, ?, ?, ?)",
                (supplier_id, key, sku, time.time()),
            )

    def supplier_includes_vat(self, supplier_id: str) -> bool | None:
        with self._connect() as db:
            row = db.execute("SELECT includes_vat FROM supplier_vat WHERE supplier_id = ?", (supplier_id,)).fetchone()
        return bool(row[0]) if row else None

    def set_supplier_includes_vat(self, supplier_id: str, includes_vat: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO supplier_vat (supplier_id, includes_vat) VALUES (?, ?)",
                (supplier_id, int(includes_vat)),
            )

    def start_run(self, chat_id: str, supplier: str, source: str) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO runs (chat_id, supplier, source, status, started_at) VALUES (?, ?, ?, 'processing', ?)",
                (chat_id, supplier, source, time.time()),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, status: str, *, rows: int, matched: int, priced: int, alerts: int, unmatched: int,
        updated: int = 0,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, rows = ?, matched = ?, priced = ?, alerts = ?, "
                "unmatched = ?, updated = ? WHERE id = ?",
                (status, time.time(), rows, matched, priced, alerts, unmatched, updated, run_id),
            )

    def set_run_status(self, run_id: int, status: str, updated: int | None = None) -> None:
        with self._connect() as db:
            if updated is None:
                db.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
            else:
                db.execute("UPDATE runs SET status = ?, updated = ? WHERE id = ?", (status, updated, run_id))

    def log_usage(self, run_id: int | None, record: UsageRecord) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage_log (run_id, model, prompt_tokens, completion_tokens, latency_seconds, attempts, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    record.model,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.latency_seconds,
                    record.attempts,
                    record.status,
                    time.time(),
                ),
            )

    def run(self, run_id: int) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT r.id, r.chat_id, r.supplier, r.source, r.status, r.rows, r.matched, r.priced, r.alerts, "
                "r.unmatched, r.updated, COUNT(u.id), COALESCE(SUM(u.prompt_tokens), 0), "
                "COALESCE(SUM(u.completion_tokens), 0), COALESCE(r.finished_at, r.started_at) - r.started_at "
                "FROM runs r LEFT JOIN usage_log u ON u.run_id = r.id WHERE r.id = ? GROUP BY r.id",
                (run_id,),
            ).fetchone()
        return RunRecord(*row) if row else None
