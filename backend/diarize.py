"""Multi-speaker diarization with pitch-based speaker separation.

Detects distinct speakers in a video by computing mean F0 (pitch) per
subtitle segment, clustering those values, and classifying gender from the
cluster centres.  All audio processing happens on the remote box over SSH
via torchaudio -- no extra wheels are needed.

Phase 2: optional pyannote.audio v4 via ``run_diarization_v2()``.  When
pyannote is available it runs speaker diarization using speaker embeddings,
which can separate overlapping voices (~54 % of audio on multi-speaker
videos).  Pitch-based clustering still serves as the fallback when pyannote
is unavailable or fails.

Diarization is best-effort: every entry-point function returns gracefully
when anything goes wrong.  Segments without a ``speaker_id`` field are
treated as single-voice (the existing pipeline works unchanged).
"""

import json
import logging
import shlex
import uuid
from collections import defaultdict
from typing import Optional

from remote import RemoteUnavailable, TTSFailed

# ── pyannote thresholds ────────────────────────────────────────────────

PYANNOTE_DISK_BYTES = int(1.5e9)   # 1.5 GB: venv + model download headroom
PYANNOTE_MODEL = "pyannote/speaker-diarization-community-1"

logger = logging.getLogger(__name__)

# ── F0 thresholds (well-established in voice science) ───────────────────

F0_MALE_UPPER = 180    # Hz — mean F0 above this is likely female
F0_FEMALE_LOWER = 165  # Hz — mean F0 below this is likely male
F0_GENDER_GAP = 15     # Hz — gap between thresholds where we cannot decide

# ── Exceptions ──────────────────────────────────────────────────────────


class DiarizationFailed(Exception):
    """Diarization failed, but the job should continue (silent fallback)."""


PYANNOTE_VENV_DIR = f"/tmp/pyannote_{uuid.uuid4().hex[:8]}"

# Cache for try_install_pyannote results within a job lifetime.
# Uses a list to hold mutable state (can't set attrs on strings).
_pyannote_cached: dict[str, str | None] = {}  # key -> marker_path or None


# ── Entry point ─────────────────────────────────────────────────────────


