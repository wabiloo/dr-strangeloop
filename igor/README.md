# igor (Dr. Strangeloop UI)

Web console (FastAPI + Vue 3), branded **Dr. Strangeloop**, to define,
launch, and monitor `dr-strangeloop` channels: author `franken-ts`
content/ad-break YAML, deploy via `its-a-live` (either backend), and
monitor running channels (status, playback URLs, logs, and — for
`ecs-express` channels — live loop position via `loop-dee-loop`'s
`serve.py` `/health` endpoint).

See the repo-root [`AGENTS.md`](../AGENTS.md) for how this fits into the
overall pipeline, and [`AGENT_BRIEF.md`](./AGENT_BRIEF.md) *(TODO)* for
this project's own architecture rationale.

## Documentation

The **Docs** entry in the header serves all of the repository's documentation
(guides, per-tool references, CLI cheatsheet) plus interactive API docs for
Igor (`/api/docs`) and for the channel API. See `AGENTS.md` for how to add a page.

## Layout

```
igor/
  src/igor/               # FastAPI backend
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

## Running via Docker (no toolchain install)

To hand this to a colleague who has Docker but doesn't want to install
Python/uv/Node/ffmpeg/tsduck/GPAC/aws-cli/cdk locally, see the repo-root
[`DOCKER_LOCAL.md`](../DOCKER_LOCAL.md) + [`docker-compose.yml`](../docker-compose.yml).
Different from [`CLOUD_DEPLOYMENT.md`](./CLOUD_DEPLOYMENT.md) below -- that
one is about hosting igor somewhere shared/reachable over a network, this
is about running it locally in a container instead of installing the
toolchain by hand.

## Running (dev)

```bash
./dev.sh
```

Starts both the backend (uvicorn, reload) and frontend (vite) and stops
both on Ctrl-C. Equivalent to running them separately:

```bash
# Backend (from repo root, using the shared workspace venv):
uv run --project igor python igor/server.py

# Frontend (separate terminal):
cd igor/frontend
npm install
npm run dev
```

Vite proxies `/api` to the backend (see `frontend/vite.config.ts`).

## Running (prod)

```bash
cd igor/frontend && npm run build   # -> frontend/dist/
uv run --project igor python igor/server.py
```

FastAPI serves the built SPA at `/admin/` and the API under `/api/v1/`
from the same process/port — no CORS needed.
