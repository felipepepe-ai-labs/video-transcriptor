import asyncio
import json
import os
import queue
import re
import shutil
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from x_progress import registry as x_progress

import dub
import jobs
import youtube
from x_sync import (
    import_cookies as _import_cookies,
    ScrapingError,
    CookieParseError,
    NoMediaFound,
    DATA_DIR,
    download_video as _download_video,
)
from remote import (
    InsufficientRemoteStorage,
    RemoteUnavailable,
    RemoteWhisper,
    TranscriptionFailed,
    TTSFailed,
)
from translate import translate_with_fallback

# ── Config ────────────────────────────────────────────────────────────
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
AUDIO_DIR = Path(__file__).parent / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
VIDEO_DIR = Path(__file__).parent / "video"
VIDEO_DIR.mkdir(exist_ok=True)
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

app = FastAPI(title="Video Transcriptor EN → ES")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── Helpers (pure, reused as-is from the previous single-file design) ──
def ts_to_seconds(ts: str) -> float:
    """Convert 'HH:MM:SS.mmm' or 'MM:SS.mmm' to total seconds."""
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        return h * 3600 + m * 60 + s
    elif len(parts) == 2:
        m, s = int(parts[0]), float(parts[1])
        return m * 60 + s
    return float(ts)


def seconds_to_ts(sec: float) -> str:
    """Convert seconds to 'HH:MM:SS' format."""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def assign_chapters(segments: list[dict], chapters: list[dict]) -> None:
    """
    Mutate segments in-place, adding 'chapter' field.
    chapters is a sorted list of {time: <seconds>, title: str}.
    Each segment gets the chapter whose start time is closest but <= segment start.
    """
    if not chapters or not segments:
        return

    sorted_chapters = sorted(chapters, key=lambda c: c["time"])

    for seg in segments:
        seg_start = ts_to_seconds(seg["start"])
        chapter_title = None

        # Find the chapter this segment falls into
        for i, ch in enumerate(sorted_chapters):
            next_ch = sorted_chapters[i + 1] if i + 1 < len(sorted_chapters) else None
            if seg_start >= ch["time"]:
                if next_ch is None or seg_start < next_ch["time"]:
                    chapter_title = ch["title"]
                elif next_ch and seg_start >= next_ch["time"]:
                    # Segments after the next chapter start
                    continue
                else:
                    chapter_title = ch["title"]
            else:
                break

        # If no chapter matched, assign to first if segment is before all chapters
        if chapter_title is None and sorted_chapters:
            if seg_start < sorted_chapters[0]["time"]:
                chapter_title = sorted_chapters[0]["title"]

        seg["chapter"] = chapter_title


def parse_srt(srt_text: str) -> list[dict]:
    """Parse SRT subtitle content into structured segments with timestamps.
    Translation is applied afterward by the caller (see translate.py) so this
    stays a pure parser, decoupled from any particular translation provider.
    """
    segments = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        index = int(lines[0].strip())
        ts_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            lines[1],
        )
        if not ts_match:
            continue
        start, end = ts_match.group(1), ts_match.group(2)
        text = " ".join(lines[2:])
        segments.append({
            "index": index,
            "start": start.replace(",", "."),
            "end": end.replace(",", "."),
            "text_en": text.strip(),
        })
    return segments


# ── Background worker ───────────────────────────────────────────────
def _error_message(prefix: str, exc: Exception) -> str:
    return f"{prefix}: {exc}"


def _synthesize_narration_track(remote, texts_es, segment_starts, total_duration, work_dir, voice, on_progress=None):
    segment_wavs = remote.run_tts_batch(texts_es, voice=voice, on_progress=on_progress)
    return dub.build_narration_track(segment_wavs, segment_starts, total_duration=total_duration, work_dir=work_dir)