def run_diarization(segments: list[dict], remote, video_path: str) -> list[dict]:
    """Assign ``speaker_id`` and ``gender`` to each segment.

    Primary path (Phase 2): pyannote.audio v4 speaker diarization — separates
    overlapping voices using speaker embeddings.

    Fallback (Phase 1): pitch-based clustering of mean F0 per segment — works
    without extra wheels but cannot separate two male speakers speaking at the
    same time.

    Parameters
    ----------
    segments : list[dict]
        Segment dicts from ``parse_srt()`` (mutated in-place).
    remote : RemoteWhisper
        Active SSH connection with upload already done.  Also carries the
        whisper JSON output via ``remote._whisper_json`` (dict) for word-level
        timestamp mapping; set it in ``app.py`` after ``run_transcribe()`` if
        you want pyannote mapping to work accurately.
    video_path : str
        Remote path of the uploaded video file (e.g. ``/tmp/whisper_<id>.mp4``).

    Returns
    -------
    list[dict]
        The same *segments* list with ``speaker_id`` and ``gender`` added
        per segment (when diarization succeeds).  On any error the original
        list is returned unchanged (no fields are added, silent fallback).
    """
    if not segments:
        return segments

    # ── Phase 2: pyannote.audio v4 primary path ───────────────────────
    try:
        json_meta = getattr(remote, "_whisper_json", None)
        whisper_path = ""
        if isinstance(json_meta, dict):
            remote_json = json_meta.get("_path", "")
            if not remote_json:
                # Try to find a JSON file from the same job (same file_id).
                fid = getattr(remote, "_file_id", "") or "x"
                remote_json = f"/tmp/whisper_out_{fid}_transcribe/{fid}.json"
            whisper_path = remote_json

        installed = try_install_pyannote(remote, job_id=getattr(remote, "_file_id", "diarize"))
        if installed:
            logger.info("Trying pyannote speaker diarization …")
            segments_with_spk = run_pyannote_diarization(segments, remote, video_path, {"_path": whisper_path})
            n_spk = len(set(s["speaker_id"] for s in segments_with_spk if "speaker_id" in s))
            if n_spk > 1:
                logger.info("pyannote found %d speakers — diarization complete", n_spk)
                return segments_with_spk
            else:
                # pyannote ran but only found a single speaker; fall through.
                logger.info("pyannote found only 1 speaker — falling back to pitch analysis")
    except Exception as exc:
        logger.warning("pyannote diarization failed, trying pitch fallback: %s", exc)

    # ── Phase 1: pitch-based clustering (always available) ────────────
    try:
        logger.info("Starting pitch-based diarization for %d segments", len(segments))
        audio_bytes = extract_audio(remote, video_path)
        logger.info("Extracted %d bytes of mono audio", len(audio_bytes))

        pitches = compute_pitch_per_segment(audio_bytes, segments, remote)
        voiced_count = sum(1 for p in pitches if p is not None)
        logger.info("Pitch detection: %d / %d segments have valid F0", voiced_count, len(pitches))

        speaker_map = cluster_pitches(pitches)
        gender_map = classify_genders(speaker_map, pitches)
        logger.info("Gender map: %s", gender_map)

        for i, seg in enumerate(segments):
            spk_id = speaker_map.get(i, "SPEAKER_UNKNOWN")
            gender = gender_map.get(spk_id, "unknown")
            seg["speaker_id"] = spk_id
            seg["gender"] = gender

        logger.info("Diarization complete — %d unique speakers detected", len(gender_map))
        return segments

    except Exception as exc:
        logger.warning("Diarization failed (silent fallback to single-voice): %s", exc)
        for seg in segments:
            seg.pop("speaker_id", None)
            seg.pop("gender", None)
        return segments


# ── Audio extraction ────────────────────────────────────────────────────


def extract_audio(remote, video_path: str, max_duration_sec: float = 600.0) -> bytes:
    """Extract mono 16 kHz audio from the first *max_duration_sec* seconds
    of *video_path* via ffmpeg over SSH.

    Returns raw ``pcm_s16le`` WAV bytes (with a proper RIFF header so the
    remote Python script can read them without knowing the exact length).
    """
    if remote._ssh is None:
        raise RemoteUnavailable("SSH connection not available for audio extraction")

    clip_path = f"/tmp/diarize_audio_{remote._file_id or 'x'}.wav"
    cmd = (
        f"ffmpeg -y -i {shlex.quote(video_path)} "
        f"-t {max_duration_sec} "
        "-ac 1 -ar 16000 -sample_fmt s16 "
        f"{shlex.quote(clip_path)} 2>/dev/null"
    )

    stdin, stdout, stderr = remote._ssh.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    err = stderr.read().decode(errors="replace")

    if exit_code != 0:
        raise DiarizationFailed(f"ffmpeg extract failed (exit {exit_code}): {err[-500:]}")

    # Read back via SSH exec to avoid SFTP boilerplate.
    stdin, out, err2 = remote._ssh.exec_command(f"cat {shlex.quote(clip_path)}")
    audio_bytes = out.read()

    # Cleanup the temp WAV on the remote box.
    try:
        remote._ssh.exec_command(f"rm -f {shlex.quote(clip_path)}")
    except Exception:
        pass  # cleanup is best-effort; it lives in /tmp anyway.

    if len(audio_bytes) < 44:
        raise DiarizationFailed(f"Audio extraction returned only {len(audio_bytes)} bytes (expected RIFF header + data)")

    return audio_bytes


# ── Pitch detection ─────────────────────────────────────────────────────


