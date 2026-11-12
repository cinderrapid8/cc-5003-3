"""SQLite-backed persistence with content-addressed audio blobs.

Blobs live on disk under <data>/blobs/<sha256>. They are reference counted
across takes, chapters, noise excerpts and *saved version renders*, so
deleting an unadopted candidate can never remove bytes still referenced by a
historical version. Garbage collection is explicit and only removes blobs
with zero live references.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    chapter_blob_id TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS marks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    sentence_label TEXT NOT NULL,
    range_start INTEGER NOT NULL,
    range_end INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS takes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    mark_id TEXT REFERENCES marks(id),
    blob_id TEXT REFERENCES blobs(id),
    state TEXT NOT NULL CHECK (state IN ('recording','complete','interrupted','abandoned')),
    sample_rate INTEGER,
    frame_count INTEGER NOT NULL DEFAULT 0,
    device_started_at REAL,
    client_clock_ms INTEGER,
    first_frame_device_offset_ms REAL,
    uploaded_byte_size INTEGER,
    finalize_error TEXT,
    created_at REAL NOT NULL,
    finalized_at REAL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    take_id TEXT NOT NULL REFERENCES takes(id),
    mark_id TEXT NOT NULL REFERENCES marks(id),
    trim_start INTEGER NOT NULL,
    trim_end INTEGER NOT NULL,
    frame_count INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS noise_excerpts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    blob_id TEXT NOT NULL REFERENCES blobs(id),
    frame_count INTEGER NOT NULL,
    rms REAL NOT NULL,
    peak REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS selections (
    mark_id TEXT PRIMARY KEY REFERENCES marks(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    mode TEXT NOT NULL CHECK (mode IN ('shift','keep_duration')),
    fade_ms REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS render_configs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    mode TEXT NOT NULL,
    fade_ms REAL NOT NULL,
    noise_id TEXT REFERENCES noise_excerpts(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    label TEXT NOT NULL,
    blob_id TEXT NOT NULL REFERENCES blobs(id),
    frame_count INTEGER NOT NULL,
    cue_json TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    render_config_id TEXT REFERENCES render_configs(id),
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.blobs_dir = os.path.join(data_dir, "blobs")
        os.makedirs(self.blobs_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "studio.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ blobs
    def put_blob(self, data: bytes) -> str:
        """Store bytes content-addressed; returns blob id (uuid alias)."""
        digest = hashlib.sha256(data).hexdigest()
        path = os.path.join(self.blobs_dir, digest)
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        with self._lock:
            row = self._conn.execute("SELECT id FROM blobs WHERE sha256=?", (digest,)).fetchone()
            if row:
                return row["id"]
            blob_id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO blobs(id, sha256, byte_size, created_at) VALUES (?,?,?,?)",
                (blob_id, digest, len(data), time.time()),
            )
            self._conn.commit()
            return blob_id

    def blob_path(self, blob_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT sha256 FROM blobs WHERE id=?", (blob_id,)).fetchone()
        if not row:
            raise KeyError(f"blob {blob_id} not found")
        return os.path.join(self.blobs_dir, row["sha256"])

    def read_blob(self, blob_id: str) -> bytes:
        with open(self.blob_path(blob_id), "rb") as fh:
            return fh.read()

    def gc_blobs(self) -> dict:
        """Delete blobs not referenced by any live owner. Returns counts."""
        with self._lock:
            refs = self._conn.execute(
                """
                SELECT blob_id FROM projects
                UNION SELECT blob_id FROM takes WHERE blob_id IS NOT NULL
                UNION SELECT blob_id FROM noise_excerpts
                UNION SELECT blob_id FROM versions
                """
            ).fetchall()
            live = {r["blob_id"] for r in refs}
            all_blobs = self._conn.execute("SELECT id FROM blobs").fetchall()
            removed = 0
            for r in all_blobs:
                if r["id"] not in live:
                    path = self.blob_path(r["id"])
                    if os.path.exists(path):
                        os.remove(path)
                    self._conn.execute("DELETE FROM blobs WHERE id=?", (r["id"],))
                    removed += 1
            self._conn.commit()
            return {"removed": removed, "kept": len(live)}

    # ---------------------------------------------------------------- generic
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        return dict(row) if row is not None else None

    def transaction(self):
        return _Tx(self._lock, self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Tx:
    def __init__(self, lock: threading.RLock, conn: sqlite3.Connection):
        self._lock = lock
        self._conn = conn

    def __enter__(self):
        self._lock.acquire()
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._lock.release()


def now_ms() -> int:
    return int(time.time() * 1000)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
