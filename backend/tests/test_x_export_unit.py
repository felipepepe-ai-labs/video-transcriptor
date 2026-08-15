"""Unit coverage for the plain-file backup of the bookmarks store.

The point of this module is that the backup survives the application: a JSON
dump and one Markdown file per bookmark, readable with nothing but a text
editor. So these tests assert on the files themselves, never on a return value
alone.
"""

import json
import sqlite3

import pytest

import x_bookmarks as xb
import x_export


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "bookmarks.db"
    monkeypatch.setattr(xb, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(xb.SCHEMA)
    conn.commit()
    yield conn
    conn.close()


def _insert(conn, url="https://x.com/midudev/status/2076688198101516482", **fields):
    cols = ["tweet_url", *fields]
    values = [url, *fields.values()]
    conn.execute(
        f"INSERT INTO bookmarks ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        values,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The JSON dump
# ---------------------------------------------------------------------------

def test_the_dump_carries_every_column(db, tmp_path):
    _insert(db, author="midu", text="short", expanded_text="the whole thing",
            status="interesting", article_content="<p>article</p>")

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    payload = json.loads((tmp_path / "backup" / "bookmarks.json").read_text())
    [bookmark] = payload["bookmarks"]
    assert bookmark["expanded_text"] == "the whole thing"
    assert bookmark["article_content"] == "<p>article</p>"
    assert bookmark["status"] == "interesting"


def test_the_dump_records_when_it_was_taken(db, tmp_path):
    _insert(db)

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    payload = json.loads((tmp_path / "backup" / "bookmarks.json").read_text())
    assert payload["count"] == 1
    assert payload["exported_at"]


def test_no_half_written_file_is_left_behind(db, tmp_path):
    """The dump is written aside and renamed, so a crash cannot corrupt it."""
    _insert(db)

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    assert list((tmp_path / "backup").glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# The Markdown files
# ---------------------------------------------------------------------------

def test_one_markdown_per_bookmark(db, tmp_path):
    _insert(db, "https://x.com/a/status/1")
    _insert(db, "https://x.com/b/status/2")

    result = x_export.export_bookmarks(tmp_path / "backup", db=db)

    assert result["markdown"] == 2
    assert len(list((tmp_path / "backup" / "md").glob("*.md"))) == 2


def test_the_markdown_carries_the_untruncated_text(db, tmp_path):
    _insert(db, text="the timeline's 280 chars", expanded_text="the whole 2341")

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    [written] = (tmp_path / "backup" / "md").glob("*.md")
    assert "the whole 2341" in written.read_text()


def test_the_markdown_falls_back_to_the_timeline_text(db, tmp_path):
    """Two bookmarks failed to expand; they must still be backed up."""
    _insert(db, text="all there is", expanded_text=None)

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    [written] = (tmp_path / "backup" / "md").glob("*.md")
    assert "all there is" in written.read_text()


def test_the_front_matter_names_the_source(db, tmp_path):
    _insert(db, "https://x.com/midudev/status/2076688198101516482", author="Miguel")

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    [written] = (tmp_path / "backup" / "md").glob("*.md")
    body = written.read_text()
    assert body.startswith("---\n")
    assert "https://x.com/midudev/status/2076688198101516482" in body
    assert "midudev" in body


def test_the_filename_survives_an_author_full_of_emoji(db, tmp_path):
    _insert(db, "https://x.com/星野/status/42", author="🔥 Ünïcødé 🔥")

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    [written] = (tmp_path / "backup" / "md").glob("*.md")
    assert written.name.isascii()
    assert "42" in written.name


def test_a_bookmark_without_a_usable_permalink_is_still_exported(db, tmp_path):
    _insert(db, "not-a-status-url")

    result = x_export.export_bookmarks(tmp_path / "backup", db=db)

    assert result["markdown"] == 1


def test_two_bookmarks_from_the_same_author_do_not_collide(db, tmp_path):
    _insert(db, "https://x.com/midudev/status/1", scraped_at="2026-08-12T10:00:00Z")
    _insert(db, "https://x.com/midudev/status/2", scraped_at="2026-08-12T10:00:00Z")

    x_export.export_bookmarks(tmp_path / "backup", db=db)

    assert len(list((tmp_path / "backup" / "md").glob("*.md"))) == 2


# ---------------------------------------------------------------------------
# Re-running it
# ---------------------------------------------------------------------------

def test_exporting_twice_refreshes_rather_than_duplicates(db, tmp_path):
    _insert(db, text="first")

    x_export.export_bookmarks(tmp_path / "backup", db=db)
    db.execute("UPDATE bookmarks SET expanded_text = 'second'")
    db.commit()
    x_export.export_bookmarks(tmp_path / "backup", db=db)

    [written] = (tmp_path / "backup" / "md").glob("*.md")
    assert "second" in written.read_text()


def test_a_bookmark_deleted_since_the_last_export_leaves_no_orphan(db, tmp_path):
    """Otherwise the backup slowly fills with files for bookmarks that are gone."""
    _insert(db, "https://x.com/a/status/1")
    _insert(db, "https://x.com/b/status/2")
    x_export.export_bookmarks(tmp_path / "backup", db=db)

    db.execute("DELETE FROM bookmarks WHERE tweet_url = 'https://x.com/b/status/2'")
    db.commit()
    x_export.export_bookmarks(tmp_path / "backup", db=db)

    assert len(list((tmp_path / "backup" / "md").glob("*.md"))) == 1


def test_an_empty_store_still_produces_a_dump(db, tmp_path):
    result = x_export.export_bookmarks(tmp_path / "backup", db=db)

    assert result["markdown"] == 0
    assert (tmp_path / "backup" / "bookmarks.json").exists()
