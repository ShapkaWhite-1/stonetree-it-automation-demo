from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .errors import InventoryError
from .models import AssetReservation, OperationStatus, StepResult, WorkflowResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteRepository:
    """Persists idempotency keys, workflow events, assets and assignments."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_json TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (request_id) REFERENCES operations(request_id)
                );

                CREATE TABLE IF NOT EXISTS assets (
                    asset_tag TEXT PRIMARY KEY,
                    asset_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('available', 'assigned'))
                );

                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_tag TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    returned_at TEXT,
                    FOREIGN KEY (asset_tag) REFERENCES assets(asset_tag)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_assignment_per_asset
                    ON assignments(asset_tag)
                    WHERE returned_at IS NULL;
                """
            )

    def begin_operation(self, request_id: str, operation: str, employee_id: str) -> bool:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO operations(request_id, operation, employee_id, status, started_at) VALUES (?, ?, ?, ?, ?)",
                    (request_id, operation, employee_id, OperationStatus.RUNNING.value, utc_now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_result(self, request_id: str) -> WorkflowResult | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT result_json FROM operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return WorkflowResult.from_dict(json.loads(row["result_json"]))

    def finish_operation(self, result: WorkflowResult) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE operations
                SET status = ?, finished_at = ?, result_json = ?
                WHERE request_id = ? AND status = ?
                """,
                (
                    result.status.value,
                    utc_now(),
                    json.dumps(result.to_dict(), sort_keys=True),
                    result.request_id,
                    OperationStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Operation is not running: {result.request_id}")

    def append_event(self, request_id: str, step: StepResult) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO events(request_id, step_name, status, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, step.name, step.status.value, json.dumps(step.details, sort_keys=True), utc_now()),
            )

    def seed_assets(self, assets: Iterable[tuple[str, str]]) -> None:
        normalized = [(tag.strip(), asset_type.strip().lower()) for tag, asset_type in assets]
        if any(not tag or not asset_type for tag, asset_type in normalized):
            raise InventoryError("Asset tag and type must be non-empty")
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR IGNORE INTO assets(asset_tag, asset_type, status) VALUES (?, ?, 'available')",
                normalized,
            )

    def reserve_assets(self, employee_id: str, asset_types: tuple[str, ...]) -> AssetReservation:
        all_tags: list[str] = []
        new_tags: list[str] = []
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for asset_type in asset_types:
                existing = connection.execute(
                    """
                    SELECT a.asset_tag
                    FROM assignments a
                    JOIN assets inventory ON inventory.asset_tag = a.asset_tag
                    WHERE a.employee_id = ? AND inventory.asset_type = ? AND a.returned_at IS NULL
                    ORDER BY a.id
                    LIMIT 1
                    """,
                    (employee_id, asset_type),
                ).fetchone()
                if existing:
                    all_tags.append(str(existing["asset_tag"]))
                    continue

                available = connection.execute(
                    "SELECT asset_tag FROM assets WHERE asset_type = ? AND status = 'available' ORDER BY asset_tag LIMIT 1",
                    (asset_type,),
                ).fetchone()
                if available is None:
                    raise InventoryError(f"No available asset of type '{asset_type}'")
                asset_tag = str(available["asset_tag"])
                connection.execute("UPDATE assets SET status = 'assigned' WHERE asset_tag = ?", (asset_tag,))
                connection.execute(
                    "INSERT INTO assignments(asset_tag, employee_id, assigned_at) VALUES (?, ?, ?)",
                    (asset_tag, employee_id, utc_now()),
                )
                all_tags.append(asset_tag)
                new_tags.append(asset_tag)
        return AssetReservation(tuple(all_tags), tuple(new_tags))

    def release_assets(self, employee_id: str, asset_tags: Iterable[str] | None = None) -> tuple[str, ...]:
        requested_tags = tuple(asset_tags) if asset_tags is not None else None
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if requested_tags is None:
                rows = connection.execute(
                    "SELECT asset_tag FROM assignments WHERE employee_id = ? AND returned_at IS NULL",
                    (employee_id,),
                ).fetchall()
            elif not requested_tags:
                return ()
            else:
                placeholders = ",".join("?" for _ in requested_tags)
                rows = connection.execute(
                    f"SELECT asset_tag FROM assignments WHERE employee_id = ? AND returned_at IS NULL AND asset_tag IN ({placeholders})",
                    (employee_id, *requested_tags),
                ).fetchall()

            released = tuple(str(row["asset_tag"]) for row in rows)
            for asset_tag in released:
                connection.execute(
                    "UPDATE assignments SET returned_at = ? WHERE employee_id = ? AND asset_tag = ? AND returned_at IS NULL",
                    (utc_now(), employee_id, asset_tag),
                )
                connection.execute("UPDATE assets SET status = 'available' WHERE asset_tag = ?", (asset_tag,))
        return released

    def active_assignments(self, employee_id: str) -> tuple[str, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT asset_tag FROM assignments WHERE employee_id = ? AND returned_at IS NULL ORDER BY asset_tag",
                (employee_id,),
            ).fetchall()
        return tuple(str(row["asset_tag"]) for row in rows)

    def asset_status(self, asset_tag: str) -> str | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT status FROM assets WHERE asset_tag = ?", (asset_tag,)).fetchone()
        return str(row["status"]) if row else None