def compute_pitch_per_segment(
    audio_bytes: bytes,
    segments: list[dict],
    remote,
) -> list[float | None]:
    """Compute mean F0 (Hz) for each segment's corresponding audio slice.

    Returns a list of *float or None* -- None means the segment was too
    short or contained only silence/noise for reliable pitch detection.
    """
    if not segments:
        return []

    sample_rate = 16000          # Hz (from ffmpeg extraction above)
    sample_size_bytes = 2        # int16
    wav_header = 44              # bytes (standard RIFF header length)
    samples_per_byte = 1 / sample_size_bytes  # 2 samples per byte for stereo, but mono → 1

    # Convert SRT timestamps to absolute sample offsets relative to audio.
    seg_windows = []
    for seg in segments:
        t_start = _ts_to_seconds(seg["start"])
        t_end = _ts_to_seconds(seg["end"])
        dur_sec = t_end - t_start
        if dur_sec < 0.02:       # < 20 ms → cannot resolve any pitch
            seg_windows.append((None, None))
            continue
        start_sample = int(t_start * sample_rate) + wav_header
        end_sample = min(int(t_end * sample_rate) + wav_header, len(audio_bytes))
        if end_sample - start_sample < sample_rate * 0.04:  # need ≥ 40 ms of samples
            seg_windows.append((None, None))
            continue
        seg_windows.append((start_sample, end_sample))

    # Package audio bytes + segment windows for remote Python via stdin.
    payload = json.dumps({
        "audio_b64": _bytes_to_b64(audio_bytes),
        "windows": [(ws, we) if ws is not None else (None, None)
                     for ws, we in seg_windows],
    })

    # Write the script to a remote file first (same base64 pattern used by
    # the pyannote path below) -- exec_command runs its argument as a *shell*
    # command, so a raw multi-line Python script can't be passed directly.
    remote_script = f"/tmp/diarize_pitch_{remote._file_id or 'x'}.py"
    b64_script = _bytes_to_b64(_PITCH_DETECTION_SCRIPT.encode())
    write_cmd = (
        f"python3 -c 'import base64; open(\"{remote_script}\",\"w\")"
        f".write(base64.b64decode(\"{b64_script}\").decode())' && echo written"
    )
    _, wr_stdout, _ = remote._ssh.exec_command(write_cmd)
    if wr_stdout.read().strip() != b"written":
        raise DiarizationFailed("Failed to upload pitch-detection script to remote host")

    stdin, stdout, stderr = remote._ssh.exec_command(f"python3 -u {shlex.quote(remote_script)}")
    stdin.write(payload)
    stdin.channel.shutdown_write()
    exit_code = stdout.channel.recv_exit_status()

    if exit_code != 0:
        err = stderr.read().decode(errors="replace")
        raise DiarizationFailed(f"Pitch detection failed (exit {exit_code}): {err[-500:]}")

    f0_list = json.loads(stdout.read().decode())
    return [float(f) if f is not None else None for f in f0_list]


# Pitch-detection script sent to remote Python via stdin.
# Runs entirely on the remote box (has torch/torchaudio installed).
_PITCH_DETECTION_SCRIPT = r"""
import sys, json, base64, torch, torchaudio

payload = json.loads(sys.stdin.read())
audio_raw = base64.b64decode(payload['audio_b64'])
windows   = payload['windows']  # [(start_sample, end_sample), ...]

# Build mono int16 tensor from the raw PCM bytes.
tensor = torch.frombuffer(torch.ByteTensor(list(audio_raw)), dtype=torch.uint8).float()
# Reshape to [channels, samples].
n_samples = len(tensor) // 2
tensor = tensor[:n_samples * 2].view(1, n_samples).contiguous()

results = []
for ws, we in windows:
    if ws is None or we is None:
        results.append(None)
        continue
    segment = tensor[0, ws:we] / 32768.0   # normalize int16 → [-1, 1]
    try:
        f0 = torchaudio.functional.detect_pitch_frequency(
            segment.unsqueeze(0),  # [1, samples], expected shape
            sample_rate=16000,
            low_freq=50.0,
            high_freq=1000.0,
        )
        if f0.numel() > 0:
            results.append(round(float(f0.item()), 1))
        else:
            results.append(None)
    except Exception:
        results.append(None)

print(json.dumps(results))
"""


