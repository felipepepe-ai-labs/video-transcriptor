import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import dub
import jobs
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


def run_transcription_job(
    job_id: str, local_path: Path, filename: str, chapters: list[dict], voice: str
) -> None:
    """Runs the full transcribe -> translate lifecycle for one job, updating
    job stage/status as it progresses. Any domain error is caught and stored
    on the job record (status=failed, error=<message>) since a background
    task has no HTTP response to raise into."""
    jobs.update_job(job_id, status="running", stage="uploading")
    remote = RemoteWhisper()

    try:
        try:
            file_size = local_path.stat().st_size
            remote.connect()
            remote.check_disk_space(file_size)
            remote.upload(local_path, filename)

            jobs.update_job(job_id, stage="transcribing")
            srt_content, duration = remote.run_transcribe()

            segments = parse_srt(srt_content)
            assign_chapters(segments, chapters)

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

            # ── Narration: one Piper clip per segment, timed to that
            # segment's own timestamp (not one continuous pass over
            # full_text_es) -- this is what makes muxing onto the video
            # possible in the next stage.
            jobs.update_job(
                job_id, stage="voicing", progress=0.0,
                segments_done=0, segments_total=len(segments),
            )

            def report_tts_progress(done: int, total: int) -> None:
                jobs.update_job(
                    job_id, segments_done=done, segments_total=total,
                    progress=round(done / total * 100, 1) if total else 0.0,
                )

            narration_track = None
            try:
                segment_wavs = remote.run_tts_batch(texts_es, voice=voice, on_progress=report_tts_progress)
                segment_starts = [ts_to_seconds(s["start"]) for s in segments]
                work_dir = VIDEO_DIR / job_id / "work"
                narration_track = dub.build_narration_track(
                    segment_wavs, segment_starts, total_duration=duration, work_dir=work_dir
                )
                (AUDIO_DIR / f"{job_id}.wav").write_bytes(narration_track.read_bytes())
                result["audio_available"] = True
            except (TTSFailed, dub.DubbingFailed) as e:
                # Narration/dubbing are bonuses on top of transcript+
                # translation -- don't fail a perfectly good job over them.
                result["audio_error"] = str(e)

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


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": os.getenv("REMOTE_MODEL", "large-v3-turbo"),
        "remote_host": os.getenv("REMOTE_HOST", "192.168.1.60"),
        "device": os.getenv("REMOTE_DEVICE", "cuda"),
    }


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