def _run_narration_and_dub(job_id, remote, local_path, duration, segments, formatted_chapters, voice, result):
    """One segment-per-Piper-clip narration track, timed to each segment's
    own timestamp (not one continuous pass over the full translated text) --
    this is what makes muxing onto the video possible in the next stage.
    Mutates `result` in place; narration/dubbing/splitting are bonuses on
    top of transcript+translation, so failures here are recorded as
    `*_error` fields rather than raised."""
    jobs.update_job(
        job_id, stage="voicing", progress=0.0,
        segments_done=0, segments_total=len(segments),
    )

    def report_tts_progress(done: int, total: int) -> None:
        jobs.update_job(
            job_id, segments_done=done, segments_total=total,
            progress=round(done / total * 100, 1) if total else 0.0,
        )

    texts_es = [s["text_es"] for s in segments]
    segment_starts = [ts_to_seconds(s["start"]) for s in segments]
    work_dir = VIDEO_DIR / job_id / "work"

    narration_track = None
    try:
        narration_track = _synthesize_narration_track(
            remote, texts_es, segment_starts, duration, work_dir, voice, on_progress=report_tts_progress
        )
        (AUDIO_DIR / f"{job_id}.wav").write_bytes(narration_track.read_bytes())
        result["audio_available"] = True
        result.pop("audio_error", None)
    except (TTSFailed, dub.DubbingFailed) as e:
        result["audio_available"] = False
        result["audio_error"] = str(e)

    result["dubbed_video_available"] = False
    result.pop("dubbed_video_error", None)
    result["chapter_clips_available"] = False
    result.pop("chapter_clips_error", None)

    # ── Dub: mux the narration track onto the original video.
    if narration_track is not None:
        jobs.update_job(job_id, stage="dubbing")
        try:
            dubbed_path = VIDEO_DIR / job_id / "dubbed.mp4"
            dub.mux_audio_into_video(local_path, narration_track, dubbed_path)
            result["dubbed_video_available"] = True
        except dub.DubbingFailed as e:
            result["dubbed_video_error"] = str(e)

        # ── Split: cut the dubbed video into one clip per chapter.
        if result["dubbed_video_available"] and formatted_chapters:
            jobs.update_job(job_id, stage="splitting")
            try:
                dub.split_video_by_chapters(
                    dubbed_path, formatted_chapters, total_duration=duration,
                    output_dir=VIDEO_DIR / job_id / "chapters",
                )
                result["chapter_clips_available"] = True
            except dub.DubbingFailed as e:
                result["chapter_clips_error"] = str(e)

        shutil.rmtree(work_dir, ignore_errors=True)

    result["voice"] = voice


def run_transcription_job(
    job_id: str, local_path: Path, filename: str, chapters: list[dict], voice: str,
    source_language: str | None = "en", source: str = "upload", url: Optional[str] = None,
) -> None:
    """Runs the full transcribe -> translate lifecycle for one job, updating
    job stage/status as it progresses. Any domain error is caught and stored
    on the job record (status=failed, error=<message>) since a background
    task has no HTTP response to raise into.

    `source_language=None` lets whisper auto-detect (YouTube path). When the
    source turns out to already be Spanish, translation and narration/dubbing
    are skipped: there is nothing to translate and re-voicing Spanish over
    Spanish is pointless."""
    jobs.update_job(job_id, status="running", stage="uploading")
    remote = RemoteWhisper()

    try:
        try:
            file_size = local_path.stat().st_size
            remote.connect()
            remote.check_disk_space(file_size)
            remote.upload(local_path, filename)

            jobs.update_job(job_id, stage="transcribing")
            srt_content, duration, detected_language = remote.run_transcribe(language=source_language)
            spanish_source = detected_language in ("es", "spanish")

            segments = parse_srt(srt_content)
            assign_chapters(segments, chapters)

            if spanish_source:
                for seg in segments:
                    seg["text_es"] = seg["text_en"]
                provider = "source-es"
            else:
                texts_en = [s["text_en"] for s in segments]
                jobs.update_job(
                    job_id, stage="translating", progress=0.0,
                    segments_done=0, segments_total=len(texts_en),
                )

                def report_translation_progress(done: int, total: int) -> None:
                    jobs.update_job(
                        job_id, segments_done=done, segments_total=total,
                        progress=round(done / total * 100, 1) if total else 0.0,
                    )

                texts_es, provider = translate_with_fallback(texts_en, on_progress=report_translation_progress)
                for seg, text_es in zip(segments, texts_es):
                    seg["text_es"] = text_es

            full_en = "\n".join(s["text_en"] for s in segments)
            full_es = "\n".join(s["text_es"] for s in segments)

            formatted_chapters = []
            for ch in sorted(chapters, key=lambda c: c.get("time", 0)):
                formatted_chapters.append({
                    "time": ch["time"],
                    "timestamp": seconds_to_ts(ch["time"]),
                    "title": ch["title"],
                })

            result = {
                "filename": filename,
                "source": source,
                "url": url,
                "source_language": detected_language,
                "duration_seconds": duration,
                "chapters": formatted_chapters,
                "segments": segments,
                "full_text_en": full_en,
                "full_text_es": full_es,
                "translation_provider": provider,
                "voice": voice,
                "audio_available": False,
                "dubbed_video_available": False,
                "chapter_clips_available": False,
            }

            _run_narration_and_dub(job_id, remote, local_path, duration, segments, formatted_chapters, voice, result)

            jobs.update_job(job_id, status="done", stage="done", progress=100.0, result=result)

        except RemoteUnavailable as e:
            jobs.update_job(job_id, status="failed", error=_error_message("502 Remote unavailable", e))
        except InsufficientRemoteStorage as e:
            jobs.update_job(job_id, status="failed", error=_error_message("507 Insufficient remote storage", e))
        except TranscriptionFailed as e:
            jobs.update_job(job_id, status="failed", error=_error_message("500 Transcription failed", e))
        except Exception as e:
            jobs.update_job(job_id, status="failed", error=_error_message("500 Job failed", e))
    finally:
        remote.cleanup()


