"""
SQLite-backed bookmark store for X (Twitter) bookmarks.

Adapted from the standalone x-bookmarks-curator project. Provides CRUD over a
local database so bookmarks survive backend restarts, independently of the
transcription jobs system (`jobs.py`).
"""

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Where the database used to default to, and why it moved: `tempfile.gettempdir()`
# is /tmp, and /tmp is a tmpfs here — RAM. Every bookmark, and every review the
# user had made by hand, was gone on the next reboot. The requirement it was
# trying to satisfy is real (SQLite cannot lock over the CIFS mount `DATA_ROOT`
# points at, see config.py) but "not on the network share" and "nowhere at all"
# are different things.
_LEGACY_DB_PATH = Path(tempfile.gettempdir()) / "video-transcriptor-x-bookmarks.db"


def _resolve_db_path() -> Path:
    """Local disk, next to jobs.db — the path .gitignore already reserved."""
    default = Path(__file__).parent / "x_bookmarks.db"
    return Path(os.getenv("X_BOOKMARKS_DB", str(default)))


def _adopt_legacy_db(target: Path, legacy: Path = _LEGACY_DB_PATH) -> bool:
    """Take over a database stranded in temporary storage. Returns whether it did.

    Without this, the fix for the tmpfs bug would be the thing that finally lost
    the bookmarks: the backend would open a brand-new empty file and carry on as
    if nothing had happened.

    An existing target always wins. A second run must never overwrite live data
    with whatever stale copy is still sitting in /tmp. A zero-byte file is not a
    database though — that is what a connection opened before init_db() leaves
    behind, and honouring it would skip the adoption for ever, silently.
    """
    if not legacy.exists():
        return False
    if target.exists() and target.stat().st_size > 0:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(legacy, target)
    logger.info("adopted the bookmarks database stranded in %s → %s", legacy, target)
    return True


DB_PATH = _resolve_db_path()
# DATA_DIR holds the credentials. The videos live under DATA_ROOT instead, so
# pointing that at a big disk doesn't drag the cookies along with it.
DATA_DIR = Path(os.getenv("X_DATA_DIR", str(Path(__file__).parent / "data" / "x-bookmarks")))


def download_dir() -> Path:
    """Where bookmark videos are saved. Resolved per call, so DATA_ROOT can move."""
    return config.media_dirs()["x_downloads"]

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """\
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_url TEXT UNIQUE NOT NULL,
    author TEXT,
    text TEXT,
    thumbnail_url TEXT,
    status TEXT DEFAULT 'new',
    local_file_path TEXT,
    scraped_at TEXT,
    downloaded_at TEXT,
    expanded_text TEXT,
    article_content TEXT,
    has_media INTEGER DEFAULT 0,
    transcription_status TEXT DEFAULT 'none',
    transcript_original TEXT,
    transcript_language TEXT,
    transcribed_at TEXT,
    job_id TEXT
)
"""

