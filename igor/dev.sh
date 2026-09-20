#!/usr/bin/env bash
# Start the igor backend (uvicorn, reload) and frontend (vite) dev servers together.
set -euo pipefail

cd "$(dirname "$0")"

cleanup() {
  kill $(jobs -p) 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run --project . python server.py &
(cd frontend && npm run dev) &

wait