def run_youtube_job(job_id: str, url: str, chapters: list[dict], voice: str) -> None:
    """Downloads a YouTube video into UPLOAD_DIR (same {job_id}.mp4 naming
    contract as uploads, so retts/chapter reruns keep working) and hands it
    to the regular pipeline with whisper language auto-detection."""
    jobs.update_job(job_id, status="running", stage="downloading")
    local_path = UPLOAD_DIR / f"{job_id}.mp4"

    try:
        meta = youtube.download_video(url, local_path)
    except youtube.DownloadFailed as e:
        jobs.update_job(job_id, status="failed", error=_error_message("502 YouTube download failed", e))
        return
    except Exception as e:
        jobs.update_job(job_id, status="failed", error=_error_message("500 YouTube download failed", e))
        return

    title = meta["title"]
    # The video's own chapter markers are adopted only when the user didn't
    # provide explicit ones -- explicit input always wins.
    if not chapters:
        chapters = meta.get("chapters") or []
    jobs.update_job(job_id, title=title, filename=f"{title}.mp4")

    run_transcription_job(
        job_id, local_path, f"{title}.mp4", chapters, voice,
        source_language=None, source="youtube", url=url,
    )


def rerun_narration_job(job_id: str, voice: str) -> None:
    """Re-runs just the voicing/dubbing/splitting stages for an already
    transcribed+translated job, reusing its stored segments and the
    original video still sitting in UPLOAD_DIR -- no re-upload, no
    re-transcribe, no re-translate."""
    job = jobs.get_job(job_id)
    result = job["result"]
    suffix = Path(result["filename"]).suffix or ".mp4"
    local_path = UPLOAD_DIR / f"{job_id}{suffix}"
    remote = RemoteWhisper()

    try:
        try:
            if not local_path.exists():
                raise FileNotFoundError("Original video is no longer available locally")
            remote.connect()
            _run_narration_and_dub(
                job_id, remote, local_path, result["duration_seconds"],
                result["segments"], result.get("chapters", []), voice, result,
            )
            jobs.update_job(job_id, status="done", stage="done", progress=100.0, result=result)
        except RemoteUnavailable as e:
            jobs.update_job(job_id, status="failed", error=_error_message("502 Remote unavailable", e))
        except Exception as e:
            jobs.update_job(job_id, status="failed", error=_error_message("500 Narration re-run failed", e))
    finally:
        remote.cleanup()


