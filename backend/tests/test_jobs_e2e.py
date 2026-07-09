"""
End-to-end tests for the async POST /jobs + GET /jobs/{id} contract.

Drives the real FastAPI app + job worker + SRT parsing + chapter assignment
+ translation orchestration through TestClient. Only the SSH/paramiko
boundary to the remote Whisper host is mocked, following the same
FakeSSHClient/FakeSFTP/FakeStream pattern used in test_transcribe_e2e.py for
the old blocking endpoint. Extended here to also fake `df` (disk pre-flight)
and cleanup instrumentation to prove the leak-on-failure bug is fixed.

Starlette's TestClient runs BackgroundTasks synchronously as part of the
request/response cycle, so by the time client.post("/jobs") returns, the
worker has already finished -- no polling loop needed for these tests.
"""
import io
import json
import re
import subprocess
import wave

import httpx
import paramiko
import pytest
from fastapi.testclient import TestClient

import app as app_module
import jobs as jobs_module
import remote as remote_module

SRT_SAMPLE = """1
00:00:00,000 --> 00:00:02,000
Hello world

2
00:00:02,500 --> 00:00:04,000
This is a test
"""


def make_fake_wav(duration: float = 0.3) -> bytes:
    """A minimal valid mono 16-bit WAV -- real bytes (not a stub), since
    dub.py parses duration via the stdlib `wave` module and ffmpeg concats
    these for real in tests (no ffmpeg mocking -- it's fast and local)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * int(22050 * duration))
    return buf.getvalue()


class FakeChannel:
    def __init__(self, exit_status):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class FakeStream:
    def __init__(self, data: bytes, exit_status=0):
        self._data = data
        self.channel = FakeChannel(exit_status)

    def read(self):
        return self._data


class FakeSFTPAttr:
    def __init__(self, filename):
        self.filename = filename


class FakeSFTPFile:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


class FakeSFTP:
    """srt_filename listing covers whisper's single-file readback;
    piper_out_dirs covers Piper's one-wav-per-stdin-line batch output."""

    def __init__(self, listdir_result, piper_out_dirs, on_put=None):
        self._listdir_result = listdir_result
        self._piper_out_dirs = piper_out_dirs
        self._on_put = on_put

    def put(self, local, remote):
        if self._on_put:
            self._on_put(local, remote)

    def listdir_attr(self, path):
        if path in self._piper_out_dirs:
            return [FakeSFTPAttr(fn) for fn in sorted(self._piper_out_dirs[path])]
        return self._listdir_result

    def open(self, path, mode="r"):
        out_dir, _, filename = path.rpartition("/")
        return FakeSFTPFile(self._piper_out_dirs[out_dir][filename])

    def close(self):
        pass


class FakeStdin:
    """Piper reads text from stdin, one line per output file. run_tts_batch()
    writes all lines then shuts down; on_shutdown synthesizes that many fake
    WAVs into the fake remote output directory, mirroring what real Piper
    would produce."""

    def __init__(self, on_shutdown):
        self.written = b""
        self.channel = self
        self._on_shutdown = on_shutdown

    def write(self, data):
        self.written += data.encode() if isinstance(data, str) else data

    def shutdown_write(self):
        self._on_shutdown(self.written.decode())


