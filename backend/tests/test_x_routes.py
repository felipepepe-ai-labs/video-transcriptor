"""Route-level coverage for the /x/* bookmark endpoints.

Ported from the standalone x-bookmarks-curator's app.bookmarks.test.js and
app.download.test.js. Background tasks are driven synchronously by FastAPI's
TestClient, so the download worker's DB effects are observable here.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import x_bookmarks as xb
import x_sync


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client backed by a throwaway bookmarks DB and cookie jar."""
    db_path = tmp_path / "bookmarks.db"
    monkeypatch.setattr(xb, "DB_PATH", db_path)
    conn = xb._get_conn()
    conn.execute(xb.SCHEMA)
    conn.commit()
    conn.close()

    data_dir = tmp_path / "x-data"
    data_dir.mkdir()
    (data_dir / "cookies.txt").write_text("# jar")
    monkeypatch.setattr(app_module, "DATA_DIR", data_dir)

    return TestClient(app_module.app)


def _add(url="https://x.com/a/status/1", status="new"):
    conn = xb._get_conn()
    cur = conn.execute(
        "INSERT INTO bookmarks (tweet_url, status) VALUES (?, ?)", (url, status)
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


# ---------------------------------------------------------------------------
# Listing and review
# ---------------------------------------------------------------------------

def test_list_returns_the_stored_bookmarks(client):
    _add()

    response = client.get("/x/bookmarks")

    assert response.status_code == 200
    assert [b["tweet_url"] for b in response.json()] == ["https://x.com/a/status/1"]


def test_list_filters_by_status(client):
    _add("https://x.com/a/status/1", status="new")
    _add("https://x.com/b/status/2", status="interesting")

    response = client.get("/x/bookmarks", params={"status": "interesting"})

    assert [b["tweet_url"] for b in response.json()] == ["https://x.com/b/status/2"]


def test_toggle_marks_a_bookmark_interesting(client):
    row_id = _add(status="new")

    response = client.patch(f"/x/bookmarks/{row_id}/interesting")

    assert response.status_code == 200
    assert response.json()["status"] == "interesting"


def test_toggle_sends_a_no_media_bookmark_back_to_new(client):
    row_id = _add(status="no_media")

    response = client.patch(f"/x/bookmarks/{row_id}/interesting")

    assert response.status_code == 200
    assert response.json()["status"] == "new"


def test_toggle_rejects_a_downloaded_bookmark(client):
    row_id = _add(status="downloaded")

    assert client.patch(f"/x/bookmarks/{row_id}/interesting").status_code == 409


def test_toggle_404s_for_an_unknown_bookmark(client):
    assert client.patch("/x/bookmarks/999999/interesting").status_code == 404


def test_delete_removes_a_bookmark(client):
    row_id = _add()

    assert client.delete(f"/x/bookmarks/{row_id}").status_code == 200
    assert client.get("/x/bookmarks").json() == []


def test_delete_404s_for_an_unknown_bookmark(client):
    assert client.delete("/x/bookmarks/999999").status_code == 404


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def test_download_persists_the_file_path(client, monkeypatch, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    monkeypatch.setattr(app_module, "_download_video", lambda *a, **k: str(clip))
    row_id = _add(status="interesting")

    response = client.post(f"/x/bookmarks/{row_id}/download")

    assert response.status_code == 200
    bookmark = xb.get_bookmark(row_id)
    assert bookmark["status"] == "downloaded"
    assert bookmark["local_file_path"] == str(clip)


def test_download_settles_a_video_less_tweet_in_no_media(client, monkeypatch):
    def no_video(*args, **kwargs):
        raise x_sync.NoMediaFound("no video")

    monkeypatch.setattr(app_module, "_download_video", no_video)
    row_id = _add(status="interesting")

    client.post(f"/x/bookmarks/{row_id}/download")

    assert xb.get_bookmark(row_id)["status"] == "no_media"


def test_download_leaves_the_status_alone_on_a_real_failure(client, monkeypatch):
    """A failure is retryable, so the bookmark must stay 'interesting'."""

    def boom(*args, **kwargs):
        raise x_sync.DownloadFailed("HTTP 401")

    monkeypatch.setattr(app_module, "_download_video", boom)
    row_id = _add(status="interesting")

    client.post(f"/x/bookmarks/{row_id}/download")

    assert xb.get_bookmark(row_id)["status"] == "interesting"


def test_download_can_be_retried_on_a_no_media_bookmark(client, monkeypatch, tmp_path):
    """The UI's Retry Download button: no_media is not a dead end."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    monkeypatch.setattr(app_module, "_download_video", lambda *a, **k: str(clip))
    row_id = _add(status="no_media")

    response = client.post(f"/x/bookmarks/{row_id}/download")

    assert response.status_code == 200
    assert xb.get_bookmark(row_id)["status"] == "downloaded"


def test_download_rejects_a_bookmark_that_was_never_reviewed(client):
    row_id = _add(status="new")

    assert client.post(f"/x/bookmarks/{row_id}/download").status_code == 409


def test_download_404s_for_an_unknown_bookmark(client):
    assert client.post("/x/bookmarks/999999/download").status_code == 404
