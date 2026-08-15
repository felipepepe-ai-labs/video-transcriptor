import asyncio
import json
from contextlib import asynccontextmanager
import logging
import os
import queue
import re
import shutil
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv

load_dotenv()  # read backend/.env into os.environ before anything reads it

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from x_progress import registry as x_progress

import config
import dub
import jobs
import media_names
import x_bookmarks as xb
import x_export
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
import summarize

# ── Config ────────────────────────────────────────────────────────────
_FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
if _frontend_origin := os.getenv("FRONTEND_ALLOW_ORIGINS"):
    _FRONTEND_ALLOW_ORIGINS = [o for o in _frontend_origin.split(",") if o]
else:
    _FRONTEND_ALLOW_ORIGINS = [_FRONTEND_ORIGIN]

# Keep FRONTEND_ORIGIN for backward compatibility with code that reads it.
FRONTEND_ORIGIN = _FRONTEND_ORIGIN

# Configured first so everything below can log. Module loggers propagate to the
# root logger, which drops anything below WARNING when nothing configured it —
# without this the modules stay mute under uvicorn. DEBUG additionally turns on
# the per-round scroll trace of the X scrape.
logging.basicConfig(
    level=config.log_level(),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# All three hang off DATA_ROOT (see config.py), which defaults to this directory
# so an unset environment keeps every file where it already is.
_MEDIA_DIRS = config.media_dirs()
UPLOAD_DIR = _MEDIA_DIRS["uploads"]
AUDIO_DIR = _MEDIA_DIRS["audio"]
VIDEO_DIR = _MEDIA_DIRS["video"]
for _media_dir in (UPLOAD_DIR, AUDIO_DIR, VIDEO_DIR):
    try:
        _media_dir.mkdir(parents=True, exist_ok=True)  # parents: the root may be new
    except OSError as _exc:
        # DATA_ROOT is user-editable now, so an unplugged disk or a bad path must
        # not stop the backend from booting — the jobs that need these directories
        # will fail loudly on their own.
        logger.warning("cannot create %s: %s", _media_dir, _exc)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # BackgroundTasks run inside this process; they can legitimately update jobs
    # to "running"/"uploading" before this handler even finishes. Marking them as
    # failed here would kill every active job on restart. Orphaned rows are left
    # in their last state so the user sees the truth, and any truly stuck ones can
    # be cleaned up manually.
    yield


app = FastAPI(title="Video Transcriptor EN → ES", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_FRONTEND_ALLOW_ORIGINS,
    # PATCH and DELETE are used by the bookmarks UI and PUT by the settings
    # panel; without them here the browser's preflight answers 400 and the
    # buttons fail with an opaque network error.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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


# ── Shared request parsing ──────────────────────────────────────────
# Upload, YouTube and X all feed the same pipeline, so they take the same two
# knobs and must reject bad input identically. Three copies of this drifted
# apart the moment X was added.
def _parse_chapters(chapters_json: Optional[str]) -> list[dict]:
    """JSON: [{"time": 0, "title": "..."}, ...] -- absent means no chapters."""
    if not chapters_json:
        return []
    try:
        return json.loads(chapters_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid chapters JSON format")


def _validated_voice(voice: str) -> str:
    """Fail here rather than let Piper fail deep inside the job."""
    if voice not in ("male", "female"):
        raise HTTPException(400, "voice must be 'male' or 'female'")
    return voice


# ── Where a job's media lives ───────────────────────────────────────
# Names carry a readable slug so the media directories can be browsed, but the
# job id stays in front because it is still the index -- see media_names.
def _job_video_dir(job_id: str, title: str) -> Path:
    """This job's directory under video/.

    An existing one wins, whichever scheme named it: a re-run against a job
    from before the slug existed must reuse its directory rather than start a
    second one beside it.
    """
    return media_names.find_media_dir(VIDEO_DIR, job_id) or (
        VIDEO_DIR / media_names.media_dir_name(job_id, title)
    )


def _job_title(result: dict) -> str:
    """What to slug a job's files with."""
    return Path(result.get("filename") or "video").stem


def _download_stem(job_id: str) -> str:
    """Base name for a file the user downloads, read from the job record."""
    job = jobs.get_job(job_id) or {}
    return media_names.slugify(Path(job.get("filename") or job.get("title") or "video").stem)


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
    title = _job_title(result)
    video_dir = _job_video_dir(job_id, title)
    work_dir = video_dir / "work"

    narration_track = None
    try:
        narration_track = _synthesize_narration_track(
            remote, texts_es, segment_starts, duration, work_dir, voice, on_progress=report_tts_progress
        )
        narration_path = AUDIO_DIR / media_names.media_name(job_id, title, ".wav")
        narration_path.write_bytes(narration_track.read_bytes())
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
            dubbed_path = video_dir / "dubbed.mp4"
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
                    output_dir=video_dir / "chapters",
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

            # A large video spends minutes here. Without this the stage label
            # sits frozen on "Subiendo video..." with no way to tell a slow
            # upload from a wedged one.
            def report_upload_progress(percent: float) -> None:
                jobs.update_job(job_id, progress=percent)

            remote.upload(local_path, filename, on_progress=report_upload_progress)

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

            # Auto-generate a summary for the completed job (non-blocking).
            try:
                t = threading.Thread(
                    target=_generate_summary_worker, args=(job_id,), daemon=True,
                )
                t.start()
            except Exception as e:
                logger.warning("Summary generation failed to start for job %s: %s", job_id, e)

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
    # Only now is there a title to name the file after: yt-dlp had to run first.
    named_path = UPLOAD_DIR / media_names.media_name(job_id, title, ".mp4")
    local_path = local_path.rename(named_path)

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
    local_path = media_names.find_media(UPLOAD_DIR, job_id)
    remote = RemoteWhisper()

    try:
        try:
            if local_path is None:
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

    local_path = media_names.find_media(UPLOAD_DIR, job_id)
    video_dir = _job_video_dir(job_id, _job_title(result))
    remote = RemoteWhisper()

    try:
        try:
            if local_path is None:
                raise FileNotFoundError("Original video is no longer available locally")
            if not chapter_segments:
                raise ValueError("No segments found in this chapter")
            remote.connect()

            texts_es = [s["text_es"] for s in chapter_segments]
            segment_starts = [ts_to_seconds(s["start"]) - start for s in chapter_segments]
            work_dir = video_dir / f"work_chapter_{index}"

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
            chapter_path = video_dir / "chapters" / f"{index:02d}.mp4"
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
            work_dir = _job_video_dir(job_id, _job_title(result)) / f"work_chapter_audio_{index}"

            def report_tts_progress(done: int, total: int) -> None:
                jobs.update_job(
                    job_id, segments_done=done, segments_total=total,
                    progress=round(done / total * 100, 1) if total else 0.0,
                )

            jobs.update_job(job_id, segments_done=0, segments_total=len(chapter_segments), progress=0.0)
            narration_track = _synthesize_narration_track(
                remote, texts_es, segment_starts, end - start, work_dir, voice, on_progress=report_tts_progress
            )
            chapter_audio = AUDIO_DIR / media_names.chapter_audio_name(job_id, index, _job_title(result))
            chapter_audio.write_bytes(narration_track.read_bytes())
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


def _generate_summary_worker(job_id: str) -> None:
    """Generate a summary for an already-completed job in a background thread."""
    try:
        job = jobs.get_job(job_id)
        if not job or not job.get("result") or not job["result"].get("segments"):
            logger.info("No data to summarize for job %s", job_id)
            return

        # Skip if a summary already exists.
        if job["result"].get("summary_es"):
            logger.info("Job %s already has a summary, skipping", job_id)
            return

        result = job["result"]
        segments_es = [s.get("text_es", "") for s in result.get("segments", []) if s.get("text_es")]
        segments_en = [s.get("text_en", "") for s in result.get("segments", []) if s.get("text_en")]
        chapters = result.get("chapters", [])

        summary = summarize.generate_summary(segments_es, segments_en, chapters)
        if not summary:
            logger.warning("Summary generation returned empty for job %s", job_id)
            return

        # Persist in both result_json (summary_es) and top-level summary column.
        new_result = dict(result)
        new_result["summary_es"] = summary
        jobs.update_job(job_id, status="done", stage="done", progress=100.0, result=new_result)
        # Also update the top-level column via raw SQL to avoid list_jobs filtering.
        conn = jobs._get_conn()
        try:
            conn.execute("UPDATE jobs SET summary_es = ? WHERE id = ?", (summary, job_id))
            conn.commit()
        finally:
            conn.close()

        logger.info("Summary generated for job %s (%d chars)", job_id, len(summary))
    except Exception as e:
        logger.exception("Summary worker crashed for job %s: %s", job_id, e)


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

    chapters = _parse_chapters(chapters_json)
    voice = _validated_voice(voice)

    # Video is saved under the job's own id (not a separate random id) so
    # later stages (dubbing, chapter splitting) can find the source file
    # for a job without needing a DB column just to track that mapping.
    job_id = jobs.create_job(video.filename)
    suffix = Path(video.filename).suffix or ".mp4"
    local_path = UPLOAD_DIR / media_names.media_name(job_id, Path(video.filename).stem, suffix)
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

    chapters = _parse_chapters(chapters_json)
    voice = _validated_voice(voice)

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
    narration = media_names.find_media(AUDIO_DIR, job_id)
    if narration:
        narration.unlink(missing_ok=True)
    for chapter_audio in AUDIO_DIR.glob(f"{job_id}_ch*.wav"):
        chapter_audio.unlink(missing_ok=True)
    video_dir = media_names.find_media_dir(VIDEO_DIR, job_id)
    if video_dir:
        shutil.rmtree(video_dir, ignore_errors=True)
    return {"deleted": job_id}


@app.post("/jobs/{job_id}/summarize")
async def summarize_job(job_id: str, background_tasks: BackgroundTasks):
    """Trigger summary generation for an existing completed job.

    Returns immediately; the summary is generated in the background.
    The frontend polls GET /jobs/{id} to detect when summary_es appears.
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    result = job.get("result")
    if not result or not result.get("segments"):
        raise HTTPException(400, "Job has no transcript to summarize")

    if result.get("summary_es"):
        return {"status": "already_generated", "has_summary": True}

    background_tasks.add_task(_summarize_background_worker, job_id)
    return {"job_id": job_id, "status": "generating"}


def _summarize_background_worker(job_id: str) -> None:
    """Background task called via FastAPI BackgroundTasks."""
    try:
        job = jobs.get_job(job_id)
        if not job or not job.get("result"):
            return

        result = job["result"]
        segments_es = [s.get("text_es", "") for s in result.get("segments", []) if s.get("text_es")]
        segments_en = [s.get("text_en", "") for s in result.get("segments", []) if s.get("text_en")]
        chapters = result.get("chapters", [])

        summary = summarize.generate_summary(segments_es, segments_en, chapters)
        if not summary:
            logger.warning("Summary generation returned empty for job %s (via endpoint)", job_id)
            return

        new_result = dict(result)
        new_result["summary_es"] = summary
        jobs.update_job(job_id, status="done", stage="done", progress=100.0, result=new_result)

        conn = jobs._get_conn()
        try:
            conn.execute("UPDATE jobs SET summary_es = ? WHERE id = ?", (summary, job_id))
            conn.commit()
        finally:
            conn.close()

        logger.info("Summary generated for job %s (via endpoint)", job_id)
    except Exception as e:
        logger.exception("Summary background worker crashed for job %s", job_id)


# The download name is decided here, not in the browser: the frontend's
# download="..." attribute is ignored because these links are cross-origin
# (:5173 → :8000), so Content-Disposition is what the user actually gets.
@app.get("/jobs/{job_id}/audio")
async def get_job_audio(job_id: str):
    audio_path = media_names.find_media(AUDIO_DIR, job_id)
    if audio_path is None:
        raise HTTPException(404, "Audio not available for this job")
    return FileResponse(
        audio_path, media_type="audio/wav",
        filename=f"{_download_stem(job_id)}.locucion.wav",
    )


@app.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str):
    video_dir = media_names.find_media_dir(VIDEO_DIR, job_id)
    video_path = video_dir / "dubbed.mp4" if video_dir else None
    if video_path is None or not video_path.exists():
        raise HTTPException(404, "Dubbed video not available for this job")
    return FileResponse(
        video_path, media_type="video/mp4",
        filename=f"{_download_stem(job_id)}.doblado.mp4",
    )


@app.get("/jobs/{job_id}/chapters/{index}/video")
async def get_job_chapter_video(job_id: str, index: int):
    video_dir = media_names.find_media_dir(VIDEO_DIR, job_id)
    chapter_path = video_dir / "chapters" / f"{index:02d}.mp4" if video_dir else None
    if chapter_path is None or not chapter_path.exists():
        raise HTTPException(404, "Chapter clip not available for this job")
    return FileResponse(
        chapter_path, media_type="video/mp4",
        filename=f"{_download_stem(job_id)}.cap{index:02d}.mp4",
    )


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
    audio_path = media_names.find_chapter_audio(AUDIO_DIR, job_id, index)
    if audio_path is None:
        raise HTTPException(404, "Audio not available for this chapter")
    return FileResponse(
        audio_path, media_type="audio/wav",
        filename=f"{_download_stem(job_id)}.cap{index:02d}.locucion.wav",
    )


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

@app.get("/config")
async def get_config():
    """Effective settings, where each came from, and the resolved directories."""
    return config.describe()


@app.get("/config/browse")
async def browse_directories(path: Optional[str] = None):
    """List the sub-directories of *path*, for picking a folder from the UI.

    A browser can't reveal a real filesystem path — `webkitdirectory` hands back
    relative names and hides the rest — so choosing a folder on the server means
    the server has to enumerate it.
    """
    target = Path(path) if path else Path.home()

    if not target.is_dir():
        raise HTTPException(404, f"no existe el directorio: {target}")

    try:
        entries = sorted(
            (e for e in target.iterdir() if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower(),
        )
    except PermissionError:
        raise HTTPException(403, f"sin permiso para leer {target}")

    return {
        "path": str(target),
        # None at the filesystem root, so the UI knows to stop offering "up".
        "parent": None if target.parent == target else str(target.parent),
        "entries": [{"name": e.name, "path": str(e)} for e in entries],
    }


@app.put("/config")
async def put_config(changes: dict):
    """Save settings from the panel.

    Validation happens before anything is written: a half-applied save would
    strand future downloads in a directory nobody can write to.
    """
    allowed = {k: v for k, v in changes.items() if k in ("data_root", "log_level")}
    if not allowed:
        raise HTTPException(400, "nothing to update")

    errors = config.validate_settings(allowed)
    if errors:
        raise HTTPException(400, errors)

    if "log_level" in allowed:
        allowed["log_level"] = str(allowed["log_level"]).upper()

    config.save_settings(allowed)
    config.apply_log_level()  # takes effect now, no restart needed
    logger.info("settings updated: %s", ", ".join(sorted(allowed)))

    described = config.describe()
    # uploads/audio/video were frozen into constants at import time, so a new
    # root only reaches them after a restart. Say so rather than imply otherwise.
    described["restart_required"] = (
        ["uploads", "audio", "video"] if "data_root" in allowed else []
    )
    return described


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
    logger.info("x-sync worker started, session=%s", session_path)
    report = x_progress.reporter("sync")
    report(message="Abriendo x.com/i/bookmarks…")
    try:
        from x_sync import sync_x_bookmarks

        scraped = sync_x_bookmarks(session_path, db=None, on_progress=report)
    except ScrapingError as e:
        logger.exception("x-sync worker failed")
        x_progress.publish({"type": "error", "job": "sync", "bookmark_id": None, "message": str(e)})
        return

    logger.info("x-sync worker finished, %d bookmarks synced", len(scraped))
    # Every successful scrape refreshes the plain-file copy, so the backup is
    # never staler than the last sync and nobody has to remember to press a
    # button for it.
    _export_bookmarks_quietly()
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


@app.post("/x/export")
async def export_x_bookmarks():
    """Write the whole bookmarks store out as JSON + one Markdown per bookmark.

    Not a background task: this is a few dozen rows of text, and the caller
    wants to be told how many files it got.
    """
    try:
        return x_export.export_bookmarks()
    except OSError as e:
        # Almost always DATA_ROOT pointing at an unmounted disk.
        raise HTTPException(500, f"no se pudo escribir el backup: {e}")


def _export_bookmarks_quietly() -> None:
    """Refresh the backup after a sync, without ever failing the sync.

    The scrape is the expensive part — three minutes of navigation — and a
    backup that cannot be written is not a reason to throw it away. Same
    degradation contract as narration and dubbing in the video pipeline.
    """
    try:
        result = x_export.export_bookmarks()
        logger.info("backup refreshed: %s markdown file(s)", result["markdown"])
    except Exception:
        logger.warning("could not refresh the bookmarks backup", exc_info=True)


@app.get("/x/bookmarks/{id}")
async def get_x_bookmark(id: int):
    """One bookmark with every column, including the ones the listing leaves out."""
    import x_bookmarks as xb
    bm = xb.get_bookmark(id)
    if bm is None:
        raise HTTPException(404, "Bookmark not found")
    return bm


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

    logger.info("x-download worker started for bookmark %s (%s)", bm_id, tweet_url)
    report = x_progress.reporter("download", bookmark_id=int(bm_id))
    report(message="Descargando video…")

    def report_download(percent=None, size=None, speed=None, message=None):
        """Forward yt-dlp's own progress onto the stream the card listens to."""
        if percent is None:
            report(message=message or "Descargando video…")
            return
        detail = f"{percent:.0f}%"
        if size:
            detail += f" de {size}"
        if speed:
            detail += f" · {speed}"
        report(message=f"Descargando… {detail}", percent=percent, size=size, speed=speed)

    try:
        file_path = _download_video(tweet_url, cookies_path, on_progress=report_download)
    except NoMediaFound:
        # Not a failure: the tweet just isn't a video. Settle it in 'no_media'
        # so it stops looking like a download still pending.
        logger.info("bookmark %s has no video, settling it in no_media", bm_id)
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
        logger.exception("x-download worker failed for bookmark %s", bm_id)
        x_progress.publish(
            {"type": "error", "job": "download", "bookmark_id": int(bm_id), "message": str(e)}
        )
        return

    # db=None so the store owns the commit — passing a connection in leaves the
    # UPDATE uncommitted and the download is silently lost.
    logger.info("bookmark %s downloaded to %s", bm_id, file_path)
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
async def transcribe_x_bookmark(
    id: int,
    background_tasks: BackgroundTasks,
    chapters_json: Optional[str] = Form(None),
    voice: str = Form("male"),
):
    """Run a downloaded bookmark video through the regular transcription pipeline.

    Same route every other source takes: the only thing X does differently is
    where the file comes from, so it takes the same voice and chapter knobs.
    Returns the job id so the UI can jump straight to it, exactly as uploading
    a file does.
    """
    bm = xb.get_bookmark(id)
    if bm is None:
        raise HTTPException(404, "Bookmark not found")
    if not bm.get("local_file_path"):
        raise HTTPException(409, "No local video file — download it first")

    chapters = _parse_chapters(chapters_json)
    voice = _validated_voice(voice)

    title = (bm.get("author") or "X") + " — " + (bm.get("text") or "")[:60]
    job_id = jobs.create_job(f"{title}.mp4", source="x", title=title, url=bm["tweet_url"])
    xb.set_job(id, job_id)

    background_tasks.add_task(
        _transcribe_bookmark_worker, id, job_id, bm["local_file_path"], title, chapters, voice
    )
    return {"ok": True, "job_id": job_id, "message": "Transcription started"}


def _place_for_pipeline(source: Path, destination: Path) -> None:
    """Make the downloaded video available under the job's expected name.

    Hard-linked rather than copied: a measured X video weighed 681 MB, and both
    directories usually hang off DATA_ROOT so the link is free. Across
    filesystems os.link raises EXDEV, and then there is no way around copying.

    CIFS is the reason for the check afterwards: with DATA_ROOT on a network
    share, os.link succeeds but *moves* the file, which silently ate the
    bookmark's download and left it flagged 'downloaded' pointing at nothing.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        os.link(source, destination)
        if not source.exists():
            logger.warning("os.link moved %s instead of linking it; restoring", source)
            shutil.copy2(destination, source)
    except OSError:
        shutil.copy2(source, destination)


def _transcribe_bookmark_worker(
    bm_id: int, job_id: str, video_path: str, title: str, chapters: list[dict], voice: str
) -> None:
    """Hand a bookmark's video to the shared pipeline.

    Deliberately thin: transcription, translation, narration, dubbing and
    chapters all live in run_transcription_job, and a second implementation here
    would only drift from it.
    """
    logger.info("x-transcribe worker started for bookmark %s (job %s)", bm_id, job_id)
    report = x_progress.reporter("transcribe", bookmark_id=int(bm_id))
    report(message="Preparando el video…", job_id=job_id)

    local_path = UPLOAD_DIR / media_names.media_name(job_id, title, ".mp4")
    try:
        _place_for_pipeline(Path(video_path), local_path)
    except OSError as e:
        logger.exception("could not stage %s for job %s", video_path, job_id)
        jobs.update_job(job_id, status="failed", error=_error_message("500 staging failed", e))
        x_progress.publish(
            {"type": "error", "job": "transcribe", "bookmark_id": int(bm_id), "message": str(e)}
        )
        return

    # source_language=None: a tweet can be in any language, and the pipeline
    # already skips translation and dubbing when it detects Spanish.
    run_transcription_job(
        job_id, local_path, f"{title}.mp4", chapters, voice,
        source_language=None, source="x", url=None,
    )

    finished = jobs.get_job(job_id) or {}
    status = "done" if finished.get("status") == "done" else "failed"
    # 'source_language' is the key the pipeline actually writes (see the result
    # dict in run_transcription_job); reading 'language' silently stored None.
    language = (finished.get("result") or {}).get("source_language")
    xb.mark_transcribed(bm_id, status, language=language)
    logger.info("x-transcribe worker finished for bookmark %s (job %s)", bm_id, job_id)
    x_progress.publish(
        {
            "type": "done",
            "job": "transcribe",
            "bookmark_id": int(bm_id),
            "job_id": job_id,
            "message": "Transcripción completada",
        }
    )


if __name__ == "__main__":
    # Opt-in, not the default: reload restarts the server whenever a file is
    # saved, and that kills any job in flight -- these run for tens of minutes.
    uvicorn.run(
        "app:app", host="0.0.0.0", port=8000,
        reload=os.getenv("RELOAD") == "1",
    )
