"""SQLite-backed state for the sync pipeline.

Stores:
  * delta_tokens: drive_id -> deltaLink + last_synced_at
  * file_vectors: file_id -> list of pinecone vector IDs and content hash, so
    we can delete old chunks before re-upserting an updated file.
  * sync_runs:   per-run audit log (counts, errors, duration).

SQLite is plenty for this workload (sync runs every few minutes, low write
contention). The DB file lives in STATE_DIR which should be a Railway Volume.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from .logging_config import get_logger

log = get_logger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS delta_tokens (
    drive_id        TEXT PRIMARY KEY,
    delta_link      TEXT NOT NULL,
    drive_metadata  TEXT,
    last_synced_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS file_vectors (
    file_id         TEXT NOT NULL,
    drive_id        TEXT NOT NULL,
    namespace       TEXT NOT NULL,
    content_hash    TEXT,
    vector_ids      TEXT NOT NULL,   -- JSON array of ids
    last_modified   TEXT,
    last_indexed_at REAL NOT NULL,
    PRIMARY KEY (file_id, drive_id)
);

CREATE INDEX IF NOT EXISTS ix_file_vectors_drive ON file_vectors(drive_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    drives_scanned  INTEGER DEFAULT 0,
    files_indexed   INTEGER DEFAULT 0,
    files_deleted   INTEGER DEFAULT 0,
    files_skipped   INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS sync_progress (
    drive_id        TEXT PRIMARY KEY,
    drive_label     TEXT,
    estimated_total INTEGER,
    files_seen      INTEGER NOT NULL DEFAULT 0,
    files_processed INTEGER NOT NULL DEFAULT 0,
    current_file    TEXT,
    phase           TEXT,                          -- 'discovering' | 'syncing' | 'done'
    started_at      REAL,
    updated_at      REAL NOT NULL
);
"""


@dataclass
class FileVectorRecord:
    file_id: str
    drive_id: str
    namespace: str
    content_hash: Optional[str]
    vector_ids: list[str]
    last_modified: Optional[str]


