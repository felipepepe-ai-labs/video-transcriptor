"""Route-level coverage for the /x/* bookmark endpoints.

Ported from the standalone x-bookmarks-curator's app.bookmarks.test.js and
app.download.test.js. Background tasks are driven synchronously by FastAPI's
TestClient, so the download worker's DB effects are observable here.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import jobs
import x_bookmarks as xb
import x_sync
from x_progress import registry as x_progress


@pytest.fixture(autouse=True)
def clean_progress():
    """The registry is process-global; don't leak in-flight jobs across tests."""
    yield
    for subscriber in list(x_progress._subscribers):
        x_progress.unsubscribe(subscriber)
    x_progress._active.clear()


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


# ---------------------------------------------------------------------------
# Live progress (SSE)
# ---------------------------------------------------------------------------

def _read_events(count=1):
    """Pull the first *count* events off the SSE generator, then close it.

    Driven directly rather than over HTTP: the stream is infinite by design, so
    a TestClient request would never complete.
    """

    async def run():
        response = await app_module.x_progress_stream()
        iterator = response.body_iterator
        chunks = []
        try:
            for _ in range(count):
                chunks.append(await iterator.__anext__())
        finally:
            await iterator.aclose()
        return response, chunks

    response, chunks = asyncio.run(run())
    events = [
        json.loads(chunk[len("data: "):])
        for chunk in chunks
        if chunk.startswith("data: ")
    ]
    return response, events


def test_progress_stream_is_an_event_stream():
    response, _ = _read_events()

    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"


def test_progress_stream_opens_with_a_snapshot():
    _, events = _read_events()

    assert events[0]["type"] == "snapshot"


def test_progress_snapshot_carries_work_already_in_flight():
    """A tab opened mid-download must still learn that one is running."""
    x_progress.publish(
        {"type": "progress", "job": "download", "bookmark_id": 7, "message": "Descargando…"}
    )

    _, events = _read_events()

    assert [job["bookmark_id"] for job in events[0]["jobs"]] == [7]


def test_progress_snapshot_omits_finished_work():
    x_progress.publish({"type": "progress", "job": "download", "bookmark_id": 7})
    x_progress.publish({"type": "done", "job": "download", "bookmark_id": 7})

    _, events = _read_events()

    assert events[0]["jobs"] == []


def test_progress_stream_forwards_a_live_event():
    """The snapshot is only the opener; later events must reach the client too."""

    async def run():
        response = await app_module.x_progress_stream()
        iterator = response.body_iterator
        try:
            await iterator.__anext__()  # the opening snapshot
            # Published only now: before subscribing it would land in the
            # snapshot instead of being delivered as a live event.
            x_progress.publish({"type": "progress", "job": "sync", "message": "Abriendo…"})
            return await iterator.__anext__()
        finally:
            await iterator.aclose()

    chunk = asyncio.run(run())

    assert json.loads(chunk[len("data: "):])["message"] == "Abriendo…"


def test_progress_stream_unsubscribes_when_the_client_goes_away():
    _read_events()

    assert x_progress._subscribers == []


def test_sync_reports_its_progress_while_it_scrapes(client, monkeypatch):
    """The scrape scrolls for up to a minute; the UI needs more than a start event."""
    def scrape(session_path, db=None, on_progress=None):
        on_progress(message="Cargando bookmarks… (12 encontrados)", found=12)
        return []

    monkeypatch.setattr(x_sync, "sync_x_bookmarks", scrape)
    (app_module.DATA_DIR / "session.json").write_text("{}")  # or the route 400s
    subscriber = x_progress.subscribe()

    client.post("/x/sync")

    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    scraping = [e for e in events if e.get("found") == 12]
    assert scraping and scraping[0]["job"] == "sync"


