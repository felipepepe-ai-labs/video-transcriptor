# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- FastAPI backend with async job model: `POST /jobs` enqueues a transcription job and returns immediately, `GET /jobs/{id}` polls status through `uploading → transcribing → translating → done`.
- Remote transcription via SSH to a GPU box running Whisper (`large-v3-turbo`), with a pre-flight disk-space check and guaranteed remote cleanup on every exit path.
- EN→ES translation via MyMemory API (batched, with retries), with automatic fallback to a second Whisper pass (`--task translate`) on the same uploaded file if MyMemory fails.
- SQLite-backed job store (`backend/jobs.db`) that survives dev-server reloads.
- React 19 + Vite frontend: upload UI, live polling, EN/ES tab toggle, `.srt` export.
- Backend test suite (`backend/tests/test_jobs_e2e.py`) with the SSH boundary mocked.
