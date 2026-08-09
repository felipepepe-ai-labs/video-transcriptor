"""
Unit tests for backend/diarize.py.

Pure-logic functions (clustering, gender classification, pyannote→segment
mapping, voice resolution) are tested directly. SSH-backed functions
(extract_audio, compute_pitch_per_segment, try_install_pyannote,
run_pyannote_diarization) use a small FakeSSHClient, following the same
pattern as FakeSSHClient in test_jobs_e2e.py.
"""
import base64
import json

import diarize


# ── Fakes (SSH boundary) ────────────────────────────────────────────────


class FakeChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class FakeStream:
    def __init__(self, data: bytes, exit_status=0):
        self._data = data
        self.channel = FakeChannel(exit_status)

    def read(self):
        return self._data


class FakeStdin:
    def __init__(self, exit_status=0, on_shutdown=None):
        self.written = b""
        self.channel = FakeChannel(exit_status)
        self._on_shutdown = on_shutdown

    def write(self, data):
        self.written += data.encode() if isinstance(data, str) else data

    def close(self):
        pass


class FakeRemote:
    """Minimal stand-in for RemoteWhisper -- diarize.py only reads
    ._ssh and ._file_id off the object it's handed."""

    def __init__(self, ssh):
        self._ssh = ssh
        self._file_id = "job123"


class ScriptedSSHClient:
    """Dispatches exec_command by matching substrings against a list of
    (predicate, handler) pairs, in order. Unmatched commands raise, so
    tests fail loudly on unexpected remote calls instead of silently."""

    def __init__(self):
        self.commands = []
        self.handlers = []

    def on(self, predicate, handler):
        self.handlers.append((predicate, handler))
        return self

    def exec_command(self, cmd):
        self.commands.append(cmd)
        for predicate, handler in self.handlers:
            if predicate(cmd):
                return handler(cmd)
        raise AssertionError(f"unexpected remote command: {cmd!r}")


# ── cluster_pitches ──────────────────────────────────────────────────────


def test_cluster_pitches_single_speaker_when_pitches_are_close():
    pitches = [120.0, 122.0, 118.0, 121.0]
    result = diarize.cluster_pitches(pitches)
    assert set(result.values()) == {"SPEAKER_0"}


def test_cluster_pitches_splits_two_distinct_voice_groups():
    # Male cluster around ~110Hz, female cluster around ~220Hz.
    pitches = [108.0, 112.0, 115.0, 218.0, 222.0, 225.0]
    result = diarize.cluster_pitches(pitches)
    assert len(result) == len(pitches)
    labels = set(result.values())
    assert len(labels) == 2
    # The two low values must share a label, distinct from the two high values.
    low_labels = {result[0], result[1]}
    high_labels = {result[3], result[4]}
    assert low_labels != high_labels
    assert len(low_labels) == 1
    assert len(high_labels) == 1


def test_cluster_pitches_ignores_none_entries():
    pitches = [None, 120.0, None, 122.0]
    result = diarize.cluster_pitches(pitches)
    assert set(result.keys()) == {1, 3}


def test_cluster_pitches_empty_list():
    assert diarize.cluster_pitches([]) == {}


def test_cluster_pitches_single_voiced_segment():
    result = diarize.cluster_pitches([None, 150.0, None])
    assert result == {1: "SPEAKER_0"}


# ── classify_genders ─────────────────────────────────────────────────────


def test_classify_genders_male_below_threshold():
    speaker_map = {0: "SPEAKER_0", 1: "SPEAKER_0"}
    pitches = [110.0, 115.0]
    genders = diarize.classify_genders(speaker_map, pitches)
    assert genders == {"SPEAKER_0": "male"}


def test_classify_genders_female_above_threshold():
    speaker_map = {0: "SPEAKER_0", 1: "SPEAKER_0"}
    pitches = [210.0, 220.0]
    genders = diarize.classify_genders(speaker_map, pitches)
    assert genders == {"SPEAKER_0": "female"}


def test_classify_genders_unknown_in_ambiguous_gap():
    speaker_map = {0: "SPEAKER_0"}
    pitches = [172.0]  # between F0_FEMALE_LOWER (165) and F0_MALE_UPPER (180)
    genders = diarize.classify_genders(speaker_map, pitches)
    assert genders == {"SPEAKER_0": "unknown"}


def test_classify_genders_multiple_speakers():
    speaker_map = {0: "SPEAKER_0", 1: "SPEAKER_1"}
    pitches = [110.0, 220.0]
    genders = diarize.classify_genders(speaker_map, pitches)
    assert genders == {"SPEAKER_0": "male", "SPEAKER_1": "female"}


