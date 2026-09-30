# Agent reference — igor (Dr. Strangeloop UI)

Web console (FastAPI + Vue 3) that lets a human or agent define
`franken-ts` playlists (content/ad-break definitions), deploy/manage `its-a-live`
channels (either backend), and monitor them -- a UI on top of the same
CLIs described in the repo-root [`AGENTS.md`](../AGENTS.md), not a
replacement for them. See [`README.md`](./README.md) for layout/run
instructions.

## What this does NOT replace

Every operation this console performs ultimately shells out to the same
tools an agent would use directly: `franken-ts <config.yaml>`,
`its-a-live/channel.py <cmd>`, and `cdk deploy`/`cdk destroy` (via
`channel.py create`/`redeploy`/`terminate`). It adds:

- a visual editor for franken-ts YAML, schema-driven from
  `franken_ts.config.Config.model_json_schema()` (so it can't drift from
  what franken-ts actually validates),
- a background job runner so long operations (build, deploy, start/stop)
  don't block an HTTP request, with a pollable status+log endpoint,
  and a `/health` proxy to loop-dee-loop's `serve.py` for live loop
  position on `ecs-express` channels.

## Pages

Playlists (franken-ts YAML editor), Archives (HAR/Proxyman import wizard,
`/archives`), **Manifests** (`/manifests`: save a VOD HLS/DASH manifest URL,
inspect its rendition ladder, pick renditions/audio, download it as a
segment list via `grave-robber ingest-url`), and Channels. A channel's
`[input] source_kind` is `playlist`, `archive` or `manifest`; the latter two
both point `source_path` at a grave-robber `manifest.json` (a segment-list
manifest, baked with the source's own segment durations).

If you're an agent asked to do a one-off content/deploy task via the CLI
directly, prefer the per-tool `AGENTS.md` files (`franken-ts/AGENTS.md`,
`its-a-live/AGENTS.md`, `loop-dee-loop/AGENTS.md`) -- this project exists
for the interactive/monitoring use case, not to be scripted against by
another agent unless you're specifically building on its HTTP API.

## API surface (backend)

| Prefix | Purpose |
|---|---|
| `GET/PUT/DELETE /api/v1/playlists/*` | franken-ts YAML playlist CRUD + `/schema` (JSON Schema) + `/duplicate` (copy under a new name) + `/build` (spawns a job) |
| `GET/POST/DELETE /api/v1/manifests/*` | VOD manifest-URL sources (separate from `/archives`: no capture/coverage/range picker). `POST /` saves a URL (`data/manifests/<name>.json`), `POST /inspect` fetches just the manifest and returns its rendition ladder, `POST /{name}/import` spawns `grave-robber ingest-url` (`renditions`: `all`/`best`/`#1,#3`, `audio`, `allow_missing_segments`) into `outputs/manifests/<name>/`, `GET /{name}/import/status` summarises the result. Channels reference it with `source_kind = "manifest"` |
| `GET/POST/DELETE /api/v1/channels/*` | its-a-live TOML CRUD (igor-only, no `channel.py` equivalent) |
| ↳ Infrastructure: `/create`, `/redeploy`, `/terminate` (job-spawning), `/outputs` | does the stack/container exist -- `/redeploy`, `/terminate` and `/outputs` are no-ops/unavailable for local-docker channels (no stack); igor's UI hides all three for local-docker. No `/list` -- igor lists channels from its local TOML store directly, not by shelling out to `channel.py list` |
| ↳ Stream: `/spark`, `/start`, `/stop`, `/refresh`, `/update` (job-spawning), `/status` | is content actually playing -- available for every backend. `/update` is `/spark`+`/refresh` combined (channel.py's `update`); igor's UI shows it instead of a separate Spark/Refresh pair once the channel is running, and plain `/spark` otherwise (staging never depends on deploy state) |
| ↳ `/health` (ecs-express) | loop-dee-loop `serve.py` proxy, not a `channel.py` command |
| ↳ `GET/POST /api/v1/channels/{name}/schedule`, `DELETE .../schedule/{window_id}` | scheduled on-air windows (`channel.py schedule list/add/remove`) -- aws-media/ecs-express only, hidden in igor's UI for local-docker. `POST` is job-spawning (an immediate window also runs `start`); `GET`/`DELETE` are synchronous (fast EventBridge Scheduler API calls). Requires `ItsALiveSharedStack-scheduler` deployed once per account/region -- see `its-a-live/README.md`'s "Scheduling" |
| `GET /api/v1/jobs/*` | poll job status/log (`?log_offset=` for incremental tailing) |

See `src/igor/app/routes/*.py` for the authoritative request/response
shapes, mirrored in `frontend/src/api/types.ts`.

## Known gaps (not yet built)

- No log tailing (CloudWatch Logs) yet -- job logs only cover the
  subprocess's own stdout/stderr, not the deployed ECS task's/MediaLive
  channel's runtime logs.
- No auth -- do not expose this off localhost without adding some.
- Job history is in-memory only; restarting the backend loses it (jobs
  already running are orphaned, not resumed).