class FakeSSHClient:
    """Simulates the remote Whisper+Piper host: disk check, SFTP upload,
    `whisper` CLI (--task transcribe), `piper` CLI (batch TTS), SRT/JSON/
    audio readback, and cleanup (`rm`) command instrumentation."""

    def __init__(
        self,
        transcribe_exit_code=0,
        transcribe_srt=SRT_SAMPLE,
        json_content=None,
        srt_filename="out.srt",
        avail_kb=10_000_000,  # ~10 GB free by default
        piper_exit_code=0,
    ):
        self.transcribe_exit_code = transcribe_exit_code
        self.transcribe_srt = transcribe_srt
        self.json_content = json_content
        self.srt_filename = srt_filename
        self.avail_kb = avail_kb
        self.piper_exit_code = piper_exit_code
        self.rm_commands = []
        self.piper_commands = []
        self.piper_out_dirs = {}  # out_dir path -> {filename: wav_bytes}
        self.upload_count = 0

    def open_sftp(self):
        listing = [FakeSFTPAttr(self.srt_filename)] if self.srt_filename else []
        return FakeSFTP(listing, self.piper_out_dirs, on_put=self._record_upload)

    def _record_upload(self, local, remote):
        self.upload_count += 1

    def exec_command(self, cmd):
        if "df --output=avail" in cmd:
            return None, FakeStream(f"Avail\n{self.avail_kb}\n".encode()), None
        if "python3 -m whisper" in cmd:
            exit_code = self.transcribe_exit_code
            err = b"" if exit_code == 0 else b"whisper: CUDA error"
            return None, FakeStream(b"", exit_code), FakeStream(err, exit_code)
        if "piper --model" in cmd:
            self.piper_commands.append(cmd)
            out_dir = re.search(r"-d (\S+)", cmd).group(1)
            exit_code = self.piper_exit_code

            def on_shutdown(stdin_text, out_dir=out_dir):
                lines = [ln for ln in stdin_text.splitlines() if ln]
                self.piper_out_dirs[out_dir] = {
                    f"{i:015d}.wav": make_fake_wav() for i in range(len(lines))
                }

            err = b"" if exit_code == 0 else b"piper: model load error"
            return FakeStdin(on_shutdown), FakeStream(b"", exit_code), FakeStream(err, exit_code)
        if cmd.startswith("cat") and cmd.rstrip().endswith(".srt"):
            return None, FakeStream(self.transcribe_srt.encode()), None
        if cmd.startswith("cat") and cmd.rstrip().endswith(".json"):
            if self.json_content is None:
                return None, FakeStream(b"not valid json"), None
            return None, FakeStream(json.dumps(self.json_content).encode()), None
        if cmd.startswith("rm") or "; rm" in cmd:
            self.rm_commands.append(cmd)
            return None, FakeStream(b""), None
        raise AssertionError(f"unexpected remote command: {cmd!r}")

    def close(self):
        pass


def install_fake_remote(monkeypatch, fake_client: FakeSSHClient):
    """Bypass real paramiko: RemoteWhisper.connect() just attaches the fake."""

    def fake_connect(self):
        self._ssh = fake_client

    monkeypatch.setattr(remote_module.RemoteWhisper, "connect", fake_connect)


class FakeConnectingSSHClient:
    """Simulates paramiko.SSHClient() at the point of .connect() failing, so
    RemoteWhisper.connect()'s own try/except mapping is exercised for real."""

    def __init__(self, error_factory):
        self._error_factory = error_factory

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        raise self._error_factory()


def install_failing_paramiko(monkeypatch, error_factory):
    monkeypatch.setattr(
        remote_module.paramiko, "SSHClient", lambda: FakeConnectingSSHClient(error_factory)
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(app_module, "VIDEO_DIR", tmp_path / "video")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "audio").mkdir()
    (tmp_path / "video").mkdir()
    return TestClient(app_module.app)


def submit(client, chapters=None):
    data = {"chapters_json": json.dumps(chapters)} if chapters is not None else {}
    return client.post(
        "/jobs",
        files={"video": ("clip.mp4", b"fake video bytes", "video/mp4")},
        data=data,
    )


def test_post_jobs_returns_job_id_immediately(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]})
    install_fake_remote(monkeypatch, fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"responseData": {"translatedText": "Hola mundo"}},
    ))

    resp = submit(client)

    assert resp.status_code in (200, 201)
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "queued"


