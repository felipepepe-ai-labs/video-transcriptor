# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Video Transcriptor EN → ES: a two-process app with no shared build.

```
Frontend (React 19 + Vite, :5173) → Backend (FastAPI, :8000) → SSH → 192.168.1.60 (Whisper large-v3-turbo)
                                                                              ↓
                                                                    MyMemory Translate API (EN→ES)
```

- **`backend/app.py`** holds only the FastAPI routes and the background worker (`run_transcription_job`) that drives one job through `uploading → transcribing → translating → done`. It reuses `parse_srt`, `assign_chapters`, `ts_to_seconds`, `seconds_to_ts` (kept here, unchanged in behavior).
- **`backend/remote.py`** — `RemoteWhisper` wraps the whole paramiko SSH lifecycle for one job: connect (mapping both `paramiko.SSHException` and `OSError`/`NoValidConnectionsError` to a single `RemoteUnavailable`), a pre-flight disk check on remote `/tmp` (`InsufficientRemoteStorage` if free space < file size × `REMOTE_DISK_SAFETY_MARGIN`), upload, running `python3 -m whisper` for a given task (`transcribe` or `translate`, `TranscriptionFailed` on non-zero exit or missing SRT), and a guaranteed `cleanup()` (`rm -f`/`rm -rf`) called from the worker's `finally` on every exit path — fixing the old leak where a failed run left the uploaded video + output dir on the remote box forever. `run_translate()` reuses the already-uploaded file, no second upload.
- **`backend/translate.py`** — `Translator` protocol with `MyMemoryTranslator` (primary; retries with backoff, batches segments under MyMemory's query length cap) and `WhisperTranslateFallback` (runs `RemoteWhisper.run_translate()` on the same uploaded file when MyMemory fails). `translate_with_fallback()` tries MyMemory first and returns which provider was actually used (`result.translation_provider`), so a degraded translation is never silent.
- **`backend/jobs.py`** — SQLite-backed job store (`backend/jobs.db`, stdlib `sqlite3`) with `create_job`/`update_job`/`get_job`. Survives the dev server's `reload=True` restarts, unlike an in-memory dict.
- Chapter markers (optional, user-supplied `chapters_json` on upload) are matched to transcript segments by nearest-preceding timestamp in `assign_chapters()`.
- Endpoints: `POST /jobs` (multipart: `video` file + optional `chapters_json`) enqueues a job and returns `{job_id, status}` immediately — it does not block on transcription. `GET /jobs/{id}` returns the job's `status`/`stage`/`error`/`result`. `GET /health` reports remote host/model config.
- Config (`REMOTE_HOST`, `REMOTE_USER`, `REMOTE_MODEL`, `REMOTE_DEVICE`, `REMOTE_DISK_SAFETY_MARGIN`, `FRONTEND_ORIGIN`) all come from `os.getenv` with the previous hardcoded values as defaults.
- **`frontend/src/App.jsx`** is a single-component React app: upload UI submits to `POST /jobs`, then polls `GET /jobs/{id}` every ~2s rendering the current stage until `done`/`failed`; same EN/ES tab toggle and `.srt` export as before. Calls the backend via `VITE_API_URL` (set in `frontend/.env`).
- There is no auth beyond CORS restricted to `FRONTEND_ORIGIN`, and no persistence beyond `backend/uploads/` + `backend/jobs.db` (local scratch, cleaned up remotely but not locally) — treat this as a personal utility tool, not a multi-user service.

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