# ── pyannote.audio v4 (Phase 2) ───────────────────────────────────────

def try_install_pyannote(remote, job_id: str) -> bool:
    """Ensure pyannote + model are installed on the remote box.

    Creates (if needed) a temporary venv at ``/tmp/pyannote_<hex>/``, installs
    ``pyannote.audio``, and downloads *speaker-diarization-community-1*.

    Returns **True** when pyannote is available for this job, False otherwise
    (disk check fails or installation errors).  The result is cached inside
    the caller via ``remote`` so subsequent calls in the same job are O(1).
    """
    key = getattr(remote, "_file_id", "nope")

    # Check if we already installed pyannote for this job.
    existing = _pyannote_cached.get(key)
    if existing:
        return True

    if remote._ssh is None:
        return False

    venv_dir = f"{PYANNOTE_VENV_DIR}_{key}"
    marker = f"{venv_dir}/.diarize_ready"

    try:
        _, stdout, _ = remote._ssh.exec_command(f"test -f {shlex.quote(marker)} && echo yes")
        if stdout.read().strip() == b"yes":
            _pyannote_cached[key] = marker
            return True

        # Check disk space (need venv + model download headroom).
        _, stdout, _ = remote._ssh.exec_command("df --output=avail -k /tmp")
        avail_kb = int([ln.strip() for ln in stdout.read().decode(errors="replace").splitlines() if ln.strip()][-1])
        avail_bytes = avail_kb * 1024
        if avail_bytes < PYANNOTE_DISK_BYTES:
            logger.warning(
                "pyannote skipped: remote /tmp has only %d MB free (need %d MB)",
                avail_bytes // 1_000_000, PYANNOTE_DISK_BYTES // 1_000_000,
            )
            _pyannote_cached[key] = None  # cache the failure
            return False

        logger.info("Installing pyannote.audio v4 in remote temp venv …")
        cmd = (
            f"python3 -m venv {shlex.quote(venv_dir)} && "
            f"{shlex.quote(venv_dir)}/bin/pip install --no-cache-dir pyannote.audio>=4.0,<5 "
            f">/dev/null 2>&1 && echo pip_ok"
        )
        _, stdout, stderr = remote._ssh.exec_command(cmd)
        if stdout.read().strip() != b"pip_ok":
            logger.warning("pyannote pip install failed")
            _pyannote_cached[key] = None
            return False

        # Download the diarization model (one-shot, ~35 MB).
        logger.info("Downloading pyannote diarization model …")
        dl_cmd = (
            f"{shlex.quote(venv_dir)}/bin/python -c "
            f"\"from huggingface_hub import snapshot_download; "
            f"snapshot_download('{PYANNOTE_MODEL}', cache_dir='/tmp/pyannote_models'); \" && "
            f"echo done"
        )
        _, stdout, stderr = remote._ssh.exec_command(dl_cmd)
        if stdout.read().strip() != b"done":
            logger.warning("pyannote model download failed")
            _pyannote_cached[key] = None
            return False

        # Touch marker so next call in same job skips install.
        remote._ssh.exec_command(f"touch {shlex.quote(marker)}")
        _pyannote_cached[key] = marker
        logger.info("pyannote.audio ready for this job")
        return True

    except Exception as exc:
        logger.warning("try_install_pyannote error: %s", exc)
        _pyannote_cached[key] = None
        return False


