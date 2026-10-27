"""SQLite persistence for assessments and verification records.

Records are only ever issued from assessments this service produced, so a caller
cannot get arbitrary claims hashed and anchored under VeriChainX's name.
"""

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    product_key TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    record TEXT NOT NULL,
    anchor TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_product_key ON records(product_key, created_at);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def save_assessment(self, assessment_id: str, kind: str, payload: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO assessments (id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (assessment_id, kind, json.dumps(payload), now_iso()),
            )

    def get_assessment(self, assessment_id: str, kind: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM assessments WHERE id = ? AND kind = ?", (assessment_id, kind)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_record(self, row: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO records (id, product_key, record_hash, record, anchor, source_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["record_id"], row["product_key"], row["record_hash"], json.dumps(row["record"]),
                    json.dumps(row["anchor"]), row["source_key"], row["record"]["issued_at"],
                ),
            )

    def update_anchor(self, record_id: str, anchor: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE records SET anchor = ? WHERE id = ?", (json.dumps(anchor), record_id))

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        return {
            "record_id": row["id"],
            "product_key": row["product_key"],
            "record_hash": row["record_hash"],
            "record": json.loads(row["record"]),
            "anchor": json.loads(row["anchor"]),
            "created_at": row["created_at"],
        }

    def get_record(self, record_id: str) -> dict | None:
        return self._row(self._conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone())

    def get_record_by_source(self, source_key: str) -> dict | None:
        return self._row(
            self._conn.execute("SELECT * FROM records WHERE source_key = ?", (source_key,)).fetchone()
        )

    def records_for_product(self, product_key: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM records WHERE product_key = ? ORDER BY created_at, rowid", (product_key,)
        ).fetchall()
        return [self._row(r) for r in rows]