def _chapter_window_and_segments(result: dict, index: int) -> tuple[float, float, list[dict]]:
    """(start, end, segments) for chapter `index`, per the same
    nearest-preceding-timestamp convention `assign_chapters` uses."""
    chapters = result["chapters"]
    start = chapters[index]["time"]
    end = chapters[index + 1]["time"] if index + 1 < len(chapters) else result["duration_seconds"]
    segments = [s for s in result["segments"] if start <= ts_to_seconds(s["start"]) < end]
    return start, end, segments


def rerun_chapter_narration_job(job_id: str, index: int, voice: str) -> None:
    """Re-synthesizes narration for just one chapter's segments and rebuilds
    just that chapter's clip -- the full narration track, dubbed video, and
    other chapter clips are left untouched."""
    job = jobs.get_job(job_id)
    result = job["result"]
    start, end, chapter_segments = _chapter_window_and_segments(result, index)

    suffix = Path(result["filename"]).suffix or ".mp4"
    local_path = UPLOAD_DIR / f"{job_id}{suffix}"
    remote = RemoteWhisper()

    try:
        try:
            if not local_path.exists():
                raise FileNotFoundError("Original video is no longer available locally")
            if not chapter_segments:
                raise ValueError("No segments found in this chapter")
            remote.connect()

            texts_es = [s["text_es"] for s in chapter_segments]
            segment_starts = [ts_to_seconds(s["start"]) - start for s in chapter_segments]
            work_dir = VIDEO_DIR / job_id / f"work_chapter_{index}"

            def report_tts_progress(done: int, total: int) -> None:
                jobs.update_job(
                    job_id, segments_done=done, segments_total=total,
                    progress=round(done / total * 100, 1) if total else 0.0,
                )

            jobs.update_job(job_id, segments_done=0, segments_total=len(chapter_segments), progress=0.0)
            narration_track = _synthesize_narration_track(
                remote, texts_es, segment_starts, end - start, work_dir, voice, on_progress=report_tts_progress
            )

            slice_path = work_dir / "slice.mp4"
            dub.extract_video_slice(local_path, start, end, slice_path)
            chapter_path = VIDEO_DIR / job_id / "chapters" / f"{index:02d}.mp4"
            dub.mux_audio_into_video(slice_path, narration_track, chapter_path)
            shutil.rmtree(work_dir, ignore_errors=True)

            jobs.update_job(job_id, status="done", stage="done", progress=100.0)
        except RemoteUnavailable as e:
            jobs.update_job(job_id, status="failed", error=_error_message("502 Remote unavailable", e))
        except Exception as e:
            jobs.update_job(job_id, status="failed", error=_error_message("500 Chapter narration re-run failed", e))
    finally:
        remote.cleanup()


def rerun_chapter_audio_job(job_id: str, index: int, voice: str) -> None:
    """Synthesizes just the audio (no video mux) for one chapter's segments
    and saves it as its own WAV -- a quick voice preview that skips the
    ffmpeg video re-encode entirely."""
    job = jobs.get_job(job_id)
    result = job["result"]
    start, end, chapter_segments = _chapter_window_and_segments(result, index)
    remote = RemoteWhisper()

    try:
        try:
            if not chapter_segments:
                raise ValueError("No segments found in this chapter")
            remote.connect()

            texts_es = [s["text_es"] for s in chapter_segments]
            segment_starts = [ts_to_seconds(s["start"]) - start for s in chapter_segments]
            work_dir = VIDEO_DIR / job_id / f"work_chapter_audio_{index}"

            def report_tts_progress(done: int, total: int) -> None:
                jobs.update_job(
                    job_id, segments_done=done, segments_total=total,
                    progress=round(done / total * 100, 1) if total else 0.0,
                )

            jobs.update_job(job_id, segments_done=0, segments_total=len(chapter_segments), progress=0.0)
            narration_track = _synthesize_narration_track(
                remote, texts_es, segment_starts, end - start, work_dir, voice, on_progress=report_tts_progress
            )
            (AUDIO_DIR / f"{job_id}_ch{index}.wav").write_bytes(narration_track.read_bytes())
            shutil.rmtree(work_dir, ignore_errors=True)

            # Persisted on the job (like audio_available/chapter_clips_available)
            # rather than left as frontend-only state, so the player still shows
            # up after a page reload or reopening the job from history.
            chapter_audio_indexes = set(result.get("chapter_audio_indexes", []))
            chapter_audio_indexes.add(index)
            result["chapter_audio_indexes"] = sorted(chapter_audio_indexes)

            jobs.update_job(job_id, status="done", stage="done", progress=100.0, result=result)
        except RemoteUnavailable as e:
            jobs.update_job(job_id, status="failed", error=_error_message("502 Remote unavailable", e))
        except Exception as e:
            jobs.update_job(job_id, status="failed", error=_error_message("500 Chapter audio generation failed", e))
    finally:
        remote.cleanup()