# ── resolve_voice_for_gender ─────────────────────────────────────────────


def test_resolve_voice_for_gender_maps_known_genders():
    assert diarize.resolve_voice_for_gender("male", "female") == "male"
    assert diarize.resolve_voice_for_gender("female", "male") == "female"


def test_resolve_voice_for_gender_falls_back_to_base_voice_when_unknown():
    assert diarize.resolve_voice_for_gender("unknown", "female") == "female"


# ── _apply_pyannote_to_segments (this is where the ts_to_seconds bug lived) ─


def test_apply_pyannote_to_segments_assigns_best_overlapping_speaker():
    segments = [
        {"start": "00:00:00,000", "end": "00:00:02,000"},
        {"start": "00:00:02,000", "end": "00:00:04,000"},
    ]
    spk_segs = [
        {"start": 0.0, "end": 2.0, "label": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "label": "SPEAKER_01"},
    ]
    diarize._apply_pyannote_to_segments(segments, spk_segs)
    assert segments[0]["speaker_id"] == "SPEAKER_00"
    assert segments[1]["speaker_id"] == "SPEAKER_01"


def test_apply_pyannote_to_segments_skips_low_overlap():
    segments = [{"start": "00:00:00,000", "end": "00:00:02,000"}]
    # Only 10% overlap -- below the 0.15 threshold.
    spk_segs = [{"start": 1.8, "end": 2.5, "label": "SPEAKER_00"}]
    diarize._apply_pyannote_to_segments(segments, spk_segs)
    assert "speaker_id" not in segments[0]


def test_apply_pyannote_to_segments_handles_empty_inputs():
    segments = [{"start": "00:00:00,000", "end": "00:00:02,000"}]
    diarize._apply_pyannote_to_segments(segments, [])
    assert "speaker_id" not in segments[0]


# ── _mean_f0_to_gender ────────────────────────────────────────────────────


def test_mean_f0_to_gender_thresholds():
    assert diarize._mean_f0_to_gender(100.0) == "male"
    assert diarize._mean_f0_to_gender(200.0) == "female"
    assert diarize._mean_f0_to_gender(172.0) == "unknown"


# ── run_diarization orchestration ────────────────────────────────────────


def test_run_diarization_returns_empty_list_unchanged():
    assert diarize.run_diarization([], FakeRemote(None), "/tmp/video.mp4") == []


def test_run_diarization_silent_fallback_on_total_failure(monkeypatch):
    """When both pyannote and pitch analysis fail, segments come back
    unmodified (no speaker_id/gender fields) rather than raising."""
    segments = [{"start": "00:00:00,000", "end": "00:00:02,000", "text": "hi"}]

    monkeypatch.setattr(diarize, "try_install_pyannote", lambda remote, job_id: False)

    def boom_extract_audio(remote, video_path):
        raise diarize.DiarizationFailed("ffmpeg not available in test")

    monkeypatch.setattr(diarize, "extract_audio", boom_extract_audio)

    result = diarize.run_diarization(segments, FakeRemote(None), "/tmp/video.mp4")

    assert result is segments
    assert "speaker_id" not in result[0]
    assert "gender" not in result[0]


def test_run_diarization_uses_pitch_fallback_when_pyannote_unavailable(monkeypatch):
    segments = [
        {"start": "00:00:00,000", "end": "00:00:02,000", "text": "a"},
        {"start": "00:00:02,000", "end": "00:00:04,000", "text": "b"},
    ]

    monkeypatch.setattr(diarize, "try_install_pyannote", lambda remote, job_id: False)
    monkeypatch.setattr(diarize, "extract_audio", lambda remote, video_path: b"\x00" * 100)
    monkeypatch.setattr(
        diarize, "compute_pitch_per_segment",
        lambda audio_bytes, segments, remote: [110.0, 220.0],
    )

    result = diarize.run_diarization(segments, FakeRemote(None), "/tmp/video.mp4")

    assert result[0]["gender"] == "male"
    assert result[1]["gender"] == "female"
    assert result[0]["speaker_id"] != result[1]["speaker_id"]