# Column definitions for migration checks (name -> type + default).
_MIGRATIONS = {
    "expanded_text": "TEXT",
    "article_content": "TEXT",
    "has_media": "INTEGER DEFAULT 0",
    "transcription_status": "TEXT DEFAULT 'none'",
    "transcript_original": "TEXT",
    "transcript_language": "TEXT",
    "transcribed_at": "TEXT",
    # The transcription job in jobs.db, so a card can link back to its results.
    "job_id": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns that appeared after this DB was first created."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bookmarks)")}
    for col, decl in _MIGRATIONS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE bookmarks ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the bookmarks table (if missing) and apply any pending migrations."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Before the first connection, which would otherwise create an empty file
    # and make the legacy database look like it was never there.
    _adopt_legacy_db(DB_PATH)
    # The download directory is created on demand by download_video(); doing it
    # here would tie startup to DATA_ROOT being mounted and writable, so a
    # mistyped setting or an unplugged disk would stop the backend from booting.
    conn = _get_conn()
    try:
        conn.execute(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _dict_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # Strip JSON blobs from list view (can be large).
    return d


def sync_bookmarks(db: sqlite3.Connection, bookmarks: list[dict]) -> int:
    """Upsert a batch of scraped bookmarks. Returns count inserted."""
    conn = _get_conn() if db is None else db
    inserted = 0
    before = conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
    for b in bookmarks:
        # normalise keys to match the schema exactly.
        row = {
            "tweet_url": b.get("tweet_url", ""),
            "author": b.get("author"),
            "text": b.get("text"),
            "thumbnail_url": b.get("thumbnail_url"),
            "has_media": bool(b.get("has_media")),
            "expanded_text": b.get("expanded_text"),
            "article_content": b.get("article_content"),
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if not row["tweet_url"]:
            continue
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        # A known URL is left alone except for one field: `expanded_text`, which
        # the scraper only learned to fetch later, so every row stored before
        # that carries the ~280-char version the timeline truncates to. Without
        # this backfill `INSERT OR IGNORE` would keep them truncated for ever.
        #
        # The WHERE is what makes it safe to re-run: a permalink that failed
        # yields NULL, and NULL must never overwrite text already captured.
        # Nothing else is touched -- status, local_file_path and job_id are the
        # user's curation, and a re-sync has no business undoing it.
        conn.execute(
            f"INSERT INTO bookmarks ({cols}) VALUES ({placeholders}) "
            "ON CONFLICT(tweet_url) DO UPDATE SET expanded_text = excluded.expanded_text "
            "WHERE excluded.expanded_text IS NOT NULL",
            list(row.values()),
        )
    conn.commit()
    after = conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
    inserted = after - before
    return inserted


def list_bookmarks(db: sqlite3.Connection | None = None, status_filter: str | None = None) -> list[dict]:
    """Return all bookmarks, minus the two genuinely unbounded columns.

    `article_content` and `transcript_original` stay out — those are the ones
    that can run to megabytes, and they are what "excluding large text fields"
    was ever about. The tweet's own text does not belong in that category: a
    tweet is a few hundred characters, and leaving `text`/`expanded_text` out
    meant the card had nothing to render but the URL. `job_id`, `has_media` and
    `transcription_status` are here because the card branches on them.

    Anything omitted is still one `get_bookmark()` away.
    """
    base = (
        "SELECT id, tweet_url, author, text, expanded_text, thumbnail_url, status, "
        "local_file_path, scraped_at, downloaded_at, job_id, has_media, "
        "transcription_status FROM bookmarks"
    )
    where = ""
    params: tuple = ()
    if status_filter:
        where = "WHERE status = ?"
        params = (status_filter,)
    query = f"{base} {where} ORDER BY scraped_at DESC"
    conn = _get_conn() if db is None else db
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        if db is None:
            conn.close()
    return [dict(r) for r in rows]


def get_bookmark(db_id: int, db: sqlite3.Connection | None = None) -> dict | None:
    """Return a single bookmark with full fields (including expanded text)."""
    conn = _get_conn() if db is None else db
    try:
        row = conn.execute("SELECT * FROM bookmarks WHERE id = ?", (db_id,)).fetchone()
    finally:
        if db is None:
            conn.close()
    return dict(row) if row else None


# Statuses a bookmark may be reviewed from, per target status. 'no_media' is
# reversible on purpose: a tweet yt-dlp found no video in can be pushed back for
# another attempt, or dropped back to 'new'. 'downloaded' is terminal.
_REVIEWABLE_FROM = {
    "interesting": ("new", "no_media"),
    "new": ("interesting", "no_media"),
}


def set_interesting(db_id: int, interesting: bool, db: sqlite3.Connection | None = None) -> bool:
    """Transition between new, interesting and no_media. Returns True on success."""
    target = "interesting" if interesting else "new"
    allowed = _REVIEWABLE_FROM[target]
    conn = _get_conn() if db is None else db
    try:
        cur = conn.execute(
            f"UPDATE bookmarks SET status = ? WHERE id = ? AND status IN ({', '.join('?' * len(allowed))})",
            (target, db_id, *allowed),
        )
        if db is None:
            conn.commit()
            conn.close()
        return cur.rowcount > 0
    except Exception:
        if db is None:
            conn.rollback()
            conn.close()
        return False


def set_job(db_id: int, job_id: str, db: sqlite3.Connection | None = None) -> bool:
    """Link a bookmark to the transcription job created for it."""
    conn = _get_conn() if db is None else db
    try:
        cur = conn.execute(
            "UPDATE bookmarks SET job_id = ?, transcription_status = 'running' WHERE id = ?",
            (job_id, db_id),
        )
        if db is None:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if db is None:
            conn.close()


def mark_downloaded(db_id: int, file_path: str, db: sqlite3.Connection | None = None) -> bool:
    """Mark bookmark as downloaded and record the local file path."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _get_conn() if db is None else db
    try:
        cur = conn.execute(
            "UPDATE bookmarks SET status = 'downloaded', local_file_path = ?, downloaded_at = ? WHERE id = ?",
            (file_path, now, db_id),
        )
        if db is None:
            conn.commit()
            conn.close()
        return cur.rowcount > 0
    finally:
        if db is None:
            conn.close()


def mark_no_media(db_id: int, db: sqlite3.Connection | None = None) -> bool:
    """Settle a bookmark yt-dlp found no video in.

    Without this the bookmark stays in 'interesting' forever, indistinguishable
    from one still waiting to be downloaded.
    """
    conn = _get_conn() if db is None else db
    try:
        cur = conn.execute(
            "UPDATE bookmarks SET status = 'no_media' WHERE id = ? AND status IN ('interesting', 'no_media')",
            (db_id,),
        )
        if db is None:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if db is None:
            conn.close()


def delete_bookmark(db_id: int, db: sqlite3.Connection | None = None) -> bool:
    """Remove a bookmark record. If it has a local file, optionally clean up."""
    bm = get_bookmark(db_id)
    conn = _get_conn() if db is None else db
    try:
        with _lock:
            cur = conn.execute("DELETE FROM bookmarks WHERE id = ?", (db_id,))
            if db is None:
                conn.commit()
            success = cur.rowcount > 0
    finally:
        if db is None:
            conn.close()
    # Best-effort file cleanup.
    if success and bm and bm.get("local_file_path"):
        try:
            Path(bm["local_file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return success


def mark_transcribed(db_id: int, status: str, language: str | None = None, transcript: str | None = None, db: sqlite3.Connection | None = None) -> bool:
    """Update transcription status for a bookmark."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _get_conn() if db is None else db
    try:
        cur = conn.execute(
            "UPDATE bookmarks SET transcription_status = ?, transcribed_at = ?"
            + (", transcript_language = ?" if language else "")
            + (", transcript_original = ?" if transcript else "")
            + " WHERE id = ?",
            tuple(
                [status, now]
                + ([language] if language else [])
                + ([transcript] if transcript else [])
                + [db_id]
            ),
        )
        if db is None:
            conn.commit()
            conn.close()
        return cur.rowcount > 0
    finally:
        if db is None:
            conn.close()


# ---------------------------------------------------------------------------
# Module-level initialisation (called once at import time).
# ---------------------------------------------------------------------------

init_db()