# ── Routes ────────────────────────────────────────────────────────────
@app.post("/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    chapters_json: Optional[str] = Form(None),  # JSON: [{"time": 0, "title": "..."}, ...]
    voice: str = Form("male"),  # "male" | "female" -- es_ES Piper narration voice
):
    """Upload a video file, optionally with chapter markers. Enqueues a
    transcription+translation job and returns immediately."""
    if not video.filename:
        raise HTTPException(400, "No filename provided")

    chapters = []
    if chapters_json:
        try:
            chapters = json.loads(chapters_json)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid chapters JSON format")

    if voice not in ("male", "female"):
        raise HTTPException(400, "voice must be 'male' or 'female'")

    # Video is saved under the job's own id (not a separate random id) so
    # later stages (dubbing, chapter splitting) can find the source file
    # for a job without needing a DB column just to track that mapping.
    job_id = jobs.create_job(video.filename)
    suffix = Path(video.filename).suffix or ".mp4"
    local_path = UPLOAD_DIR / f"{job_id}{suffix}"
    with open(local_path, "wb") as f:
        shutil.copyfileobj(video.file, f, length=1048576)

    background_tasks.add_task(run_transcription_job, job_id, local_path, video.filename, chapters, voice)

    return {"job_id": job_id, "status": "queued"}


@app.post("/jobs/youtube")
async def create_youtube_job(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    chapters_json: Optional[str] = Form(None),
    voice: str = Form("male"),
):
    """Enqueue a transcription job for a YouTube URL. The video is downloaded
    server-side with yt-dlp; language is auto-detected (a Spanish video gets
    transcript-only treatment, an English one runs the full dub pipeline)."""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "url must be an http(s) URL")

    chapters = []
    if chapters_json:
        try:
            chapters = json.loads(chapters_json)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid chapters JSON format")

    if voice not in ("male", "female"):
        raise HTTPException(400, "voice must be 'male' or 'female'")

    job_id = jobs.create_job(url, source="youtube", url=url)
    background_tasks.add_task(run_youtube_job, job_id, url, chapters, voice)

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs")
async def list_jobs():
    return jobs.list_jobs()


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    if not jobs.delete_job(job_id):
        raise HTTPException(404, "Job not found")
    (AUDIO_DIR / f"{job_id}.wav").unlink(missing_ok=True)
    for chapter_audio in AUDIO_DIR.glob(f"{job_id}_ch*.wav"):
        chapter_audio.unlink(missing_ok=True)
    shutil.rmtree(VIDEO_DIR / job_id, ignore_errors=True)
    return {"deleted": job_id}


@app.get("/jobs/{job_id}/audio")
async def get_job_audio(job_id: str):
    audio_path = AUDIO_DIR / f"{job_id}.wav"
    if not audio_path.exists():
        raise HTTPException(404, "Audio not available for this job")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str):
    video_path = VIDEO_DIR / job_id / "dubbed.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Dubbed video not available for this job")
    return FileResponse(video_path, media_type="video/mp4", filename=f"{job_id}_dubbed.mp4")


@app.get("/jobs/{job_id}/chapters/{index}/video")
async def get_job_chapter_video(job_id: str, index: int):
    chapter_path = VIDEO_DIR / job_id / "chapters" / f"{index:02d}.mp4"
    if not chapter_path.exists():
        raise HTTPException(404, "Chapter clip not available for this job")
    return FileResponse(chapter_path, media_type="video/mp4", filename=f"{job_id}_chapter{index:02d}.mp4")


