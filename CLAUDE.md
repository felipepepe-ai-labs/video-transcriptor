# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Video Transcriptor EN → ES: a two-process app with no shared build.

```
Frontend (React 19 + Vite, :5173) → Backend (FastAPI, :8000) → SSH → 192.168.1.60 (Whisper large-v3-turbo, Piper TTS)
                                                                              ↓                    ↓
                                                                    MyMemory Translate API   Ollama (qwen3.6:27b, EN→ES fallback)
                                                                       (EN→ES, primary)
```

- **`backend/app.py`** holds only the FastAPI routes and the background worker (`run_transcription_job`) that drives one job through `uploading → transcribing → translating → voicing → dubbing → splitting → done`. It reuses `parse_srt`, `assign_chapters`, `ts_to_seconds`, `seconds_to_ts` (kept here, unchanged in behavior). Narration, dubbing, and chapter-splitting are best-effort bonuses on top of transcript+translation: a failure in any of them (`TTSFailed`/`dub.DubbingFailed`) is recorded on the job result (`*_error` fields) but never fails the job.
- **`backend/remote.py`** — `RemoteWhisper` wraps the whole paramiko SSH lifecycle for one job: connect (mapping both `paramiko.SSHException` and `OSError`/`NoValidConnectionsError` to a single `RemoteUnavailable`), a pre-flight disk check on remote `/tmp` (`InsufficientRemoteStorage` if free space < file size × `REMOTE_DISK_SAFETY_MARGIN`), upload, `run_transcribe()` (runs `python3 -m whisper --task transcribe`, `TranscriptionFailed` on non-zero exit or missing SRT), `run_tts_batch()` (runs Piper over the same SSH connection to synthesize one WAV per translated segment, chunked to amortize Piper's model-load cost, `TTSFailed` on failure), and a guaranteed `cleanup()` (`rm -f`/`rm -rf` of every uploaded file + output dir) called from the worker's `finally` on every exit path. There is no `run_translate()` — Whisper's `--task translate` only ever translates speech INTO English, so it cannot serve as an EN→ES path.
- **`backend/translate.py`** — `Translator` protocol with `MyMemoryTranslator` (primary; retries with backoff, batches segments under MyMemory's ~500-char query cap) and `OllamaTranslator` (fallback; batch-translates via a numbered-list prompt against a local/remote Ollama model, degrading to one segment at a time if the batch response doesn't parse cleanly). `translate_with_fallback()` tries MyMemory first, then Ollama, then gives up and returns the untranslated text (`provider` = `"untranslated"`) rather than failing the job — the provider actually used is always recorded (`result.translation_provider`), so a degraded translation is never silent.
- **`backend/dub.py`** — builds the Spanish narration track and dubbed video, all locally via `ffmpeg`/stdlib `wave` (only the TTS synthesis itself runs remotely, via `RemoteWhisper.run_tts_batch()`). `build_narration_track()` lays out per-segment Piper clips sequentially with silence gaps sized to each subtitle's timestamp — segments are not time-stretched to fit their slot, so a long segment pushes the rest of the track forward rather than overlapping audio. `mux_audio_into_video()` muxes that track onto the original video (video stream copied, audio re-encoded to AAC). `split_video_by_chapters()` cuts the dubbed video into one clip per chapter. Any `ffmpeg` failure raises `DubbingFailed`.
- **`backend/jobs.py`** — SQLite-backed job store (`backend/jobs.db`, stdlib `sqlite3`) with `create_job`/`update_job`/`get_job`/`list_jobs`/`delete_job`. Survives the dev server's `reload=True` restarts, unlike an in-memory dict.
- **`backend/retranslate_job.py`** — one-off maintenance script (not imported by the app) to re-translate a job's stored segments in place when `translation_provider` shows a degraded/broken result, e.g. jobs produced before the Whisper-translate fallback was replaced with Ollama. Tracks per-segment progress via a `retranslated` marker so a long run against Ollama is resumable and inspectable mid-flight.
- Chapter markers (optional, user-supplied `chapters_json` on upload) are matched to transcript segments by nearest-preceding timestamp in `assign_chapters()`.
- Endpoints: `POST /jobs` (multipart: `video` file + optional `chapters_json` + optional `voice` = `"male"`/`"female"`, default `"male"`) enqueues a job and returns `{job_id, status}` immediately. `GET /jobs` lists all jobs. `GET /jobs/{id}` returns the job's `status`/`stage`/`error`/`result`. `DELETE /jobs/{id}` removes the job row and its local audio/video artifacts. `GET /jobs/{id}/audio` / `GET /jobs/{id}/video` / `GET /jobs/{id}/chapters/{index}/video` stream the narration WAV, dubbed MP4, and per-chapter clips respectively (404 if that stage didn't complete). `GET /health` reports remote host/model config.
- Config (`REMOTE_HOST`, `REMOTE_USER`, `REMOTE_MODEL`, `REMOTE_DEVICE`, `REMOTE_DISK_SAFETY_MARGIN`, `FRONTEND_ORIGIN`, `OLLAMA_URL`, `OLLAMA_MODEL`, `MYMEMORY_EMAIL`) all come from `os.getenv` with the previous hardcoded values as defaults.
- **`frontend/src/App.jsx`** is a single-component React app: upload UI submits to `POST /jobs`, then polls `GET /jobs/{id}` every ~2s rendering the current stage until `done`/`failed`; same EN/ES tab toggle and `.srt` export as before. Calls the backend via `VITE_API_URL` (set in `frontend/.env`).
- There is no auth beyond CORS restricted to `FRONTEND_ORIGIN`, and no persistence beyond `backend/uploads/`, `backend/audio/`, `backend/video/`, and `backend/jobs.db` (local scratch, cleaned up remotely but not locally) — treat this as a personal utility tool, not a multi-user service.

## Commands

```bash
# Backend — from backend/
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py                    # → http://localhost:8000 (reload=True)

# Frontend — from frontend/
npm install
npm run dev                      # → http://localhost:5173
npm run build
npm run lint                     # oxlint
```

Backend tests: `cd backend && source .venv/bin/activate && pytest -v` (`backend/tests/test_jobs_e2e.py`, SSH boundary mocked). No test suite exists for the frontend.

## Requirements

- Backend: Python 3.12+, and passwordless SSH key auth to `felipe@192.168.1.60` (the remote box must already have `whisper` installed and a GPU available).
- Frontend: Node 22+.