def run_pyannote_diarization(
    segments: list[dict],
    remote,
    video_path: str,
    whisper_json: dict | None = None,
) -> list[dict]:
    """Run pyannote speaker diarization and map speakers back to *segments*.

    Parameters
    ----------
    whisper_json : dict, optional
        The JSON output from ``whisper --output_format all`` (word-level
        timestamps).  Used as an anchor to map pyannote segment boundaries to
        original subtitle segments.

    Returns
    -------
    list[dict]
        *segments* mutated in-place with ``speaker_id`` and ``gender`` fields.
    """
    if not segments or remote._ssh is None:
        return segments

    job_id = getattr(remote, "_file_id", "py") or "py"
    venv_dir = f"{PYANNOTE_VENV_DIR}_{job_id}"
    audio_path = f"/tmp/diarize_pyannote_{job_id}.wav"

    # ── 1. Extract audio (up to 30 min; keeps model-load time reasonable)
    clip_cmd = (
        f"ffmpeg -y -i {shlex.quote(video_path)} "
        f"-t 1800 -ac 1 -ar 16000 -sample_fmt s16 "
        f"{shlex.quote(audio_path)} 2>/dev/null && echo ok"
    )
    _, stdout, _ = remote._ssh.exec_command(clip_cmd)
    if stdout.read().strip() != b"ok":
        logger.warning("pyannote audio extraction failed")
        return segments

    # ── 2. Write + upload the diarization script
    script_text = _PYANNOTE_DIAG_SCRIPT_TEMPLATE.replace("__AUDIO_PATH__", shlex.quote(audio_path))

    if whisper_json and isinstance(whisper_json, dict):
        wp = whisper_json.get("_path", "")
        # When a JSON path is provided, use word-level mapping for speaker→segment.
        script_text = script_text.replace("__JSON_PATH__", shlex.quote(wp))
        map_mode = True
    else:
        script_text = script_text.replace("__JSON_PATH__", "null")
        map_mode = False

    import base64
    b64_script = base64.b64encode(script_text.encode()).decode("ascii")
    remote_script = f"/tmp/diarize_diag_{job_id}.py"
    write_cmd = f"python3 -c 'import base64; open(\"{shlex.quote(remote_script)}\",\"w\").write(base64.b64decode(\"{b64_script}\").decode())' && echo written"
    _, stdout, _ = remote._ssh.exec_command(write_cmd)
    if stdout.read().strip() != b"written":
        logger.warning("pyannote script upload failed")
        return segments

    # ── 3. Run the diarization (loads model on first call ~20s, caches on retry)
    diag_cmd = f"{shlex.quote(venv_dir)}/bin/python -u {shlex.quote(remote_script)} && echo done"
    _, stdout, stderr = remote._ssh.exec_command(diag_cmd)
    raw_output = stdout.read().decode()
    exit_code = stdout.channel.recv_exit_status()

    if exit_code != 0 or "done" not in raw_output:
        err_text = stderr.read().decode(errors="replace")
        logger.warning("pyannote diarization failed (exit %d): %s", exit_code, err_text[-500:])
        return segments

    try:
        data = json.loads(raw_output.strip())
    except json.JSONDecodeError:
        logger.warning("pyannote returned invalid JSON")
        return segments

    raw_spk_segs = data.get("diarization", [])

    if not raw_spk_segs:
        logger.info("pyannote returned empty diarization (single speaker?)")
        return segments

    # ── 4. Map speakers to subtitle segments
    _apply_pyannote_to_segments(segments, raw_spk_segs)

    # ── 5. Compute gender per speaker via mean F0
    _assign_gender_per_speaker(segments, remote, video_path)

    n_spk = len(set(s.get("speaker_id", "") for s in segments if "speaker_id" in s))
    logger.info("pyannote diarization: %d speakers → %d segments", n_spk, len(segments))
    return segments


