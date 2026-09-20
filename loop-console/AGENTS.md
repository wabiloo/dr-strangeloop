# Agent reference — loop-console (Dr. Strangeloop UI)

Web console (FastAPI + Vue 3) that lets a human or agent define
`franken-ts` content/ad-break configs, deploy/manage `its-a-live`
channels (either backend), and monitor them -- a UI on top of the same
CLIs described in the repo-root [`AGENTS.md`](../AGENTS.md), not a
replacement for them. See [`README.md`](./README.md) for layout/run
instructions.

## What this does NOT replace

Every operation this console performs ultimately shells out to the same
tools an agent would use directly: `franken-ts <config.yaml>`,
`its-a-live/channel.py <cmd>`, and `cdk deploy` (via `channel.py create`/
`redeploy`). It adds:

- a visual editor for franken-ts YAML, schema-driven from
  `franken_ts.config.Config.model_json_schema()` (so it can't drift from
  what franken-ts actually validates),
- a background job runner so long operations (build, deploy, start/stop)
  don't block an HTTP request, with a pollable status+log endpoint,
  and a `/health` proxy to loop-dee-loop's `serve.py` for live loop
  position on `ecs-express` channels.

If you're an agent asked to do a one-off content/deploy task via the CLI
directly, prefer the per-tool `AGENTS.md` files (`franken-ts/AGENTS.md`,
`its-a-live/AGENTS.md`, `loop-dee-loop/AGENTS.md`) -- this project exists
for the interactive/monitoring use case, not to be scripted against by
another agent unless you're specifically building on its HTTP API.

## API surface (backend)

| Prefix | Purpose |
|---|---|
| `GET/PUT/DELETE /api/v1/configs/*` | franken-ts YAML CRUD + `/schema` (JSON Schema) + `/build` (spawns a job) |
| `GET/POST/DELETE /api/v1/channels/*` | its-a-live TOML CRUD, `/status`, `/outputs`, `/health` (ecs-express), and job-spawning `/create`, `/spark`, `/start`, `/stop`, `/refresh`, `/redeploy` |
| `GET /api/v1/jobs/*` | poll job status/log (`?log_offset=` for incremental tailing) |

See `src/loop_console/app/routes/*.py` for the authoritative request/response
shapes, mirrored in `frontend/src/api/types.ts`.

## Known gaps (not yet built)

- No `cdk destroy` wiring (only create/redeploy) -- tearing down a
  channel's AWS stack still needs the CLI directly.
- No log tailing (CloudWatch Logs) yet -- job logs only cover the
  subprocess's own stdout/stderr, not the deployed ECS task's/MediaLive
  channel's runtime logs.
- No auth -- do not expose this off localhost without adding some.
- Job history is in-memory only; restarting the backend loses it (jobs
  already running are orphaned, not resumed).