def test_job_transitions_to_done_with_full_result(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]})
    install_fake_remote(monkeypatch, fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"responseData": {"translatedText": "Hola mundo"}},
    ))

    resp = submit(client, chapters=[{"time": 0, "title": "Intro"}])
    job_id = resp.json()["job_id"]

    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "done"
    assert job["stage"] == "done"
    result = job["result"]
    assert result["duration_seconds"] == 4.0
    assert len(result["segments"]) == 2
    assert result["segments"][0]["text_en"] == "Hello world"
    assert result["segments"][0]["text_es"] == "Hola mundo"
    assert result["segments"][0]["chapter"] == "Intro"
    assert result["chapters"][0]["title"] == "Intro"
    assert result["translation_provider"] == "mymemory"
    assert result["voice"] == "male"
    assert result["audio_available"] is True
    assert len(fake.piper_commands) == 1
    assert "es_ES-davefx-medium" in fake.piper_commands[0]
    assert "--speaker" not in fake.piper_commands[0]
    # cleanup must have run on the success path too
    assert any("rm -f" in c for c in fake.rm_commands)


def test_female_voice_uses_sharvard_speaker_1(client, monkeypatch):
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]})
    install_fake_remote(monkeypatch, fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"responseData": {"translatedText": "Hola mundo"}},
    ))

    resp = client.post(
        "/jobs",
        files={"video": ("clip.mp4", b"fake video bytes", "video/mp4")},
        data={"voice": "female"},
    )
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["result"]["voice"] == "female"
    assert "es_ES-sharvard-medium" in fake.piper_commands[0]
    assert "--speaker 1" in fake.piper_commands[0]


def test_invalid_voice_rejected(client):
    resp = client.post(
        "/jobs",
        files={"video": ("clip.mp4", b"fake video bytes", "video/mp4")},
        data={"voice": "robot"},
    )
    assert resp.status_code == 400


def test_tts_failure_does_not_fail_the_job(client, monkeypatch):
    """Narration is a bonus on top of transcript+translation -- a broken
    Piper invocation must degrade gracefully, not sink the whole job."""
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]}, piper_exit_code=1)
    install_fake_remote(monkeypatch, fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"responseData": {"translatedText": "Hola mundo"}},
    ))

    resp = submit(client)
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "done"
    assert job["result"]["audio_available"] is False
    assert "audio_error" in job["result"]


def test_unknown_job_id_returns_404(client):
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404


def test_low_disk_space_fails_without_uploading(client, monkeypatch):
    fake = FakeSSHClient(avail_kb=0)  # no free space at all
    install_fake_remote(monkeypatch, fake)

    resp = submit(client)
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "failed"
    assert "507" in job["error"] or "storage" in job["error"].lower()
    assert fake.upload_count == 0


@pytest.mark.parametrize("error_factory", [
    lambda: paramiko.SSHException("Authentication failed"),
    lambda: paramiko.ssh_exception.NoValidConnectionsError(
        {("192.168.1.60", 22): "Connection refused"}
    ),
])
def test_ssh_connect_failures_map_consistently(client, monkeypatch, error_factory):
    """
    Fixes the inconsistency the old test_transcribe_e2e.py documented:
    paramiko.SSHException and paramiko.ssh_exception.NoValidConnectionsError
    (an OSError subclass, not an SSHException subclass) must now both be
    classified the same way (RemoteUnavailable -> 502-flavored job error),
    instead of one falling through to a generic 500.
    """
    install_failing_paramiko(monkeypatch, error_factory)

    resp = submit(client)
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "failed"
    assert "502" in job["error"]
    assert "Remote unavailable" in job["error"]


def test_both_ssh_failure_modes_produce_equivalent_error_shape(client, monkeypatch):
    """Directly proves the two error paths are now consistent with each other."""
    install_failing_paramiko(monkeypatch, lambda: paramiko.SSHException("boom"))
    resp1 = submit(client)
    job1 = client.get(f"/jobs/{resp1.json()['job_id']}").json()

    install_failing_paramiko(monkeypatch, lambda: paramiko.ssh_exception.NoValidConnectionsError(
        {("192.168.1.60", 22): "Connection refused"}
    ))
    resp2 = submit(client)
    job2 = client.get(f"/jobs/{resp2.json()['job_id']}").json()

    assert job1["status"] == job2["status"] == "failed"
    assert job1["error"].split(":")[0] == job2["error"].split(":")[0]  # same "502 Remote unavailable" prefix


