"""SQLite-backed work-order store, interface-compatible with the JSONL one.

v4.2's Phase 5.2 asked for a SQLite migration once the JSONL file grows past
~10MB or once field-level queries are needed. Neither trigger has been reached
(``state/work_orders.jsonl`` is a few KB), so this ships **unused by default** —
but implemented and tested, so the migration is a config change rather than a
project.

The two stores expose the same four methods, so ``WorkOrderStore`` can be swapped
for ``SQLiteWorkOrderStore`` anywhere the chain calls a store.

Why keep both: the JSONL log is append-only, human-readable, and survives a
truncated write with only the last line lost. SQLite adds indexed queries at the
cost of an opaque file and a lock. Neither dominates; the trigger decides.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_PATH = "state/work_orders.db"
MIGRATION_THRESHOLD_BYTES = 10 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_orders_order ON work_orders(order_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def should_migrate(jsonl_path: Any, threshold: int = MIGRATION_THRESHOLD_BYTES) -> dict:
    """Report whether the JSONL file has crossed the migration trigger."""
    try:
        size = os.path.getsize(jsonl_path)
    except OSError:
        return {"exists": False, "size_bytes": 0, "should_migrate": False,
                "threshold_bytes": threshold}
    return {
        "exists": True,
        "size_bytes": size,
        "threshold_bytes": threshold,
        "should_migrate": size >= threshold,
    }


class SQLiteWorkOrderStore:
    """Same contract as ``runtime.storage.WorkOrderStore``."""

    def __init__(self, path: Any = None) -> None:
        self.path = str(path) if path is not None else DEFAULT_PATH
        self.write_error: str | None = None
        self.skipped_lines = 0

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.executescript(SCHEMA)
        return connection

    # -- writing ------------------------------------------------------------

    def import_records(self, records: Any) -> dict:
        """Copy already-stored records in verbatim, skipping the ones present.

        `save` re-stamps `saved_at` — deliberately, so a caller cannot backdate
        a record. A migration is not a caller recording a new event; it is
        moving records that already have timestamps. Re-stamping them made the
        copy unfaithful *and* non-idempotent: every run looked like new data, so
        re-running grew the table 5 -> 10 -> 15 while `list()` (one row per order
        id) kept showing the right count and hid it.

        Returns ``{"imported": n, "skipped": n, "invalid": n}``.
        """
        if not isinstance(records, (list, tuple)):
            raise TypeError("records must be a list of mappings")
        imported = skipped = invalid = 0
        with self._connect() as connection:
            existing = {
                (str(row[0]), str(row[1] or ""))
                for row in connection.execute("SELECT order_id, saved_at FROM work_orders")
            }
            for item in records:
                if not isinstance(item, Mapping):
                    invalid += 1
                    continue
                order_id = item.get("order_id")
                if not isinstance(order_id, str) or not order_id.strip():
                    invalid += 1
                    continue
                record = dict(item)
                record["order_id"] = order_id
                saved_at = str(record.get("saved_at") or _now())
                record["saved_at"] = saved_at
                if (order_id, saved_at) in existing:
                    skipped += 1
                    continue
                connection.execute(
                    "INSERT INTO work_orders (order_id, saved_at, data) VALUES (?, ?, ?)",
                    (order_id, saved_at,
                     json.dumps(record, ensure_ascii=False, default=str)),
                )
                existing.add((order_id, saved_at))
                imported += 1
        return {"imported": imported, "skipped": skipped, "invalid": invalid}

    def save(self, order_id: Any, data: Any) -> dict:
        if not isinstance(order_id, str) or not order_id.strip():
            raise ValueError("order_id must be a nonempty string")
        if not isinstance(data, Mapping):
            raise TypeError("data must be a mapping")
        record = dict(data)
        record["order_id"] = order_id
        record["saved_at"] = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO work_orders (order_id, saved_at, data) VALUES (?, ?, ?)",
                    (order_id, record["saved_at"],
                     json.dumps(record, ensure_ascii=False, default=str)),
                )
        except sqlite3.Error as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            raise
        return record

    def __call__(self, archive: Any) -> bool:
        if not isinstance(archive, Mapping):
            return False
        order_id = archive.get("task_id")
        if not isinstance(order_id, str) or not order_id.strip():
            order_id = "unknown-task"
        try:
            self.save(order_id, dict(archive))
        except (sqlite3.Error, TypeError, ValueError):
            return False
        return True

    # -- reading ------------------------------------------------------------

    def records(self) -> list[dict]:
        self.skipped_lines = 0
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT data FROM work_orders ORDER BY id"
                ).fetchall()
        except sqlite3.Error:
            return []
        records = []
        for (blob,) in rows:
            try:
                parsed = json.loads(blob)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                self.skipped_lines += 1
        return records

    def load(self, order_id: Any) -> dict | None:
        if not isinstance(order_id, str):
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT data FROM work_orders WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            parsed = json.loads(row[0])
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def list(self, limit: int = 50) -> list[dict]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT data FROM work_orders
                    WHERE id IN (
                        SELECT MAX(id) FROM work_orders GROUP BY order_id
                    )
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        records = []
        for (blob,) in rows:
            try:
                parsed = json.loads(blob)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    def history(self, order_id: Any) -> list[dict]:
        """Every record for ``order_id``, oldest first — the same answer the JSONL
        store gives. Without it the viewer's history route raised AttributeError on
        this backend only, and the two stores are supposed to be interchangeable.
        """
        if not isinstance(order_id, str):
            return []
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT data FROM work_orders WHERE order_id = ? ORDER BY id ASC",
                    (order_id,),
                ).fetchall()
        except sqlite3.Error:
            return []
        records = []
        for (blob,) in rows:
            try:
                parsed = json.loads(blob)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    def get_state(self) -> dict:
        return {
            "path": self.path,
            "backend": "sqlite",
            "orders": len(self.list(limit=10_000)),
            "skipped_lines": self.skipped_lines,
            "write_error": self.write_error,
        }


def migrate_jsonl_to_sqlite(
    jsonl_path: Any, db_path: Any, *, keep_source: bool = True
) -> dict:
    """Copy a JSONL log into SQLite, preserving order.

    The source is kept by default: a migration that deletes its input is a
    migration you cannot re-run if the target turns out to be wrong.
    """
    store = SQLiteWorkOrderStore(db_path)
    source = open(jsonl_path, "r", encoding="utf-8")
    try:
        raw_lines = source.read().splitlines()
    finally:
        source.close()

    # Re-running must not duplicate, and the copy must keep the timestamps the
    # source recorded. Both come from import_records, which bypasses the
    # envelope re-stamping that `save` performs by design.
    parsed_records = []
    skipped = 0
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(parsed, dict):
            skipped += 1
            continue
        parsed_records.append(parsed)

    outcome = store.import_records(parsed_records)
    migrated = outcome["imported"]
    skipped += outcome["skipped"] + outcome["invalid"]

    return {
        "migrated": migrated,
        "skipped": skipped,
        "source": str(jsonl_path),
        "target": str(db_path),
        "source_kept": bool(keep_source),
    }