def _require_reranable_job(job_id: str) -> dict:
    """Shared guard for the two retts routes: job must exist, not already be
    running, and have a transcript+translation to re-voice."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job["status"] == "running":
        raise HTTPException(409, "Job is already running")
    result = job.get("result")
    if not result or not result.get("segments"):
        raise HTTPException(400, "Job has no transcript/translation yet")
    return job


@app.post("/jobs/{job_id}/retts")
async def retts_job(job_id: str, background_tasks: BackgroundTasks, voice: str = Form("male")):
    """Re-run just the voicing/dubbing/splitting stages for a job that's
    already been transcribed+translated -- e.g. to try the other voice or
    retry after a TTS/mux failure, without a full re-transcribe+translate."""
    _require_reranable_job(job_id)
    if voice not in ("male", "female"):
        raise HTTPException(400, "voice must be 'male' or 'female'")

    jobs.update_job(job_id, status="running", stage="voicing", error=None)
    background_tasks.add_task(rerun_narration_job, job_id, voice)
    return {"job_id": job_id, "status": "queued"}


@app.post("/jobs/{job_id}/chapters/{index}/retts")
async def retts_chapter(
    job_id: str, index: int, background_tasks: BackgroundTasks,
    voice: Optional[str] = Form(None),
):
    """Re-synthesize narration for just one chapter's segments and rebuild
    just that chapter's clip, leaving the full narration/dubbed video and
    other chapters untouched."""
    job = _require_reranable_job(job_id)
    result = job["result"]
    chapters = result.get("chapters") or []
    if index < 0 or index >= len(chapters):
        raise HTTPException(404, "Chapter not found")

    chosen_voice = voice or result.get("voice", "male")
    if chosen_voice not in ("male", "female"):
        raise HTTPException(400, "voice must be 'male' or 'female'")

    jobs.update_job(job_id, status="running", stage="voicing_chapter", error=None)
    background_tasks.add_task(rerun_chapter_narration_job, job_id, index, chosen_voice)
    return {"job_id": job_id, "status": "queued"}


@app.post("/jobs/{job_id}/chapters/{index}/audio")
async def generate_chapter_audio(
    job_id: str, index: int, background_tasks: BackgroundTasks,
    voice: Optional[str] = Form(None),
):
    """Synthesize just the audio (no video) for one chapter's text --
    a quick voice preview, skipping the ffmpeg re-encode entirely."""
    job = _require_reranable_job(job_id)
    result = job["result"]
    chapters = result.get("chapters") or []
    if index < 0 or index >= len(chapters):
        raise HTTPException(404, "Chapter not found")

    chosen_voice = voice or result.get("voice", "male")
    if chosen_voice not in ("male", "female"):
        raise HTTPException(400, "voice must be 'male' or 'female'")

    jobs.update_job(job_id, status="running", stage="voicing_chapter", error=None)
    background_tasks.add_task(rerun_chapter_audio_job, job_id, index, chosen_voice)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}/chapters/{index}/audio")
async def get_job_chapter_audio(job_id: str, index: int):
    audio_path = AUDIO_DIR / f"{job_id}_ch{index}.wav"
    if not audio_path.exists():
        raise HTTPException(404, "Audio not available for this chapter")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}_chapter{index:02d}.wav")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": os.getenv("REMOTE_MODEL", "large-v3-turbo"),
        "remote_host": os.getenv("REMOTE_HOST", "192.168.1.60"),
        "device": os.getenv("REMOTE_DEVICE", "cuda"),
    }


# ── X (Twitter) Bookmarks routes ───────────────────────────────────────

# Seconds between keep-alive comments on an idle SSE stream, so proxies and
# browsers don't quietly drop a connection that simply has nothing to report.
_SSE_HEARTBEAT_SECONDS = 15


@app.get("/x/progress")
async def x_progress_stream():
    """Live progress for the X sync and download workers, as Server-Sent Events.

    Both run as fire-and-forget background tasks, so this is the only way the UI
    can tell when they actually finish rather than guessing.
    """

    async def stream():
        subscriber = x_progress.subscribe()
        try:
            yield _sse({"type": "snapshot", "jobs": x_progress.snapshot()})
            while True:
                try:
                    event = await asyncio.to_thread(
                        subscriber.get, True, _SSE_HEARTBEAT_SECONDS
                    )
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
        finally:
            x_progress.unsubscribe(subscriber)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # don't let a proxy buffer the stream
        },
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"

@app.post("/x/import-cookies")
async def import_x_cookies(file: UploadFile = File(...)):
    """Upload a Netscape-format cookies.txt file. Validates that the required
    X authentication cookies (auth_token, ct0, twid) are present, then writes
    a Playwright sessionState JSON + cookies.txt (chmod 0600) to data/x-bookmarks/."""
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "cookies.upload"
    with open(str(tmp), "wb") as f:
        shutil.copyfileobj(file.file, f, length=1048576)

    try:
        result = _import_cookies(str(tmp))
    except CookieParseError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)

    return {"ok": True, "message": "Cookies imported", "found": result["found_cookies"]}


@app.post("/x/sync")
async def sync_x_bookmarks(background_tasks: BackgroundTasks):
    """Trigger Playwright headless scraping of X bookmarks. The session must
    have been set up first via POST /x/import-cookies."""
    session_path = DATA_DIR / "session.json"
    if not session_path.exists():
        raise HTTPException(400, "No session found — import cookies first (POST /x/import-cookies)")

    background_tasks.add_task(_sync_x_bookmarks_worker, str(session_path))
    return {"ok": True, "message": "Sync started"}


def _sync_x_bookmarks_worker(session_path: str) -> None:
    """Background worker for X bookmark sync."""
    report = x_progress.reporter("sync")
    report(message="Abriendo x.com/i/bookmarks…")
    try:
        from x_sync import sync_x_bookmarks

        scraped = sync_x_bookmarks(session_path, db=None)
    except ScrapingError as e:
        print(f"[x-sync] Error: {e}")  # noqa: T201
        x_progress.publish({"type": "error", "job": "sync", "bookmark_id": None, "message": str(e)})
        return

    x_progress.publish(
        {
            "type": "done",
            "job": "sync",
            "bookmark_id": None,
            "message": f"{len(scraped)} bookmarks sincronizados",
            "count": len(scraped),
        }
    )


@app.get("/x/bookmarks")
async def list_x_bookmarks(status: str | None = None):
    """List all X bookmarks. Optionally filter by status (new, interesting, downloaded)."""
    import x_bookmarks as xb
    return xb.list_bookmarks(status_filter=status)


@app.patch("/x/bookmarks/{id}/interesting")
async def toggle_interesting(id: int):
    """Toggle a bookmark's status between 'new' and 'interesting'."""
    import x_bookmarks as xb
    bm = xb.get_bookmark(id)
    if bm is None:
        raise HTTPException(404, "Bookmark not found")

    target = "interesting" if bm["status"] == "new" else "new"
    ok = xb.set_interesting(id, interesting=(target == "interesting"))
    if not ok:
        raise HTTPException(409, f"Cannot transition from '{bm['status']}' to '{target}'")
    return {"ok": True, "status": target}