def test_whisper_nonzero_exit_fails_job_and_still_cleans_up(client, monkeypatch):
    fake = FakeSSHClient(transcribe_exit_code=1)
    install_fake_remote(monkeypatch, fake)

    resp = submit(client)
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "failed"
    assert "Whisper failed" in job["error"]
    # This is the core leak fix: cleanup must run even on the failure path.
    assert len(fake.rm_commands) > 0
    assert any(c.startswith("rm -f") for c in fake.rm_commands)


@pytest.fixture
def real_video_bytes(tmp_path):
    """A real playable 5s mp4 (matches SRT_SAMPLE's 0-4s segments) -- needed
    because dub.py shells out to real ffmpeg (not mocked, it's local and
    fast), which will reject the placeholder `b"fake video bytes"` used by
    every other test."""
    path = tmp_path / "real.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:v", "libx264", "-c:a", "aac", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def test_full_pipeline_dubs_video_and_splits_by_chapter(client, monkeypatch, real_video_bytes):
    """The real feature ask: translated audio muxed onto the video, and the
    result cut into one clip per chapter. Exercises dub.py for real (real
    ffmpeg) end to end -- only the SSH/Piper boundary is faked."""
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]})
    install_fake_remote(monkeypatch, fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"responseData": {"translatedText": "Hola mundo"}},
    ))

    resp = client.post(
        "/jobs",
        files={"video": ("clip.mp4", real_video_bytes, "video/mp4")},
        data={"chapters_json": json.dumps([{"time": 0, "title": "Intro"}, {"time": 2.5, "title": "Parte 2"}])},
    )
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert result["dubbed_video_available"] is True, result.get("dubbed_video_error")
    assert result["chapter_clips_available"] is True, result.get("chapter_clips_error")

    video_resp = client.get(f"/jobs/{job_id}/video")
    assert video_resp.status_code == 200
    assert video_resp.headers["content-type"] == "video/mp4"
    assert len(video_resp.content) > 1000

    ch0_resp = client.get(f"/jobs/{job_id}/chapters/0/video")
    ch1_resp = client.get(f"/jobs/{job_id}/chapters/1/video")
    assert ch0_resp.status_code == 200
    assert ch1_resp.status_code == 200
    assert ch0_resp.content != ch1_resp.content

    missing_resp = client.get(f"/jobs/{job_id}/chapters/5/video")
    assert missing_resp.status_code == 404


def test_mymemory_failure_falls_back_to_ollama(client, monkeypatch):
    """MyMemory down -> fall back to the local Ollama model. Whisper's own
    --task translate is not usable here: it only translates speech INTO
    English, never into an arbitrary target language like Spanish."""
    fake = FakeSSHClient(json_content={"segments": [{"end": 4.0}]})
    install_fake_remote(monkeypatch, fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))

    ollama_responses = {"Hello world": "Hola mundo", "This is a test": "Esto es una prueba"}

    def fake_post(url, json, **kwargs):
        prompt = json["prompt"]
        numbered_lines = re.findall(r"^\d+\.\s*(.+)$", prompt, re.MULTILINE)
        if numbered_lines:
            translated = [ollama_responses.get(t, t) for t in numbered_lines]
            response_text = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(translated))
        else:
            text = prompt.rsplit(":\n", 1)[-1].strip()
            response_text = ollama_responses.get(text, text)
        return httpx.Response(200, json={"response": response_text}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    resp = submit(client)
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "done"
    result = job["result"]
    assert result["translation_provider"] == "ollama"
    assert result["segments"][0]["text_es"] == "Hola mundo"
    assert result["segments"][1]["text_es"] == "Esto es una prueba"
