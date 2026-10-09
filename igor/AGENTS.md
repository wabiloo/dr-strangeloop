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

**Docs** (`/docs`, header menu): renders the repo's Markdown on demand and
links the API docs. The catalog (which files, titles, grouping) is
`src/igor/integrations/docs.py` -- **add a new `.md` there to make it appear**;
`tests/test_docs.py` fails if a catalogued file is missing. Rendering
(`frontend/src/utils/markdown.ts`, markdown-it with raw HTML off) rewrites
relative links between catalogued pages to in-app routes and shows links to
anything else (source files, folders) as plain paths. The new overview, Igor tour
and CLI/API cheatsheet live in the repo-root `docs/` folder; keep the CLI
cheatsheet in step with the tools' `--help` when flags change.

If you're an agent asked to do a one-off content/deploy task via the CLI
directly, prefer the per-tool `AGENTS.md` files (`franken-ts/AGENTS.md`,
`its-a-live/AGENTS.md`, `loop-dee-loop/AGENTS.md`) -- this project exists
for the interactive/monitoring use case, not to be scripted against by
another agent unless you're specifically building on its HTTP API.

## Startover & catchup (channel detail page)

For `ecs-express` / `local-docker` channels with `[timeshift]` enabled (the
default), a running channel's page shows a **Startover & catchup** panel under
the players (**collapsed by default**, open/closed remembered in the browser; while a
preview is active the collapsed header summarises it and offers *Back to live*): pick a start (and optional end) in UTC or local time, presets
(last N minutes, previous/current loop), *whole loops only*, and a timeline
override (`timeline=default|continuous|periodic`, fixed name, also usable on
live URLs by leaving the start empty), and *Pretend “now” is…* (`offset=-PT1H` /
`-3600`, fixed name, negative or positive, with presets; plays the live edge as it
was/will be, and start/end are judged against that moment). The offset can also be
given as the **datetime “now” should pretend to be**: Igor computes `offset =
datetime − moment of Preview/Copy` (whole seconds) and freezes it in the URL, so the
stream then advances in real time from that instant; it builds the HLS and DASH URLs (param names from the channel's
`[timeshift]` config, values as ISO 8601 / epoch s / epoch ms), validates them
the way `serve.py` will, lets you copy them, and **Preview** swaps them into
the HLS/DASH players (which then start from the beginning of the range rather
than the live edge; *Back to live* restores). URL building/validation lives
in `frontend/src/utils/timeshift.ts` (tests: `node --test
--experimental-strip-types src/utils/timeshift.test.ts`). The per-channel
settings are in the New/Edit forms' "Timeshift" group
(`timeshift_*` fields → `[timeshift]`). The DASH playhead clock shown during
a preview is approximate. Design: `loop-dee-loop/SCOPE.md` §13.

## Channel epoch (New/Edit forms, "Timeline" group)

`ecs-express` / `local-docker` channels have a **Channel epoch (UTC)**
(`[timeline] epoch_utc`, API field `epoch_utc`): loop 0's start and the DASH
`availabilityStartTime`. A UTC date-time picker with three presets -- **Unix
epoch (1970)**, **1 Jan 2026** (the default) and **Now** (fills in the current
UTC time as a fixed value, not a moving one) -- stored as
`YYYY-MM-DDTHH:MM:SSZ`, the only form `serve.py --epoch-utc` accepts (the API
rejects anything else). A recent epoch keeps loop numbers, media sequence
numbers and Period ids small. It is applied on `redeploy` (local-docker:
redeploy/refresh), not by a plain `start`; changing it on a running channel
restarts the numbering. Helpers/tests: `frontend/src/utils/epoch.ts` (`node
--test --experimental-strip-types src/utils/epoch.test.ts`), component
`EpochFields.vue`.

## Channel config sections ↔ TOML tables (keep these in lockstep)

The New channel form, the read-only Configuration panel and the Configuration
edit form (channel detail page) share one layout,
`frontend/src/utils/channelConfigLayout.ts`, and **every section in it is
exactly one its-a-live TOML table, named after it**:

| TOML table | UI group | Holds |
|---|---|---|
| `[deploy]` | Deploy | `name`, `backend` (New form only; the detail page shows the backend as a pill) |
| `[input]` | Input | `source_kind`, `source_path`, `allow_missing_segments` |
| `[infrastructure.aws]` | Infrastructure · AWS | `region` (not local-docker) |
| `[infrastructure.s3]` | Infrastructure · S3 | `bucket_name`, `content_folder` (not local-docker) |
| `[infrastructure.express]` / `[infrastructure.docker]` | Infrastructure · Express / Docker | `port` (+ `cpu`, `memory`, `cdn` for express) |
| `[timeline]` | Timeline | `epoch_utc`, `continuous` |
| `[packaging]` | Packaging | `segment_duration`, `dvr_window_seconds`, `hls_format`, `hls_ts_mux_audio`, `dash_addressing` |
| `[timeshift]` | Timeshift | `enabled`, `start_param`, `end_param`, `max_span_seconds` |
| `[markers]` | Markers | `daterange_mode`, `cue_tags`, `dash_*`, `increment_event_ids`, `daterange_id_format`, `period_on_segmentation(_apply)` |

Rules, for any change that touches channel config:

