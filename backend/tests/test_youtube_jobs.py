"""
Tests for the YouTube ingestion path: POST /jobs/youtube downloads the video
with yt-dlp (mocked here at the youtube.download_video boundary) and then
drives the exact same transcription pipeline as an uploaded file.

Key behavioral contract:
- An English video runs the full pipeline (translate + narrate + dub).
- A Spanish video keeps its transcript as-is: no translation call, no Piper
  narration, no dubbing -- re-voicing Spanish over Spanish is pointless.
- Download failures fail the job with a clear error; they never crash the app.
- source/title/url are persisted on the job row so the history view can
  distinguish YouTube jobs from uploads.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import jobs as jobs_module
import youtube as youtube_module
from tests.test_jobs_e2e import SRT_SAMPLE, FakeSSHClient, install_fake_remote

SRT_SPANISH = """1
00:00:00,000 --> 00:00:02,000
Hola a todos

2
00:00:02,500 --> 00:00:04,000
Esto ya es español
"""

YT_URL = "https://www.youtube.com/watch?v=abc123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(app_module, "VIDEO_DIR", tmp_path / "video")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "audio").mkdir()
    (tmp_path / "video").mkdir()
    return TestClient(app_module.app)


def install_fake_download(monkeypatch, meta=None, error=None):
    """Replace the yt-dlp boundary: writes a placeholder file and returns
    video metadata, or raises DownloadFailed like a dead/blocked URL would."""
    calls = []

    def fake_download(url, dest_path):
        calls.append(url)
        if error is not None:
            raise error
        dest_path.write_bytes(b"fake video bytes")
        result = {"title": "Mi Video", "duration": 4.0, "chapters": []}
        result.update(meta or {})
        return result

    monkeypatch.setattr(app_module.youtube, "download_video", fake_download)
    return calls


def mock_mymemory_ok(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"responseData": {"translatedText": "Hola mundo"}},
    ))


def submit_youtube(client, url=YT_URL, **data):
    return client.post("/jobs/youtube", data={"url": url, **data})


def test_youtube_english_video_runs_full_pipeline(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}], "language": "en"})
    install_fake_remote(monkeypatch, fake)
    mock_mymemory_ok(monkeypatch)
    calls = install_fake_download(monkeypatch)

    resp = submit_youtube(client)
    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["status"] == "queued"
    assert calls == [YT_URL]

    job = client.get(f"/jobs/{body['job_id']}").json()
    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert result["source"] == "youtube"
    assert result["url"] == YT_URL
    assert result["source_language"] == "en"
    assert result["segments"][0]["text_en"] == "Hello world"
    assert result["segments"][0]["text_es"] == "Hola mundo"
    assert result["translation_provider"] == "mymemory"
    # Narration must still run for an English source video.
    assert result["audio_available"] is True
    assert len(fake.piper_commands) == 1


def test_youtube_spanish_video_skips_translation_and_narration(client, monkeypatch):
    fake = FakeSSHClient(
        transcribe_srt=SRT_SPANISH,
        json_content={"segments": [{"end": 4.0}], "language": "es"},
    )
    install_fake_remote(monkeypatch, fake)
    install_fake_download(monkeypatch)

    def no_translation(*a, **k):
        raise AssertionError("translate_with_fallback must not be called for a Spanish source")

    monkeypatch.setattr(app_module, "translate_with_fallback", no_translation)

    resp = submit_youtube(client)
    job = client.get(f"/jobs/{resp.json()['job_id']}").json()

    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert result["source_language"] == "es"
    assert result["translation_provider"] == "source-es"
    assert result["segments"][0]["text_es"] == "Hola a todos"
    assert result["segments"][0]["text_es"] == result["segments"][0]["text_en"]
    # No Piper narration and no dubbed video for an already-Spanish video --
    # and their absence is by design, not an error.
    assert fake.piper_commands == []
    assert result["audio_available"] is False
    assert "audio_error" not in result
    assert result["dubbed_video_available"] is False
    assert "dubbed_video_error" not in result


def test_youtube_download_failure_fails_job(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]})
    install_fake_remote(monkeypatch, fake)
    install_fake_download(
        monkeypatch, error=youtube_module.DownloadFailed("Video unavailable")
    )

    resp = submit_youtube(client)
    job = client.get(f"/jobs/{resp.json()['job_id']}").json()

    assert job["status"] == "failed"
    assert "download" in job["error"].lower()
    assert "Video unavailable" in job["error"]
    # Nothing was ever sent to the remote whisper host.
    assert fake.upload_count == 0


def test_youtube_job_adopts_video_chapters_when_none_provided(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}], "language": "en"})
    install_fake_remote(monkeypatch, fake)
    mock_mymemory_ok(monkeypatch)
    install_fake_download(monkeypatch, meta={
        "chapters": [{"time": 0, "title": "Intro"}, {"time": 2.5, "title": "Parte 2"}],
    })

    resp = submit_youtube(client)
    job = client.get(f"/jobs/{resp.json()['job_id']}").json()

    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert [c["title"] for c in result["chapters"]] == ["Intro", "Parte 2"]
    assert result["segments"][0]["chapter"] == "Intro"
    assert result["segments"][1]["chapter"] == "Parte 2"


def test_youtube_job_explicit_chapters_win_over_video_chapters(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}], "language": "en"})
    install_fake_remote(monkeypatch, fake)
    mock_mymemory_ok(monkeypatch)
    install_fake_download(monkeypatch, meta={
        "chapters": [{"time": 0, "title": "Del Video"}],
    })

    resp = submit_youtube(
        client, chapters_json=json.dumps([{"time": 0, "title": "Del Usuario"}])
    )
    job = client.get(f"/jobs/{resp.json()['job_id']}").json()

    assert [c["title"] for c in job["result"]["chapters"]] == ["Del Usuario"]


def test_youtube_url_must_be_http(client):
    resp = submit_youtube(client, url="ftp://example.com/video")
    assert resp.status_code == 400


def test_youtube_invalid_voice_rejected(client):
    resp = submit_youtube(client, voice="robot")
    assert resp.status_code == 400


def test_history_distinguishes_youtube_jobs_from_uploads(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}], "language": "en"})
    install_fake_remote(monkeypatch, fake)
    mock_mymemory_ok(monkeypatch)
    install_fake_download(monkeypatch)

    upload_resp = client.post(
        "/jobs",
        files={"video": ("clip.mp4", b"fake video bytes", "video/mp4")},
    )
    yt_resp = submit_youtube(client)

    listed = {j["id"]: j for j in client.get("/jobs").json()}
    upload_row = listed[upload_resp.json()["job_id"]]
    yt_row = listed[yt_resp.json()["job_id"]]

    assert upload_row["source"] == "upload"
    assert upload_row["title"] is None
    assert yt_row["source"] == "youtube"
    assert yt_row["title"] == "Mi Video"
    assert yt_row["url"] == YT_URL


def test_upload_pipeline_still_assumes_english(client, monkeypatch):
    """Regression guard: the upload path keeps forcing --language en (the
    app's contract is EN uploads), so whisper detection changes must not
    alter its behavior."""
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}], "language": "en"})
    install_fake_remote(monkeypatch, fake)
    mock_mymemory_ok(monkeypatch)

    resp = client.post(
        "/jobs",
        files={"video": ("clip.mp4", b"fake video bytes", "video/mp4")},
    )
    job = client.get(f"/jobs/{resp.json()['job_id']}").json()

    assert job["status"] == "done"
    result = job["result"]
    assert result["source"] == "upload"
    assert result["source_language"] == "en"
    assert result["translation_provider"] == "mymemory"