class StateStore:
    def __init__(self, state_dir: str) -> None:
        os.makedirs(state_dir, exist_ok=True)
        self._path = os.path.join(state_dir, "sync_state.sqlite3")
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        log.info("state.init", path=self._path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Each call opens a fresh connection — SQLite is fine with that and
        # it sidesteps thread-affinity issues with apscheduler workers.
        conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    # ─── delta_tokens ─────────────────────────────────────────────────
    def get_delta_link(self, drive_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT delta_link FROM delta_tokens WHERE drive_id = ?",
                (drive_id,),
            ).fetchone()
            return row["delta_link"] if row else None

    def set_delta_link(self, drive_id: str, link: str, drive_metadata: dict | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delta_tokens (drive_id, delta_link, drive_metadata, last_synced_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(drive_id) DO UPDATE SET
                    delta_link = excluded.delta_link,
                    drive_metadata = excluded.drive_metadata,
                    last_synced_at = excluded.last_synced_at
                """,
                (drive_id, link, json.dumps(drive_metadata or {}), time.time()),
            )

    # ─── file_vectors ─────────────────────────────────────────────────
    def get_file_record(self, drive_id: str, file_id: str) -> Optional[FileVectorRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM file_vectors WHERE drive_id = ? AND file_id = ?",
                (drive_id, file_id),
            ).fetchone()
            if not row:
                return None
            return FileVectorRecord(
                file_id=row["file_id"],
                drive_id=row["drive_id"],
                namespace=row["namespace"],
                content_hash=row["content_hash"],
                vector_ids=json.loads(row["vector_ids"]),
                last_modified=row["last_modified"],
            )

    def upsert_file_record(self, rec: FileVectorRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO file_vectors
                  (file_id, drive_id, namespace, content_hash, vector_ids,
                   last_modified, last_indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, drive_id) DO UPDATE SET
                    namespace = excluded.namespace,
                    content_hash = excluded.content_hash,
                    vector_ids = excluded.vector_ids,
                    last_modified = excluded.last_modified,
                    last_indexed_at = excluded.last_indexed_at
                """,
                (
                    rec.file_id,
                    rec.drive_id,
                    rec.namespace,
                    rec.content_hash,
                    json.dumps(rec.vector_ids),
                    rec.last_modified,
                    time.time(),
                ),
            )

    def delete_file_record(self, drive_id: str, file_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM file_vectors WHERE drive_id = ? AND file_id = ?",
                (drive_id, file_id),
            )

    # ─── sync_runs ────────────────────────────────────────────────────
    def start_run(self) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_runs (started_at) VALUES (?)",
                (time.time(),),
            )
            return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        drives_scanned: int,
        files_indexed: int,
        files_deleted: int,
        files_skipped: int,
        errors: int,
        notes: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, drives_scanned = ?, files_indexed = ?,
                    files_deleted = ?, files_skipped = ?, errors = ?, notes = ?
                WHERE id = ?
                """,
                (
                    time.time(),
                    drives_scanned,
                    files_indexed,
                    files_deleted,
                    files_skipped,
                    errors,
                    notes,
                    run_id,
                ),
            )

    def latest_runs(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── progress (live during a sync run) ───────────────────────────
    def reset_progress(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sync_progress")

    def upsert_drive_progress(
        self,
        drive_id: str,
        *,
        drive_label: str | None = None,
        estimated_total: int | None = None,
        files_seen: int | None = None,
        files_processed: int | None = None,
        current_file: str | None = None,
        phase: str | None = None,
        mark_started: bool = False,
    ) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM sync_progress WHERE drive_id = ?", (drive_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO sync_progress
                      (drive_id, drive_label, estimated_total,
                       files_seen, files_processed, current_file, phase,
                       started_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        drive_id,
                        drive_label or "",
                        estimated_total,
                        files_seen or 0,
                        files_processed or 0,
                        current_file,
                        phase or "discovering",
                        time.time() if mark_started else None,
                        time.time(),
                    ),
                )
            else:
                fields = []
                params: list = []
                if drive_label is not None:
                    fields.append("drive_label = ?"); params.append(drive_label)
                if estimated_total is not None:
                    fields.append("estimated_total = ?"); params.append(estimated_total)
                if files_seen is not None:
                    fields.append("files_seen = ?"); params.append(files_seen)
                if files_processed is not None:
                    fields.append("files_processed = ?"); params.append(files_processed)
                if current_file is not None:
                    fields.append("current_file = ?"); params.append(current_file)
                if phase is not None:
                    fields.append("phase = ?"); params.append(phase)
                if mark_started:
                    fields.append("started_at = ?"); params.append(time.time())
                fields.append("updated_at = ?"); params.append(time.time())
                params.append(drive_id)
                conn.execute(
                    f"UPDATE sync_progress SET {', '.join(fields)} WHERE drive_id = ?",
                    params,
                )

    def increment_drive_progress(
        self,
        drive_id: str,
        *,
        seen_delta: int = 0,
        processed_delta: int = 0,
        current_file: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_progress
                SET files_seen = files_seen + ?,
                    files_processed = files_processed + ?,
                    current_file = COALESCE(?, current_file),
                    updated_at = ?
                WHERE drive_id = ?
                """,
                (seen_delta, processed_delta, current_file, time.time(), drive_id),
            )

    def progress_snapshot(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_progress ORDER BY started_at NULLS LAST, drive_label"
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._connect() as conn:
            files = conn.execute("SELECT COUNT(*) AS c FROM file_vectors").fetchone()["c"]
            drives = conn.execute("SELECT COUNT(*) AS c FROM delta_tokens").fetchone()["c"]
            last = conn.execute(
                "SELECT * FROM sync_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "indexed_files": files,
                "tracked_drives": drives,
                "last_run": dict(last) if last else None,
            }