- A group title is the table path, capitalised (`[timeline]` → "Timeline", `[infrastructure.aws]` → "Infrastructure · AWS"). Do
  not invent friendlier group names ("Serving", "HLS packaging", "SCTE-35
  signaling" were retired for this reason) -- field *labels* can be friendly,
  group titles cannot.
- A setting is shown in the group of the table it is stored in, in all three
  places (create form, read-only panel, edit form) and in the same order. If a
  setting looks like it belongs in another group, move it in the TOML
  (generator, reader, docs, tests) -- do not just display it elsewhere.
- Adding a table means adding its section (id = table name) to
  `CONFIG_SECTION_TITLE`, `buildConfigSections`, and both Vue templates
  (`ChannelNew.vue`, `ChannelDetail.vue` edit form) together. Renaming a
  table or key means the same plus `its-a-live/` readers, `generate_toml`
  (`src/igor/integrations/its_a_live.py`), `its-a-live/config.toml`,
  `its-a-live/README.md`, `its-a-live/AGENTS.md`, every `data/channels/*.toml`
  and `its-a-live/configs/*.toml`, and the tests, in one change. The form/API
  field names equal the TOML key names.

## API surface (backend)

| Prefix | Purpose |
|---|---|
| `GET/PUT/DELETE /api/v1/playlists/*` | franken-ts YAML playlist CRUD + `/schema` (JSON Schema) + `/duplicate` (copy under a new name) + `/build` (spawns a job) |
| `GET/POST/DELETE /api/v1/manifests/*` | VOD manifest-URL sources (separate from `/archives`: no capture/coverage/range picker). `POST /` saves a URL (`data/manifests/<name>.json`), `POST /inspect` fetches just the manifest and returns its rendition ladder, `POST /{name}/import` spawns `grave-robber ingest-url` (`renditions`: `all`/`best`/`#1,#3`, `audio`, `allow_missing_segments`) into `outputs/manifests/<name>/`, `GET /{name}/import/status` summarises the result. Channels reference it with `source_kind = "manifest"` |
| `GET/POST/DELETE /api/v1/channels/*` | its-a-live TOML CRUD (igor-only, no `channel.py` equivalent) |
| ↳ Infrastructure: `/create`, `/redeploy`, `/terminate` (job-spawning), `/outputs` | does the stack/container exist -- `/redeploy`, `/terminate` and `/outputs` are no-ops/unavailable for local-docker channels (no stack); igor's UI hides all three for local-docker. No `/list` |
| ↳ `GET /api/v1/channels/` (+ `?live=false`), `GET .../{name}/summary` | the channel table. `?live=false` is instant (local TOML only: name, backend, source); `/{name}/summary` runs `channel.py list <that config>` (stack/live state + `reachable`, the slow part). The UI fetches the quick list, then every summary in parallel and fills rows in as they arrive; a row without a resolved summary is never deletable or startable. Plain `GET /` still returns the full list in one (slow) call |
| ↳ Stream: `/spark`, `/start`, `/stop`, `/refresh`, `/update` (job-spawning), `/status` | is content actually playing -- available for every backend. `/update` is `/spark`+`/refresh` combined (channel.py's `update`); igor's UI shows it instead of a separate Spark/Refresh pair once the channel is running, and plain `/spark` otherwise (staging never depends on deploy state) |
| ↳ `/health` (ecs-express) | loop-dee-loop `serve.py` proxy, not a `channel.py` command |
| ↳ `GET .../{name}/timeline`, `.../docs`, `.../openapi.yaml` | proxies of `serve.py`'s `/timeline.json` (query params forwarded; drawn by the **Timeline** panel, `WindowPanel.vue`), `/docs` (HTML API docs, opened by the panel's **API docs** button) and `/openapi.yaml` (what `/docs` loads, via a relative URL) -- ecs-express / local-docker only |
| ↳ `GET/POST /api/v1/channels/{name}/schedule`, `DELETE .../schedule/{window_id}` | scheduled on-air windows (`channel.py schedule list/add/remove`) -- aws-media/ecs-express only, hidden in igor's UI for local-docker. `POST` is job-spawning (an immediate window also runs `start`); `GET`/`DELETE` are synchronous (fast EventBridge Scheduler API calls). Requires `ItsALiveSharedStack-scheduler` deployed once per account/region -- see `its-a-live/README.md`'s "Scheduling" |
| `GET /api/v1/docs/`, `.../pages/{slug}`, `.../channel-api/docs`, `.../channel-api/openapi.yaml` | documentation hub: catalog, Markdown of a catalogued page (only catalogued slugs are served), and the channel API spec (`loop-dee-loop/openapi.yaml`) with a ReDoc viewer that needs no running channel |
| `GET /api/docs`, `/api/redoc`, `/api/openapi.json` | Igor's own generated OpenAPI docs. Deliberately under `/api` (not FastAPI's default `/docs`) so the one `/api` proxy rule in Vite dev and ingress covers them |
| `GET /api/v1/playback-test/info`, `POST .../setup`, `GET/POST .../channels/{name}`, `GET .../channels/{name}/{run_id}[/{file}.png]` | **Playback test** panel (`PlaybackTestPanel.vue`, headless mode). `info` reports installed player SDKs/Chrome/ffmpeg; `setup` spawns `player-lab setup` (job); `POST channels/{name}` spawns `player-lab run --channel <toml> --out-dir outputs/player-lab/channels/<name>/<run_id>` (job + `run_id`; body: `players`, `formats`, `boundaries`, `duration_s`, `max_seconds`, `ffmpeg_s`); `GET channels/{name}` lists past run summaries + the active job; `{run_id}` returns `report.json`, `{file}.png` a failure screenshot. Needs Chrome on the Igor host. **In-browser mode:** `GET .../harness/{path}` serves the player-lab harness page and vendor SDKs same-origin (path-traversal guarded); `POST .../channels/{name}/browser-results` takes the raw snapshots a driver tab measured, judges them with `player-lab judge` and stores `report.json` (`mode: "browser"`), returning the report plus `run_id`. The panel opens `harness/browser.html` in a new tab (see `player-lab/AGENTS.md`) |
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