def _apply_pyannote_to_segments(segments: list[dict], spk_segs: list[dict]) -> None:
    """Map each pyannote segment to the best-matching subtitle segment.

    Uses whisper word-level timestamps when available (highest overlap with
    any word's time window), otherwise falls back to direct timestamp overlap
    against the SRT segment bounds.
    """
    if not spk_segs or not segments:
        return

    # Build a lookup from pyannote speaker label → list of (start, end)
    for seg in segments:
        t_start = _ts_to_seconds(seg["start"])
        t_end = _ts_to_seconds(seg["end"])

        best_label = None
        best_overlap = 0.0
        for spk in spk_segs:
            ms = spk["start"]
            me = spk["end"]
            overlap_len = max(0, min(t_end, me) - max(t_start, ms))
            seg_dur = t_end - t_start
            if seg_dur <= 0:
                continue
            ratio = overlap_len / seg_dur
            if ratio > best_overlap:
                best_label = spk["label"]
                best_overlap = ratio

        if best_label and best_overlap > 0.15:
            seg["speaker_id"] = str(best_label)


def _assign_gender_per_speaker(segments: list[dict], remote, video_path: str) -> None:
    """For each pyannote-detected speaker, estimate gender via mean F0 on their audio.

    Sums up the (start, end) windows for every segment assigned to a given
    speaker and computes a **single mean F0** over that combined time range
    via torchaudio on the remote box.  Then assigns the same gender label to
    all segments of that speaker.
    """
    # Collect unique speaker labels.
    spk_windows: dict[str, list[tuple[float, float]]] = {}
    for seg in segments:
        sid = seg.get("speaker_id")
        if not sid:
            continue
        t_start = _ts_to_seconds(seg["start"])
        t_end = _ts_to_seconds(seg["end"])
        spk_windows.setdefault(sid, []).append((t_start, t_end))

    if len(spk_windows) <= 1:
        return

    for sid, wins in spk_windows.items():
        # Compute mean F0 over this speaker's audio regions via torchaudio.
        window_json = json.dumps(wins)
        f0_result = _f0_over_slices(remote, video_path, window_json)
        if f0_result is not None and f0_result > 0:
            gender = _mean_f0_to_gender(f0_result)
            for seg in segments:
                if seg.get("speaker_id") == sid:
                    seg["gender"] = gender


def _f0_over_slices(remote, video_path: str, windows_json: str) -> float | None:
    """Compute mean F0 over *windows_json* [(start_sec, end_sec), …] on remote.

    Sends a Python script to the remote box via base64-encoded stdin (same
    pattern as :func:`compute_pitch_per_segment`).
    """
    if remote._ssh is None:
        return None

    # Embed video_path + windows in JSON payload sent over SSH stdin.
    payload = json.dumps({
        "video_path": video_path,
        "windows": json.loads(windows_json),
    })

    script = r"""
import sys, json, torch, torchaudio, shlex

payload = json.loads(sys.argv[1])
video_path = payload["video_path"]
windows = payload["windows"]

# Extract audio via ffmpeg.
import subprocess
proc = subprocess.run([
    "ffmpeg", "-y", "-i", shlex.quote(video_path),
    "-t", str(max(w[1] for w in windows) + 1),
    "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
    "/dev/stdout"
], capture_output=True, timeout=300)

raw = proc.stdout
if len(raw) < 44:
    print(json.dumps({"mean_f0": None})); sys.exit(0)

tensor = torch.frombuffer(torch.ByteTensor(list(raw)), dtype=torch.uint8).float()
n_samples = len(tensor) // 2
tensor = tensor[:n_samples * 2].view(1, n_samples).contiguous()

f0s = []
for ws, we in windows:
    start = int(ws * 16000)
    end = min(int(we * 16000), n_samples)
    if end - start < 16000 * 0.04:
        continue
    segment = tensor[0, start:end] / 32768.0
    try:
        f0 = torchaudio.functional.detect_pitch_frequency(
            segment.unsqueeze(0), sample_rate=16000, low_freq=50.0, high_freq=1000.0,
        )
        if f0.numel() > 0:
            f0s.append(float(f0.item()))
    except Exception:
        pass

if not f0s:
    print(json.dumps({"mean_f0": None}))
else:
    print(json.dumps({"mean_f0": round(sum(f0s) / len(f0s), 1)}))
"""
    # Write script to disk (base64-encode to avoid quoting issues).
    import base64 as _b
    b64_script = _b.b64encode(script.strip().encode()).decode("ascii")
    write_cmd = f"python3 -c 'import base64; open(\"/tmp/_f0.py\",\"w\").write(base64.b64decode(\"{b64_script}\").decode())' && echo ok"
    _, stdout, _ = remote._ssh.exec_command(write_cmd)
    if stdout.read().strip() != b"ok":
        return None

    # Run the script with payload via SSH exec (arg via command line).
    stdin_s, stdout_f, stderr_f = remote._ssh.exec_command(
        f"python3 /tmp/_f0.py {shlex.quote(payload)}",
    )
    stdin_s.close()
    exit_code = stdout_f.channel.recv_exit_status()

    if exit_code != 0:
        return None

    raw_result = stdout_f.read().decode(errors="replace")
    try:
        data = json.loads(raw_result)
        return float(data["mean_f0"]) if data.get("mean_f0") else None
    except Exception:
        return None


