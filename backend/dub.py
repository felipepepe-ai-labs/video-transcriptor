"""
Builds a timestamp-aligned Spanish narration track from per-segment Piper
clips, muxes it onto the original video, and cuts the result into
per-chapter clips. Runs locally (ffmpeg + stdlib `wave`) -- only the TTS
synthesis itself happens remotely, via RemoteWhisper.run_tts_batch().

Segments are laid out sequentially with silence gaps, not overlaid/mixed:
each clip starts no earlier than its subtitle's timestamp, but if a
segment's Spanish narration runs longer than the gap to the next segment,
the track drifts forward from there rather than overlapping audio. There
is no time-stretching to force-fit each slot.
"""
import io
import subprocess
import wave
from pathlib import Path


class DubbingFailed(Exception):
    """ffmpeg exited non-zero while building/muxing the dubbed audio or video."""


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.getnframes() / w.getframerate()


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", *args], capture_output=True)
    if proc.returncode != 0:
        raise DubbingFailed(proc.stderr.decode(errors="replace")[-800:])


def _silence(duration: float, out_path: Path) -> None:
    _run_ffmpeg([
        "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
        "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(out_path),
    ])


def build_narration_track(
    segment_wavs: list[bytes], segment_starts: list[float], total_duration: float, work_dir: Path
) -> Path:
    """segment_starts[i] is segment i's start time in seconds, relative to
    the video. Returns the path to one WAV spanning total_duration."""
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_list = work_dir / "concat.txt"
    lines: list[str] = []
    cursor = 0.0

    for i, (wav_bytes, start) in enumerate(zip(segment_wavs, segment_starts)):
        gap = start - cursor
        if gap > 0.02:
            silence_path = work_dir / f"gap_{i}.wav"
            _silence(gap, silence_path)
            lines.append(f"file '{silence_path.name}'")
            cursor += gap
        seg_path = work_dir / f"seg_{i}.wav"
        seg_path.write_bytes(wav_bytes)
        lines.append(f"file '{seg_path.name}'")
        cursor += wav_duration_seconds(wav_bytes)

    if total_duration - cursor > 0.02:
        tail_path = work_dir / "gap_tail.wav"
        _silence(total_duration - cursor, tail_path)
        lines.append(f"file '{tail_path.name}'")

    concat_list.write_text("\n".join(lines) + "\n")
    out_path = work_dir / "narration_track.wav"
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(out_path)])
    return out_path


def mux_audio_into_video(video_path: Path, audio_path: Path, output_path: Path) -> None:
    _run_ffmpeg([
        "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(output_path),
    ])


def split_video_by_chapters(
    video_path: Path, chapters: list[dict], total_duration: float, output_dir: Path
) -> list[Path]:
    """chapters: [{"time": seconds, "title": str}, ...], any order."""
    output_dir.mkdir(parents=True, exist_ok=True)
    sorted_chapters = sorted(chapters, key=lambda c: c["time"])
    outputs = []
    for i, ch in enumerate(sorted_chapters):
        start = ch["time"]
        end = sorted_chapters[i + 1]["time"] if i + 1 < len(sorted_chapters) else total_duration
        out_path = output_dir / f"{i:02d}.mp4"
        _run_ffmpeg(["-i", str(video_path), "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-c", "copy", str(out_path)])
        outputs.append(out_path)
    return outputs