@app.delete("/x/bookmarks/{id}")
async def delete_x_bookmark(id: int):
    """Delete a bookmark record (and its local file if present)."""
    import x_bookmarks as xb
    ok = xb.delete_bookmark(id)
    if not ok:
        raise HTTPException(404, "Bookmark not found")
    return {"deleted": id}


@app.post("/x/bookmarks/{id}/download")
async def download_x_bookmark(id: int, background_tasks: BackgroundTasks):
    """Download the video from a tweet (if it has one) using yt-dlp. The
    bookmarks' cookies.txt must be installed first."""
    import x_bookmarks as xb
    bm = xb.get_bookmark(id)
    if bm is None:
        raise HTTPException(404, "Bookmark not found")

    # Only bookmarks the user actually picked are downloaded. 'no_media' is
    # included so the UI can retry a tweet yt-dlp found no video in.
    if bm["status"] not in ("interesting", "no_media"):
        raise HTTPException(409, f"Cannot download a bookmark with status '{bm['status']}'")

    cookies_path = DATA_DIR / "cookies.txt"
    if not cookies_path.exists():
        raise HTTPException(400, "No cookies file — import cookies first")

    background_tasks.add_task(_download_bookmark_worker, id, bm["tweet_url"], str(cookies_path))
    return {"ok": True, "message": "Download started"}