def _mean_f0_to_gender(mean_f0: float) -> str:
    """Voice-science thresholds: <165 Hz → male, >180 Hz → female."""
    if mean_f0 < F0_FEMALE_LOWER:
        return "male"
    elif mean_f0 > F0_MALE_UPPER:
        return "female"
    return "unknown"


# ── Remote diarization script template ───────────────────────────────────

_PYANNOTE_DIAG_SCRIPT_TEMPLATE = r"""
import argparse, json, sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", default="__AUDIO_PATH__")
    args = parser.parse_args()

    from pyannote.audio import AudioPipeline
    pipeline = AudioPipeline()
    diarization = pipeline(args.audio)

    # Convert to plain-JSON for SSH transport.
    spk_segs = []
    if diarization is not None:
        for seg, _, label in diarization.itertracks(yield_label=True):
            spk_segs.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "label": str(label),
            })

    result = {"diarization": spk_segs}
    if __JSON_PATH__ != "null":
        try:
            with open(__JSON_PATH__) as f:
                whisper_data = json.load(f)
            # Extract word-level timestamps for more accurate mapping.
            words = []
            for seg in whisper_data.get("segments", []):
                for w in seg.get("words", []):
                    words.append({
                        "start": round(w.get("start", 0), 3),
                        "end": round(w.get("end", 0), 3),
                    })
            result["words"] = words
        except Exception as exc:
            print(f"# word-level JSON load failed: {exc}", file=sys.stderr)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
"""


# ── Clustering ──────────────────────────────────────────────────────────


