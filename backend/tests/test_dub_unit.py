"""
Unit tests for `dub.py` -- calls its functions directly with real ffmpeg/
ffprobe subprocess calls and real WAV/mp4 fixtures (no mocking of ffmpeg,
matching the convention already used in test_jobs_e2e.py: it's fast and
local, so faking it would only hide real bugs).

Covers pure-function behavior that has no worker/FastAPI dependencies:
drift-forward vs. silence padding in build_narration_track, the
wav_duration_seconds round-trip, mux_audio_into_video's stream mapping, and
split_video_by_chapters' ordering/bounds.
"""
import io
import json
import subprocess
import wave
from pathlib import Path

import pytest

import dub


def make_fake_wav(duration: float = 0.3) -> bytes:
    """A minimal valid mono 16-bit WAV -- real bytes (not a stub), mirrors
    the helper in test_jobs_e2e.py."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * int(22050 * duration))
    return buf.getvalue()


def make_real_video(path: Path, duration: float, with_audio: bool = False) -> None:
    """Real, ffprobe-inspectable mp4. `-g 1` forces a keyframe on every
    frame so `-c copy` splits/trims land on exact boundaries."""
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    args += ["-g", "1", "-c:v", "libx264"]
    if with_audio:
        args += ["-c:a", "aac"]
    args += [str(path)]
    subprocess.run(args, check=True)


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


def probe_streams(path: Path) -> list[dict]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)["streams"]


# ── build_narration_track: drift-forward (P1) ──────────────────────────

def test_drift_forward_shifts_subsequent_audio(tmp_path):
    """Segment 0's real clip (1.0s) overruns the 0.5s gap to segment 1's
    start -- the track must drift forward from the overrun point instead
    of inserting silence or time-stretching."""
    work_dir = tmp_path / "work"
    segment_wavs = [make_fake_wav(1.0), make_fake_wav(0.3)]
    segment_starts = [0.0, 0.5]

    out_path = dub.build_narration_track(
        segment_wavs, segment_starts, total_duration=1.5, work_dir=work_dir
    )

    # The gap>0.02 branch must be skipped for the overrunning transition.
    assert not (work_dir / "gap_1.wav").exists()

    # Cursor must have advanced by segment 0's real clip length (1.0s), not
    # the nominal 0.5s gap. Proven by the tail silence duration: with the
    # correct drift, cursor after both segments is 1.0 + 0.3 = 1.3s, so the
    # tail must be total_duration(1.5) - 1.3 = ~0.2s. If the implementation
    # instead used the nominal gap (cursor = 0.5 + 0.3 = 0.8s), the tail
    # would be ~0.7s -- a clearly distinguishable difference.
    tail_path = work_dir / "gap_tail.wav"
    assert tail_path.exists()
    tail_duration = dub.wav_duration_seconds(tail_path.read_bytes())
    assert tail_duration == pytest.approx(0.2, abs=0.05)

    final_duration = dub.wav_duration_seconds(out_path.read_bytes())
    assert final_duration == pytest.approx(1.5, abs=0.05)


def test_non_overrunning_segments_get_silence_padded(tmp_path):
    """When each clip is shorter than its gap to the next segment's start,
    silence must be inserted for every gap > 0.02s, and the final track
    must be padded out to total_duration."""
    work_dir = tmp_path / "work"
    segment_wavs = [make_fake_wav(0.2), make_fake_wav(0.2), make_fake_wav(0.2)]
    segment_starts = [0.0, 1.0, 2.0]

    out_path = dub.build_narration_track(
        segment_wavs, segment_starts, total_duration=3.0, work_dir=work_dir
    )

    assert (work_dir / "gap_1.wav").exists()
    assert (work_dir / "gap_2.wav").exists()
    assert (work_dir / "gap_tail.wav").exists()

    final_duration = dub.wav_duration_seconds(out_path.read_bytes())
    assert final_duration == pytest.approx(3.0, abs=0.05)


# ── wav_duration_seconds ────────────────────────────────────────────────

def test_wav_duration_seconds_matches_known_duration(tmp_path):
    """Round-trips a real WAV generated independently via ffmpeg's
    `anullsrc` (not the stdlib `wave`-based make_fake_wav helper used
    elsewhere) so the assertion isn't tautological."""
    wav_path = tmp_path / "known.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
            "-t", "1.5", "-c:a", "pcm_s16le", str(wav_path),
        ],
        check=True,
    )

    duration = dub.wav_duration_seconds(wav_path.read_bytes())

    assert duration == pytest.approx(1.5, abs=0.02)


