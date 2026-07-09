"""
SQLite-backed job store. Stdlib sqlite3, no new dependency; survives the dev
server's `reload=True` restarts, unlike an in-memory dict.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(os.getenv("JOBS_DB_PATH", str(Path(__file__).parent / "jobs.db")))

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                filename TEXT,
                status TEXT NOT NULL,
                stage TEXT,
                progress REAL DEFAULT 0,
                error TEXT,
                result_json TEXT,
                created_at REAL,
                updated_at REAL,
                segments_done INTEGER DEFAULT 0,
                segments_total INTEGER DEFAULT 0
            )
            """
        )
        # Migration for DBs created before segments_done/segments_total existed.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for col in ("segments_done", "segments_total"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} INTEGER DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def create_job(filename: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = _get_conn()
    try:
        with _lock:
            conn.execute(
                "INSERT INTO jobs (id, filename, status, stage, progress, error, result_json, "
                "created_at, updated_at) VALUES (?, ?, 'queued', 'uploading', 0.0, NULL, NULL, ?, ?)",
                (job_id, filename, now, now),
            )
            conn.commit()
    finally:
        conn.close()
    return job_id


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    if "result" in fields:
        fields["result_json"] = json.dumps(fields.pop("result"))
    fields["updated_at"] = time.time()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn = _get_conn()
    try:
        with _lock:
            conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", values)
            conn.commit()
    finally:
        conn.close()


def get_job(job_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    job = dict(row)
    result_json = job.pop("result_json", None)
    job["result"] = json.loads(result_json) if result_json else None
    return job


def delete_job(job_id: str) -> bool:
    """Returns True if a row was deleted, False if job_id didn't exist."""
    conn = _get_conn()
    try:
        with _lock:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def list_jobs(limit: int = 50) -> list[dict]:
    """Job history for the frontend list view. Excludes result_json (can be
    large for long videos) -- callers fetch a single job's full result via
    get_job() when the user picks it from the list."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, filename, status, stage, progress, error, "
            "segments_done, segments_total, created_at, updated_at "
            "FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


init_db()
