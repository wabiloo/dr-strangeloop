# loop-console (Dr. Strangeloop UI)

Web console (FastAPI + Vue 3), branded **Dr. Strangeloop**, to define,
launch, and monitor `dr-strangeloop` channels: author `franken-ts`
content/ad-break YAML, deploy via `its-a-live` (either backend), and
monitor running channels (status, playback URLs, logs, and — for
`ecs-express` channels — live loop position via `loop-dee-loop`'s
`serve.py` `/health` endpoint).

See the repo-root [`AGENTS.md`](../AGENTS.md) for how this fits into the
overall pipeline, and [`AGENT_BRIEF.md`](./AGENT_BRIEF.md) *(TODO)* for
this project's own architecture rationale.

## Layout

```
loop-console/
  src/loop_console/       # FastAPI backend
    app/                   # app assembly, routes
    jobs/                  # background job runner wrapping long CLI/CDK operations
    store/                 # channel-definition persistence
    integrations/          # thin wrappers around franken-ts / its-a-live / loop-dee-loop
  frontend/                # Vue 3 + Vite + TS + PrimeVue SPA
  server.py                # uvicorn entrypoint
```

## Requirements

- Repo-root `.venv` (`uv sync --all-packages` from the repo root) — this
  project is a workspace member, so it shares that environment and can
  import `franken_ts` directly (used to derive the content-editor's JSON
  Schema from `franken_ts.config`, so the two never drift).
- `its-a-live`'s own separate venv (`cd its-a-live && uv sync`) must exist
  — the backend shells out to `its-a-live/channel.py` using that venv's
  Python, exactly like `loop-dee-loop`'s ops modules do.
- `aws` CLI v2, `cdk` CLI v2 (Node.js), Docker, GPAC/ffmpeg/tsduck — same
  external dependencies `its-a-live`/`loop-dee-loop`/`franken-ts` need,
  since this console only orchestrates those tools, it doesn't replace
  their runtime requirements.
- Node.js/npm for the frontend.

## Running (dev)

```bash
# Backend (from repo root, using the shared workspace venv):
uv run --project loop-console python loop-console/server.py

# Frontend (separate terminal):
cd loop-console/frontend
npm install
npm run dev
```

Vite proxies `/api` to the backend (see `frontend/vite.config.ts`).

## Running (prod)

```bash
cd loop-console/frontend && npm run build   # -> frontend/dist/
uv run --project loop-console python loop-console/server.py
```

FastAPI serves the built SPA at `/admin/` and the API under `/api/v1/`
from the same process/port — no CORS needed.
