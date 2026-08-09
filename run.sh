#!/usr/bin/env bash
# Launches backend (FastAPI, venv) and frontend (Vite) together.
# Ctrl+C stops both.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
VENV_DIR="$BACKEND_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating backend venv at $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -r "$BACKEND_DIR/requirements.txt"
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "Installing frontend dependencies..."
  (cd "$FRONTEND_DIR" && npm install)
fi

cleanup() {
  echo "Stopping backend and frontend..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd "$BACKEND_DIR" && source "$VENV_DIR/bin/activate" && python app.py) &
BACKEND_PID=$!

(cd "$FRONTEND_DIR" && npm run dev --host) &
FRONTEND_PID=$!

echo "Backend (PID $BACKEND_PID) → http://localhost:8000"
echo "Frontend (PID $FRONTEND_PID) → http://localhost:5173"

wait "$BACKEND_PID" "$FRONTEND_PID"