# ── mux_audio_into_video ─────────────────────────────────────────────────

def test_mux_audio_into_video_drops_original_audio_stream(tmp_path):
    """The source video has its OWN audio stream (sine wave); the output
    must contain exactly one video stream and one AAC audio stream sourced
    from the narration WAV, with the original audio discarded."""
    video_path = tmp_path / "source.mp4"
    make_real_video(video_path, duration=2.0, with_audio=True)
    narration_path = tmp_path / "narration.wav"
    narration_path.write_bytes(make_fake_wav(2.0))
    output_path = tmp_path / "muxed.mp4"

    dub.mux_audio_into_video(video_path, narration_path, output_path)

    streams = probe_streams(output_path)
    video_streams = [s for s in streams if s["codec_type"] == "video"]
    audio_streams = [s for s in streams if s["codec_type"] == "audio"]

    assert len(video_streams) == 1
    assert len(audio_streams) == 1
    assert audio_streams[0]["codec_name"] == "aac"


# ── split_video_by_chapters ──────────────────────────────────────────────

def test_split_video_by_chapters_sorts_unsorted_input(tmp_path):
    """Chapters supplied out of time order must still produce
    chronologically ordered, correctly bounded clips."""
    video_path = tmp_path / "source.mp4"
    make_real_video(video_path, duration=40.0)
    chapters = [{"time": 30, "title": "C"}, {"time": 0, "title": "A"}, {"time": 10, "title": "B"}]
    output_dir = tmp_path / "chapters"

    outputs = dub.split_video_by_chapters(video_path, chapters, total_duration=40.0, output_dir=output_dir)

    assert [p.name for p in outputs] == ["00.mp4", "01.mp4", "02.mp4"]
    assert probe_duration(outputs[0]) == pytest.approx(10.0, abs=0.5)  # [0, 10)
    assert probe_duration(outputs[1]) == pytest.approx(20.0, abs=0.5)  # [10, 30)
    assert probe_duration(outputs[2]) == pytest.approx(10.0, abs=0.5)  # [30, 40)


def test_split_video_by_chapters_time_beyond_total_duration_characterization(tmp_path):
    """CHARACTERIZATION TEST -- documents the CURRENT observed behavior of
    split_video_by_chapters when a chapter's `time` exceeds total_duration
    (an unvalidated input). It does NOT assert a new contract: the current
    implementation computes `-ss <time> -to total_duration` for the last
    chapter, and when `time` (100) > total_duration (5), ffmpeg rejects the
    resulting `-to` value as smaller than `-ss` and split_video_by_chapters
    surfaces that as DubbingFailed. If this ever needs to become graceful
    input validation, that would be a production behavior change requiring
    its own change proposal -- not asserted here."""
    video_path = tmp_path / "source.mp4"
    make_real_video(video_path, duration=5.0)
    chapters = [{"time": 0, "title": "A"}, {"time": 100, "title": "B"}]

    with pytest.raises(dub.DubbingFailed):
        dub.split_video_by_chapters(video_path, chapters, total_duration=5.0, output_dir=tmp_path / "chapters")


# ── Known out-of-scope gap: work_dir leak on build_narration_track failure ─

def test_build_narration_track_failure_leaks_work_dir_characterization(tmp_path):
    """CHARACTERIZATION TEST of a KNOWN production gap -- NOT desired
    behavior, and NO production fix is made here (see spec/design
    "Out of Scope" section for this change).

    build_narration_track() creates `work_dir` as its very first action
    (`work_dir.mkdir(parents=True, exist_ok=True)`), before any ffmpeg
    call. If a later step raises, `work_dir` is left on disk because the
    only caller-side cleanup (app.py's `shutil.rmtree(work_dir, ...)`)
    sits inside `if narration_track is not None:`, which never becomes
    true when this function itself raises. This test proves the leak at
    the dub.py level: work_dir exists after the exception propagates."""
    work_dir = tmp_path / "work"
    invalid_wav_bytes = b"not a valid wav file"

    with pytest.raises(Exception):
        dub.build_narration_track(
            [invalid_wav_bytes], [0.0], total_duration=1.0, work_dir=work_dir
        )

    assert work_dir.exists()  # the leak: mkdir already ran before the failure