def _download_bookmark_worker(bm_id: int, tweet_url: str, cookies_path: str) -> None:
    """Background worker for downloading a bookmark's video."""
    import x_bookmarks as xb

    report = x_progress.reporter("download", bookmark_id=int(bm_id))
    report(message="Descargando video…")

    try:
        file_path = _download_video(tweet_url, cookies_path)
    except NoMediaFound:
        # Not a failure: the tweet just isn't a video. Settle it in 'no_media'
        # so it stops looking like a download still pending.
        xb.mark_no_media(int(bm_id))
        x_progress.publish(
            {
                "type": "done",
                "job": "download",
                "bookmark_id": int(bm_id),
                "status": "no_media",
                "message": "El tweet no tiene video",
            }
        )
        return
    except Exception as e:
        print(f"[x-download] Error for bookmark {bm_id}: {e}")  # noqa: T201
        x_progress.publish(
            {"type": "error", "job": "download", "bookmark_id": int(bm_id), "message": str(e)}
        )
        return

    # db=None so the store owns the commit — passing a connection in leaves the
    # UPDATE uncommitted and the download is silently lost.
    xb.mark_downloaded(int(bm_id), file_path)
    x_progress.publish(
        {
            "type": "done",
            "job": "download",
            "bookmark_id": int(bm_id),
            "status": "downloaded",
            "message": "Descarga completada",
        }
    )


@app.post("/x/bookmarks/{id}/transcribe")
async def transcribe_x_bookmark(id: int, background_tasks: BackgroundTasks):
    """Trigger SSH Whisper transcription for a downloaded bookmark video.
    Requires the remote Whisper server to be configured (same env vars as main jobs)."""
    import x_bookmarks as xb
    bm = xb.get_bookmark(id)
    if bm is None:
        raise HTTPException(404, "Bookmark not found")
    if not bm.get("local_file_path"):
        raise HTTPException(400, "No local video file — download first")

    background_tasks.add_task(_transcribe_bookmark_worker, id, bm["local_file_path"])
    return {"ok": True, "message": "Transcription started"}


def _transcribe_bookmark_worker(bm_id: int, video_path: str) -> None:
    """Background worker for transcribing a bookmark's downloaded video via remote Whisper."""
    try:
        # Use the same RemoteWhisper pipeline as regular jobs.
        remote = RemoteWhisper()
        remote.connect()

        srt_content, duration, lang = remote.run_transcribe(language=None)  # auto-detect

        segments = parse_srt(srt_content)

        conn = xb._get_conn()
        try:
            with xb._lock:
                conn.execute(
                    "UPDATE bookmarks SET transcription_status=?, transcript_language=?, transcribed_at=? WHERE id=?",
                    ("done", lang, time.strftime("%Y-%m-%dT%H:%M:%SZ"), bm_id),
                )
                conn.commit()
        finally:
            conn.close()

        # Translate segments to Spanish using the existing pipeline.
        texts_en = [s["text_en"] for s in segments]

        def report_translation_progress(done: int, total: int) -> None:
            pass  # no DB update needed for background worker

        texts_es, provider = translate_with_fallback(texts_en, on_progress=report_translation_progress)
        for seg, text_en, text_es in zip(segments, texts_en, texts_es):
            seg["text_es"] = text_es

        full_es = "\n".join(s["text_es"] for s in segments)

        conn2 = xb._get_conn()
        try:
            with xb._lock:
                conn2.execute(
                    "UPDATE bookmarks SET transcript_original=?, full_text_es=?, translation_provider=? WHERE id=?",
                    ("\n".join(s["text_en"] for s in segments), full_es, provider, bm_id),
                )
                conn2.commit()
        finally:
            conn2.close()

    except Exception as e:
        print(f"[x-transcribe] Error for bookmark {bm_id}: {e}")  # noqa: T201


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