def test_run_diarization_prefers_pyannote_when_it_finds_multiple_speakers(monkeypatch):
    segments = [
        {"start": "00:00:00,000", "end": "00:00:02,000", "text": "a"},
        {"start": "00:00:02,000", "end": "00:00:04,000", "text": "b"},
    ]

    def fake_pyannote(segs, remote, video_path, whisper_json):
        segs[0]["speaker_id"] = "SPEAKER_00"
        segs[0]["gender"] = "male"
        segs[1]["speaker_id"] = "SPEAKER_01"
        segs[1]["gender"] = "female"
        return segs

    monkeypatch.setattr(diarize, "try_install_pyannote", lambda remote, job_id: True)
    monkeypatch.setattr(diarize, "run_pyannote_diarization", fake_pyannote)

    result = diarize.run_diarization(segments, FakeRemote(None), "/tmp/video.mp4")

    assert result[0]["speaker_id"] == "SPEAKER_00"
    assert result[1]["speaker_id"] == "SPEAKER_01"


def test_run_diarization_falls_back_to_pitch_when_pyannote_finds_one_speaker(monkeypatch):
    segments = [{"start": "00:00:00,000", "end": "00:00:02,000", "text": "a"}]

    def fake_pyannote(segs, remote, video_path, whisper_json):
        segs[0]["speaker_id"] = "SPEAKER_00"
        return segs

    monkeypatch.setattr(diarize, "try_install_pyannote", lambda remote, job_id: True)
    monkeypatch.setattr(diarize, "run_pyannote_diarization", fake_pyannote)
    monkeypatch.setattr(diarize, "extract_audio", lambda remote, video_path: b"\x00" * 100)
    monkeypatch.setattr(
        diarize, "compute_pitch_per_segment",
        lambda audio_bytes, segments, remote: [130.0],
    )

    result = diarize.run_diarization(segments, FakeRemote(None), "/tmp/video.mp4")

    # Pitch fallback overwrote the single-speaker pyannote result.
    assert result[0]["speaker_id"] == "SPEAKER_0"
    assert result[0]["gender"] == "male"


# ── compute_pitch_per_segment (SSH boundary, exercises the script-upload fix) ─


def test_compute_pitch_per_segment_writes_script_then_runs_it():
    """Regression test for the bug where the raw Python script text was
    passed directly to exec_command() as a shell command instead of being
    written to a file and executed with `python3`."""
    ssh = ScriptedSSHClient()
    written_files = {}

    def handle_write(cmd):
        assert cmd.startswith("python3 -c 'import base64;")
        # Extract the destination path and decode the written content, to
        # make sure it's actually the pitch-detection script -- and not,
        # say, the raw script text mistakenly used as the command itself.
        path = cmd.split('"', 2)[1]
        b64 = cmd.split('"')[3]
        written_files[path] = base64.b64decode(b64).decode()
        return None, FakeStream(b"written\n"), None

    def handle_run(cmd):
        assert cmd.startswith("python3 -u ")
        # The script must have actually been written before being run.
        path = cmd.split("python3 -u ", 1)[1].strip()
        assert path in written_files
        assert "detect_pitch_frequency" in written_files[path]
        stdin = FakeStdin()
        return stdin, FakeStream(json.dumps([123.4, None]).encode()), FakeStream(b"")

    ssh.on(lambda c: c.startswith("python3 -c"), handle_write)
    ssh.on(lambda c: c.startswith("python3 -u "), handle_run)

    remote = FakeRemote(ssh)
    segments = [
        {"start": "00:00:00,000", "end": "00:00:02,000"},
        {"start": "00:00:02,000", "end": "00:00:02,010"},  # too short -> None
    ]
    audio_bytes = b"RIFF" + b"\x00" * 44 + b"\x00\x00" * 32000  # header + 2s of silence

    result = diarize.compute_pitch_per_segment(audio_bytes, segments, remote)

    assert result == [123.4, None]


def test_compute_pitch_per_segment_raises_on_nonzero_exit():
    ssh = ScriptedSSHClient()
    ssh.on(lambda c: c.startswith("python3 -c"), lambda c: (None, FakeStream(b"written\n"), None))

    def handle_run(cmd):
        stdin = FakeStdin(exit_status=1)
        return stdin, FakeStream(b"", exit_status=1), FakeStream(b"torch error")

    ssh.on(lambda c: c.startswith("python3 -u "), handle_run)

    remote = FakeRemote(ssh)
    segments = [{"start": "00:00:00,000", "end": "00:00:02,000"}]
    audio_bytes = b"\x00" * (44 + 32000)

    try:
        diarize.compute_pitch_per_segment(audio_bytes, segments, remote)
        assert False, "expected DiarizationFailed"
    except diarize.DiarizationFailed as exc:
        assert "torch error" in str(exc)
