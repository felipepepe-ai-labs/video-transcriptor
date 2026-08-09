"""Unit coverage for the X bookmarks store.

Ported from the standalone x-bookmarks-curator's vitest suite (db.test.js,
db.migration.test.js, bookmarks.test.js, sync.upsert.test.js), which is the
only place this subsystem was ever tested before it was adapted into this app.
"""

import sqlite3

import pytest

import x_bookmarks as xb


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, fully-migrated bookmarks DB backed by its own file."""
    path = tmp_path / "bookmarks.db"
    monkeypatch.setattr(xb, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(xb.SCHEMA)
    conn.commit()
    yield conn
    conn.close()


def _insert(conn, url, **fields):
    cols = ["tweet_url", *fields]
    values = [url, *fields.values()]
    placeholders = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO bookmarks ({', '.join(cols)}) VALUES ({placeholders})", values
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Schema and migrations
# ---------------------------------------------------------------------------

def test_migrate_adds_columns_missing_from_an_older_database(tmp_path):
    """A DB created before the expanded-text columns existed must gain them."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute(
        "CREATE TABLE bookmarks ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " tweet_url TEXT UNIQUE NOT NULL,"
        " status TEXT DEFAULT 'new')"
    )

    xb._migrate(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(bookmarks)")}
    assert set(xb._MIGRATIONS).issubset(columns)
    conn.close()


def test_migrate_is_idempotent_across_restarts(tmp_path):
    """Re-running migrations must not raise 'duplicate column name'."""
    conn = sqlite3.connect(tmp_path / "repeat.db")
    conn.execute(xb.SCHEMA)

    xb._migrate(conn)
    xb._migrate(conn)  # simulates a second backend start against the same file

    conn.close()


# ---------------------------------------------------------------------------
# sync_bookmarks
# ---------------------------------------------------------------------------

def test_sync_bookmarks_inserts_scraped_rows(db):
    inserted = xb.sync_bookmarks(
        db,
        [
            {"tweet_url": "https://x.com/a/status/1", "author": "a", "text": "hi"},
            {"tweet_url": "https://x.com/b/status/2", "author": "b", "text": "yo"},
        ],
    )

    assert inserted == 2
    assert db.execute("SELECT count(*) FROM bookmarks").fetchone()[0] == 2


def test_sync_bookmarks_records_scraped_at(db):
    """scraped_at drives list ordering, so it must actually be persisted."""
    xb.sync_bookmarks(db, [{"tweet_url": "https://x.com/a/status/1"}])

    scraped_at = db.execute("SELECT scraped_at FROM bookmarks").fetchone()[0]
    assert scraped_at


def test_sync_bookmarks_stores_expanded_and_article_content(db):
    xb.sync_bookmarks(
        db,
        [
            {
                "tweet_url": "https://x.com/a/status/1",
                "expanded_text": "the full untruncated tweet",
                "article_content": "the article body",
                "has_media": True,
            }
        ],
    )

    row = db.execute(
        "SELECT expanded_text, article_content, has_media FROM bookmarks"
    ).fetchone()
    assert row["expanded_text"] == "the full untruncated tweet"
    assert row["article_content"] == "the article body"
    assert row["has_media"] == 1


def test_sync_bookmarks_ignores_already_known_urls(db):
    bookmark = {"tweet_url": "https://x.com/a/status/1", "text": "first"}
    xb.sync_bookmarks(db, [bookmark])

    inserted = xb.sync_bookmarks(db, [bookmark])

    assert inserted == 0
    assert db.execute("SELECT count(*) FROM bookmarks").fetchone()[0] == 1


def test_sync_bookmarks_never_overwrites_a_reviewed_status(db):
    """Re-syncing must not drag a reviewed bookmark back to 'new'."""
    xb.sync_bookmarks(db, [{"tweet_url": "https://x.com/a/status/1"}])
    db.execute("UPDATE bookmarks SET status = 'interesting'")
    db.commit()

    xb.sync_bookmarks(db, [{"tweet_url": "https://x.com/a/status/1"}])

    assert db.execute("SELECT status FROM bookmarks").fetchone()[0] == "interesting"


def test_sync_bookmarks_skips_entries_without_a_url(db):
    inserted = xb.sync_bookmarks(db, [{"author": "nobody"}, {"tweet_url": ""}])

    assert inserted == 0


def test_sync_bookmarks_accepts_an_empty_batch(db):
    assert xb.sync_bookmarks(db, []) == 0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def test_list_bookmarks_filters_by_status(db):
    _insert(db, "https://x.com/a/status/1", status="new")
    _insert(db, "https://x.com/b/status/2", status="interesting")

    rows = xb.list_bookmarks(db=db, status_filter="interesting")

    assert [r["tweet_url"] for r in rows] == ["https://x.com/b/status/2"]


def test_list_bookmarks_returns_every_status_when_unfiltered(db):
    _insert(db, "https://x.com/a/status/1", status="new")
    _insert(db, "https://x.com/b/status/2", status="downloaded")

    assert len(xb.list_bookmarks(db=db)) == 2


def test_get_bookmark_returns_the_full_row(db):
    row_id = _insert(db, "https://x.com/a/status/1", expanded_text="the long text")

    bookmark = xb.get_bookmark(row_id, db=db)

    assert bookmark["expanded_text"] == "the long text"


def test_get_bookmark_returns_none_for_an_unknown_id(db):
    assert xb.get_bookmark(999999, db=db) is None


# ---------------------------------------------------------------------------
# Status machine
# ---------------------------------------------------------------------------

def test_set_interesting_promotes_a_new_bookmark(db):
    row_id = _insert(db, "https://x.com/a/status/1", status="new")

    assert xb.set_interesting(row_id, True, db=db) is True
    assert xb.get_bookmark(row_id, db=db)["status"] == "interesting"


def test_set_interesting_demotes_back_to_new(db):
    row_id = _insert(db, "https://x.com/a/status/1", status="interesting")

    assert xb.set_interesting(row_id, False, db=db) is True
    assert xb.get_bookmark(row_id, db=db)["status"] == "new"


def test_set_interesting_rejects_a_downloaded_bookmark(db):
    """The status machine is linear: downloaded is terminal, not re-reviewable."""
    row_id = _insert(db, "https://x.com/a/status/1", status="downloaded")

    assert xb.set_interesting(row_id, True, db=db) is False
    assert xb.get_bookmark(row_id, db=db)["status"] == "downloaded"


def test_set_interesting_reports_failure_for_an_unknown_id(db):
    assert xb.set_interesting(999999, True, db=db) is False


def test_mark_downloaded_records_the_local_path(db):
    row_id = _insert(db, "https://x.com/a/status/1", status="interesting")

    assert xb.mark_downloaded(row_id, "/tmp/clip.mp4", db=db) is True

    bookmark = xb.get_bookmark(row_id, db=db)
    assert bookmark["status"] == "downloaded"
    assert bookmark["local_file_path"] == "/tmp/clip.mp4"
    assert bookmark["downloaded_at"]


# ---------------------------------------------------------------------------
# Transcription bookkeeping
# ---------------------------------------------------------------------------

def test_mark_transcribed_updates_only_the_target_bookmark(db):
    target = _insert(db, "https://x.com/a/status/1")
    other = _insert(db, "https://x.com/b/status/2")

    xb.mark_transcribed(target, "done", language="es", transcript="hola", db=db)

    assert xb.get_bookmark(target, db=db)["transcription_status"] == "done"
    assert xb.get_bookmark(other, db=db)["transcription_status"] == "none"


def test_mark_transcribed_stores_language_and_transcript(db):
    row_id = _insert(db, "https://x.com/a/status/1")

    xb.mark_transcribed(row_id, "done", language="en", transcript="hello", db=db)

    bookmark = xb.get_bookmark(row_id, db=db)
    assert bookmark["transcript_language"] == "en"
    assert bookmark["transcript_original"] == "hello"
    assert bookmark["transcribed_at"]


def test_mark_transcribed_records_a_failure_without_a_transcript(db):
    row_id = _insert(db, "https://x.com/a/status/1")

    assert xb.mark_transcribed(row_id, "failed", db=db) is True
    assert xb.get_bookmark(row_id, db=db)["transcription_status"] == "failed"


def test_mark_transcribed_reports_failure_for_an_unknown_id(db):
    assert xb.mark_transcribed(999999, "done", db=db) is False


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def test_delete_bookmark_removes_the_row(db):
    row_id = _insert(db, "https://x.com/a/status/1")

    assert xb.delete_bookmark(row_id, db=db) is True
    assert xb.get_bookmark(row_id, db=db) is None


def test_delete_bookmark_also_removes_the_downloaded_file(db, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    row_id = _insert(db, "https://x.com/a/status/1", local_file_path=str(clip))

    xb.delete_bookmark(row_id, db=db)

    assert not clip.exists()


def test_delete_bookmark_survives_an_already_missing_file(db, tmp_path):
    row_id = _insert(
        db, "https://x.com/a/status/1", local_file_path=str(tmp_path / "gone.mp4")
    )

    assert xb.delete_bookmark(row_id, db=db) is True


def test_delete_bookmark_reports_failure_for_an_unknown_id(db):
    assert xb.delete_bookmark(999999, db=db) is False