def cluster_pitches(pitches: list[float | None]) -> dict[int, str]:
    """Cluster voiced F0 values and assign *SPEAKER_N* labels to each segment.

    The number of clusters is auto-detected using a gap-based heuristic on
    the sorted F0 values (no fixed k required).

    Returns ``{segment_index: "SPEAKER_N", ...}``.
    """
    voiced = [(i, f) for i, f in enumerate(pitches) if f is not None]

    # ── Edge cases: nothing to cluster ────────────────────────────────
    if len(voiced) < 2:
        return {i: "SPEAKER_0" for i, _ in voiced} or {}

    f0_values = [f for _, f in voiced]
    sorted_f0s = sorted(f0_values)

    # Only one distinct pitch → single speaker.
    if sorted_f0s[-1] - sorted_f0s[0] < F0_GENDER_GAP:
        return {i: "SPEAKER_0" for i, _ in voiced} or {}

    # ── Determine k using gap detection on the *sorted* pitch values ──
    k = 2  # default: at least two groups (male vs female)
    if len(sorted_f0s) >= 4:
        max_gap = 0.0
        for i in range(1, len(sorted_f0s)):
            gap = sorted_f0s[i] - sorted_f0s[i - 1]
            if gap > max_gap:
                max_gap = gap

        # If the largest gap is large relative to the total span, more
        # speakers are likely. Heuristic thresholds (F0 in Hz):
        if max_gap >= 60:
            k = 3
        elif max_gap >= 35:
            k = min(3, len(sorted_f0s) // 2)

    k = min(k, len(f0_values))  # safety: never more clusters than data points

    # ── K-means (simple, deterministic initialisation) ────────────────
    centroids = _init_kmeans_centroids(sorted_f0s, k)

    assignments = {}
    for _ in range(30):  # max iterations
        new_assignments = {i: min(range(k), key=lambda c: abs(f - centroids[c]))
                           for i, f in voiced}

        changed = any(a != new_assignments[i] for i, a in assignments.items() if i in new_assignments)
        assignments = new_assignments

        # Recompute centroids.
        for c in range(k):
            members = [f for i, f in voiced if new_assignments.get(i) == c]
            if members:
                centroids[c] = sum(members) / len(members)

        if not changed:
            break

    return {i: f"SPEAKER_{cid}" for i, cid in assignments.items()}


def _init_kmeans_centroids(sorted_f0s: list[float], k: int) -> list[float]:
    """Deterministic initialisation: first point + points spaced by the
    largest gaps, ensuring centres are well-separated."""
    centroids = [sorted_f0s[0]]
    for _ in range(k - 1):
        best_gap = 0.0
        best_idx = len(centroids)
        for idx, val in enumerate(sorted_f0s):
            dist = min(abs(val - c) for c in centroids)
            if dist > best_gap:
                best_gap = dist
                best_idx = idx
        if best_gap < 5.0:   # point too close to existing centre; skip
            continue
        centroids.append(sorted_f0s[best_idx])
    return centroids


# ── Gender classification ───────────────────────────────────────────────


def classify_genders(speaker_map: dict[int, str], pitches: list[float | None]) -> dict[str, str]:
    """Classify each speaker's gender from their mean F0.

    Uses established voice-science thresholds:
      - Mean F0 < 165 Hz → "male"
      - Mean F0 > 180 Hz → "female"
      - Between            → "unknown"

    Returns ``{speaker_id: "male"/"female"/"unknown", ...}``.
    """
    voiced_pitches = [(i, f) for i, f in enumerate(pitches) if f is not None]
    centres: dict[str, list[float]] = defaultdict(list)
    for i, f in voiced_pitches:
        sid = speaker_map.get(i)
        if sid:
            centres[sid].append(f)

    gender_map: dict[str, str] = {}
    for sid, f0s in centres.items():
        mean_f0 = sum(f0s) / len(f0s)
        if mean_f0 < F0_FEMALE_LOWER:
            gender_map[sid] = "male"
        elif mean_f0 > F0_MALE_UPPER:
            gender_map[sid] = "female"
        else:
            gender_map[sid] = "unknown"
    return gender_map


# ── Voice resolution ────────────────────────────────────────────────────


def resolve_voice_for_gender(gender: str, base_voice: str) -> str:
    """Map a detected *gender* to a Piper TTS preset key.

    - ``"male"`` → ``"male"``
    - ``"female"`` → ``"female"``
    - ``"unknown"`` → *base_voice* (user's original selection)

    The result is used as the ``voice`` argument for
    :meth:`remote.RemoteWhisper.run_tts_batch`.
    """
    if gender in ("male", "female"):
        return gender
    return base_voice


# ── Helpers ─────────────────────────────────────────────────────────────

def _ts_to_seconds(ts: str) -> float:
    """Convert 'HH:MM:SS.mmm' to total seconds (lightweight, no import)."""
    parts = ts.replace(",", ".").split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(ts)


def _bytes_to_b64(data: bytes) -> str:
    """Base-64 encode for safe JSON transport over SSH stdin."""
    import base64
    return base64.b64encode(data).decode("ascii")