# ---------------------------------------------------------------------------
# Transcribing a bookmark through the shared pipeline
# ---------------------------------------------------------------------------
# X reuses run_transcription_job exactly like YouTube does: the only thing that
# differs between the three sources is how the file reaches UPLOAD_DIR.

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """Capture the call into the shared pipeline instead of running it."""
    calls = []
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(
        app_module, "run_transcription_job",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def _downloaded(tmp_path, name="clip.mp4"):
    clip = tmp_path / name
    clip.write_bytes(b"video bytes")
    row_id = _add(status="interesting")
    xb.mark_downloaded(row_id, str(clip))
    return row_id, clip


def test_transcribing_creates_a_job_for_the_bookmark(client, pipeline, tmp_path):
    row_id, _ = _downloaded(tmp_path)

    response = client.post(f"/x/bookmarks/{row_id}/transcribe")

    assert response.status_code == 200
    job = jobs.get_job(response.json()["job_id"])
    assert job["source"] == "x"
    assert job["url"] == "https://x.com/a/status/1"


def test_the_video_reaches_the_pipeline_without_copying_it(client, pipeline, tmp_path):
    """A measured X video weighed 681 MB; linking beats duplicating it."""
    row_id, clip = _downloaded(tmp_path)

    job_id = client.post(f"/x/bookmarks/{row_id}/transcribe").json()["job_id"]

    linked = app_module.UPLOAD_DIR / f"{job_id}.mp4"
    assert linked.exists()
    assert linked.stat().st_ino == clip.stat().st_ino  # same bytes on disk


def test_the_download_survives_being_handed_to_the_pipeline(client, pipeline, tmp_path, monkeypatch):
    """On CIFS os.link moves the file instead of linking it, which silently ate
    the bookmark's download and left it marked 'downloaded' with nothing there."""
    def link_that_moves(source, destination):
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()

    monkeypatch.setattr(app_module.os, "link", link_that_moves)
    row_id, clip = _downloaded(tmp_path)

    client.post(f"/x/bookmarks/{row_id}/transcribe")

    assert clip.exists(), "the bookmark's own copy must still be there"


def test_the_job_is_recorded_on_the_bookmark(client, pipeline, tmp_path):
    """So the card can offer "see transcription" later."""
    row_id, _ = _downloaded(tmp_path)

    job_id = client.post(f"/x/bookmarks/{row_id}/transcribe").json()["job_id"]

    assert xb.get_bookmark(row_id)["job_id"] == job_id


def test_the_language_is_auto_detected(client, pipeline, tmp_path):
    """A tweet can be in any language; forcing English would mistranscribe it."""
    row_id, _ = _downloaded(tmp_path)

    client.post(f"/x/bookmarks/{row_id}/transcribe")

    _, kwargs = pipeline[0]
    assert kwargs["source_language"] is None


# The three sources share one pipeline, so they must also share the knobs that
# drive it: picking a voice and supplying chapters cannot be a privilege of the
# upload and YouTube tabs.

def test_the_chosen_voice_reaches_the_pipeline(client, pipeline, tmp_path):
    row_id, _ = _downloaded(tmp_path)

    client.post(f"/x/bookmarks/{row_id}/transcribe", data={"voice": "female"})

    args, _ = pipeline[0]
    assert args[4] == "female"


def test_the_voice_defaults_to_male(client, pipeline, tmp_path):
    row_id, _ = _downloaded(tmp_path)

    client.post(f"/x/bookmarks/{row_id}/transcribe")

    args, _ = pipeline[0]
    assert args[4] == "male"


def test_an_unknown_voice_is_refused(client, pipeline, tmp_path):
    """Same 400 the other two sources give, rather than a Piper failure later."""
    row_id, _ = _downloaded(tmp_path)

    response = client.post(f"/x/bookmarks/{row_id}/transcribe", data={"voice": "robot"})

    assert response.status_code == 400
    assert not pipeline


def test_chapters_reach_the_pipeline(client, pipeline, tmp_path):
    row_id, _ = _downloaded(tmp_path)

    client.post(
        f"/x/bookmarks/{row_id}/transcribe",
        data={"chapters_json": '[{"time": 0, "title": "Intro"}]'},
    )

    args, _ = pipeline[0]
    assert args[3] == [{"time": 0, "title": "Intro"}]


def test_malformed_chapters_json_is_refused(client, pipeline, tmp_path):
    row_id, _ = _downloaded(tmp_path)

    response = client.post(
        f"/x/bookmarks/{row_id}/transcribe", data={"chapters_json": "not json"}
    )

    assert response.status_code == 400
    assert not pipeline


def test_the_detected_language_is_recorded_on_the_bookmark(client, monkeypatch, tmp_path):
    """The pipeline stores the key as 'source_language'; reading 'language'
    silently wrote None for every bookmark ever transcribed."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", uploads)

    def finish(job_id, *args, **kwargs):
        jobs.update_job(
            job_id, status="done", stage="done",
            result={"source_language": "en", "segments": []},
        )

    monkeypatch.setattr(app_module, "run_transcription_job", finish)
    row_id, _ = _downloaded(tmp_path)

    client.post(f"/x/bookmarks/{row_id}/transcribe")

    assert xb.get_bookmark(row_id)["transcript_language"] == "en"


def test_transcribing_an_undownloaded_bookmark_is_refused(client, pipeline):
    row_id = _add(status="interesting")

    assert client.post(f"/x/bookmarks/{row_id}/transcribe").status_code == 409


def test_transcribing_an_unknown_bookmark_404s(client, pipeline):
    assert client.post("/x/bookmarks/999999/transcribe").status_code == 404


def test_download_publishes_a_terminal_event(client, monkeypatch, tmp_path):
    """The UI refreshes off this event, so it must fire on the no-video path too."""
    def no_video(*args, **kwargs):
        raise x_sync.NoMediaFound("no video")

    monkeypatch.setattr(app_module, "_download_video", no_video)
    row_id = _add(status="interesting")
    subscriber = x_progress.subscribe()

    client.post(f"/x/bookmarks/{row_id}/download")

    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    terminal = [e for e in events if e["type"] in ("done", "error")]
    assert terminal and terminal[-1]["status"] == "no_media"
