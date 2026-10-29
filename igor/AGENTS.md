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
  position on `ecs-express` and `aws-lambda` channels.

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

## Jobs drawer

Job output no longer renders inline. `frontend/src/components/JobDrawer.vue`
(mounted in `App.vue`) is a bottom drawer with one tab per job: resizable
(drag the top edge; height + open state remembered in localStorage), copy /
download log, Cancel (`POST /api/v1/jobs/{id}/cancel`, kills the process group;
job ends `failed` with `cancelled: true`), per-tab dismiss and *Clear
finished*. State and the single poller live in `frontend/src/jobDrawer.ts`; on
load it pulls `GET /api/v1/jobs/` so jobs survive closing the page (until the
backend restarts). A new job adds a tab but does **not** open the drawer.
`JobPanel.vue` is now just a one-line status + *View output* that registers its
`jobId` with the drawer and still emits `finished`. To surface a job from new
code, call `trackJob(job)` (or render `JobPanel`).

## Channel preview page, Timeshift & URL overrides

A channel has two pages: the **detail page** (`/channels/:name`, `ChannelDetail.vue`: actions,
schedule, configuration) and the **preview page** (`/channels/:name/preview`,
`ChannelPreview.vue`: live players, the panels below, the timeline window, the Playback test).
The detail page has a **Preview** button while the channel is playing, the channel list has an eye
icon on running rows, and when an action brings a channel newly alive the detail page navigates
to the preview page itself (`?alive=1`, which plays the "It's alive" banner there). The preview
page has an *Actions & configuration* button back, and says so when the channel is not playing.

Two separate, collapsible panels under the players -- **Timeshift** (`TimeshiftPanel.vue`:
start/end, presets, whole loops, offset; shown while `[query] timeshift` is on) and **URL
overrides** (`UrlOverridesPanel.vue`: every other per-request parameter; shown while `[query]
overrides` is on) -- build ONE set of manifest URLs. Their state, validation and URL building are
shared in `composables/useUrlRequestForm.ts`; `UrlRequestPanels.vue` hosts both, plus the shared
output (kind/summary, problems, the HLS/DASH URLs, copy, **Preview**, *Back to live*). It emits
`preview` (swap the players) and `built` (the URLs as currently built, whether previewed or not:
the Playback test runs against these). The text below describes both.

For `ecs-express` / `aws-lambda` / `local-docker` channels with `[query] timeshift` on (the
default), a running channel's page shows the **Timeshift** panel under
the players (**collapsed by default**, open/closed remembered in the browser; while a
preview is active the output card summarises it and offers *Back to live*): pick a start (and optional end) in UTC or local time, presets
(last N minutes, previous/current loop), *whole loops only*, and a timeline
override (`timeline_mode=default|continuous|periodic`, fixed name, also usable on
live URLs by leaving the start empty), and *Pretend “now” is…* (`offset=-PT1H` /
`-3600`, fixed name, negative or positive, with presets; plays the live edge as it
was/will be, and start/end are judged against that moment). The offset can also be
given as the **datetime “now” should pretend to be**: Igor computes `offset =
datetime − moment of Preview/Copy` (whole seconds) and freezes it in the URL, so the
stream then advances in real time from that instant; it builds the HLS and DASH URLs (param names from the channel's
`[query]` config -- `start_param`/`end_param`, and the optional `prefix` put in front of every name -- values as ISO 8601 / epoch s / epoch ms), validates them
the way `serve.py` will, lets you copy them, and **Preview** swaps them into
the HLS/DASH players (which then start from the beginning of the range rather
than the live edge; *Back to live* restores). URL building/validation lives
in `frontend/src/utils/timeshift.ts` (tests: `node --test
--experimental-strip-types src/utils/timeshift.test.ts`). The per-channel
settings are in the New/Edit forms' "Query" group (`timeshift_enabled`,
`query_prefix`, `timeshift_start_param`, `timeshift_end_param` → `[query]`) and
"Timeshift" group (`timeshift_max_span_seconds` → `[timeshift] max_span_seconds`).
The panel is one form for every query parameter of the channel: below the startover
controls (shown while `[query] timeshift` is on) a **Rendering overrides** block (shown while
`[query] overrides` is on; each switch shows only its own kind, with a message when it is off)
is **opt-in, to keep the form short**: it starts empty, and an **Add override…** picker (grouped
**HLS and DASH** / **HLS** / **DASH**) adds the ones you want -- timeline mode, epoch, DVR window,
descriptor grouping, event ids, `split_on`, ladder limits; `cue_tags` and the ID template; DASH
style, addressing and signal format. Each added override is a row under its subheading with a
remove button (removing clears its value); an added row with no value is left off the URLs.
One **Preview**
sends the result to the existing HLS and DASH players (not a player per combination): each
player's URL carries only the parameters its format reads (`queryParams.ts`:
`appliesTo`/`overridePairs`; `descriptor_grouping=grouped` is HLS only), all behind the
channel's `prefix`. An empty field keeps the channel's own value, shown as "channel: X". Values are
validated up front like the server does (`validateOverrides`), and an `epoch` override moves what
"before the channel epoch" means. The registry of overridable parameters -- name, label, kind,
formats, help -- is `frontend/src/utils/queryParams.ts` (tests: `queryParams.test.ts`); it must
list the same names as `loop-dee-loop/timeshift.py` `OVERRIDE_PARAMS`.

**Override badge.** Next to every channel setting a request can override, in the New and
Edit forms and the read-only Configuration panel, a small grey ampersand-in-a-circle mark (`QpBadge.vue`) opens a
popup with the query parameter's name (prefix applied, following what is typed in the form),
the formats that read it and a one-line explanation; it is dimmed, with a warning, when
`[query] overrides` is off. Settings fixed at bake time (segment duration, HLS container,
audio muxing) get none. The field-to-parameter map is `FIELD_OVERRIDES` in `queryParams.ts`;
add a setting there when it gains a query parameter. The DASH playhead clock shown during
a preview is approximate. Design: `loop-dee-loop/SCOPE.md` §13.

## Channel epoch (New/Edit forms, "Timeline" group)

`ecs-express` / `aws-lambda` / `local-docker` channels have a **Channel epoch (UTC)**
(`[timeline] epoch`, API field `epoch_utc`): loop 0's start and the DASH
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
| `[infrastructure.express]` / `[infrastructure.lambda]` / `[infrastructure.docker]` | Infrastructure · Express / Lambda / Docker | `port` (+ `cpu`, `memory`, `cdn` for express); `memory`, `timeout`, `reserved_concurrency`, `cache_mb`, `architecture` for lambda, whose API/form fields are `lambda_memory`, `lambda_timeout`, `lambda_reserved_concurrency`, `lambda_cache_mb`, `lambda_architecture` (mapping: `lambdaFieldsFromConfig` in `channelConfigLayout.ts`) |
| `[timeline]` | Timeline | `epoch`, `continuous` |
| `[packaging]` | Packaging | `segment_duration`, `dvr_window` |
| `[packaging.hls]` | Packaging · HLS | `container`, `mux_audio` |
| `[packaging.dash]` | Packaging · DASH | `timeline_style`, `addressing` |
| `[query]` | Query | `timeshift`, `overrides`, `prefix`, `start_param`, `end_param` |
| `[timeshift]` | Timeshift | `max_span_seconds` |
| `[markers]` | Markers | `increment_event_ids`, `split_on` |
| `[markers.hls]` | Markers · HLS | `descriptor_grouping`, `cue_tags`, `id_template` |
| `[markers.dash]` | Markers · DASH | `descriptor_grouping`, `signal_format` |

**Interim naming gap.** The *groups* above follow the TOML tables exactly, but the
API payload (`ChannelCreatePayload`) and the form fields still use their older flat
names; `formFieldsFromConfig` (`channelConfigLayout.ts`) and
`its_a_live.generate_toml` are the only two places that map between the two:

| TOML | API / form field |
|---|---|
| `[packaging] dvr_window` | `dvr_window_seconds` |
| `[packaging.hls] container`, `mux_audio` | `hls_format`, `hls_ts_mux_audio` |
| `[packaging.dash] timeline_style`, `addressing` | `dash_timeline`, `dash_addressing` |
| `[timeline] epoch` | `epoch_utc` |
| `[markers] split_on` | `period_on_segmentation` (the old `period_on_segmentation_apply` is gone) |
| `[markers.hls] descriptor_grouping`, `cue_tags`, `id_template` | `daterange_mode`, `cue_tags`, `daterange_id_format` |
| `[markers.dash] descriptor_grouping`, `signal_format` | `dash_descriptor_mode`, `dash_signal_format` |
| `[query] timeshift`, `overrides`, `prefix`, `start_param`, `end_param` | `timeshift_enabled`, `query_overrides`, `query_prefix`, `timeshift_start_param`, `timeshift_end_param` |

Harmonising the API and UI field names with the TOML (one `descriptor_grouping`
per format, no `hls_`/`dash_` prefixes) is deliberately left for a later change.
Old channel TOML files are migrated with `its-a-live/migrate_config.py` (see
`its-a-live/AGENTS.md`, "`config_version`"); Igor writes `config_version = 2` in every file it
generates, and a channel whose file is older shows a warning with the migration command on its
detail page (its settings are shown as defaults until it is migrated).

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
| `GET/PUT/DELETE /api/v1/profiles/*` | Output profiles (mode, resolution/bitrate or renditions, framerate, GOP, H.264 profile/level -- global plus optional per-rendition -- and service provider/name) for the playlist editor's Output section. Built-ins (`1080p single, 5 Mbps`, `broadpeak default, 5-rung ladder`, whose per-rung profile/level come from its avc1 codec strings) live in `src/igor/integrations/profiles.py` and are read-only (403 on overwrite/delete); user profiles are `data/profiles/<name>.yaml` (`profiles_dir` in the config). `PUT /{name}` saves or overwrites. The UI copies values into the playlist on apply -- playlist YAML never references a profile and franken-ts doesn't know about them |
| `GET/POST/DELETE /api/v1/manifests/*` | VOD manifest-URL sources (separate from `/archives`: no capture/coverage/range picker). `POST /` saves a URL (`data/manifests/<name>.json`), `POST /inspect` fetches just the manifest and returns its rendition ladder, `POST /{name}/import` spawns `grave-robber ingest-url` (`renditions`: `all`/`best`/`#1,#3`, `audio`, `allow_missing_segments`) into `outputs/manifests/<name>/`, `GET /{name}/import/status` summarises the result. Channels reference it with `source_kind = "manifest"` |
| `GET/POST/DELETE /api/v1/channels/*` | its-a-live TOML CRUD (igor-only, no `channel.py` equivalent) |
| ↳ Infrastructure: `/create`, `/redeploy`, `/terminate` (job-spawning), `/outputs` | does the stack/container exist -- `/redeploy`, `/terminate` and `/outputs` are no-ops/unavailable for local-docker channels (no stack); igor's UI hides all three for local-docker. No `/list` |
| ↳ `GET /api/v1/channels/` (+ `?live=false`), `GET .../{name}/summary` | the channel table. `?live=false` is instant (local TOML only: name, backend, source); `/{name}/summary` runs `channel.py list <that config>` (stack/live state + `reachable`, the slow part). The UI fetches the quick list, then every summary in parallel and fills rows in as they arrive; a row without a resolved summary is never deletable or startable. Plain `GET /` still returns the full list in one (slow) call |
| ↳ Stream: `/spark`, `/start`, `/stop`, `/refresh`, `/update` (job-spawning), `/status` | is content actually playing -- available for every backend. `/update` is `/spark`+`/refresh` combined (channel.py's `update`); igor's UI shows it instead of a separate Spark/Refresh pair once the channel is running, and plain `/spark` otherwise (staging never depends on deploy state) |
| ↳ `POST /api/v1/channels/{name}/image`, `GET .../image` | local-docker only (400 otherwise). `POST {save?, epoch_utc?}` spawns `channel.py image [--save]` (job): builds the self-contained image (code + baked loop package) regardless of the channel's `bundle_content`; `GET` downloads the tarball a `save: true` build exported. `bundle_content` (bool, default false, local-docker only) is also a field on the channel create/update payload, written to `[infrastructure.docker]`. The UI shows it as a checkbox, an "Export shareable image" action with a confirm, and a download link |
| ↳ `/health` (ecs-express / aws-lambda) | loop-dee-loop `serve.py` proxy, not a `channel.py` command |
| ↳ `GET .../{name}/timeline`, `.../docs`, `.../openapi.yaml` | proxies of `serve.py`'s `/timeline.json` (query params forwarded; drawn by the **Timeline** panel, `WindowPanel.vue`), `/docs` (HTML API docs, opened by the panel's **API docs** button) and `/openapi.yaml` (what `/docs` loads, via a relative URL) -- ecs-express / aws-lambda / local-docker only. For aws-lambda they go through the channel's CloudFront domain (the Function URL is IAM-locked), which has uncached all-query-string behaviors for `/timeline.json` and `/health` |
| ↳ `GET/POST /api/v1/channels/{name}/schedule`, `DELETE .../schedule/{window_id}` | scheduled on-air windows (`channel.py schedule list/add/remove`) -- aws-media/ecs-express/aws-lambda only, hidden in igor's UI for local-docker. `POST` is job-spawning (an immediate window also runs `start`); `GET`/`DELETE` are synchronous (fast EventBridge Scheduler API calls). Requires `ItsALiveSharedStack-scheduler` deployed once per account/region -- see `its-a-live/README.md`'s "Scheduling" |
| `GET /api/v1/docs/`, `.../pages/{slug}`, `.../channel-api/docs`, `.../channel-api/openapi.yaml` | documentation hub: catalog, Markdown of a catalogued page (only catalogued slugs are served), and the channel API spec (`loop-dee-loop/openapi.yaml`) with a ReDoc viewer that needs no running channel |
| `GET /api/docs`, `/api/redoc`, `/api/openapi.json` | Igor's own generated OpenAPI docs. Deliberately under `/api` (not FastAPI's default `/docs`) so the one `/api` proxy rule in Vite dev and ingress covers them |
| `GET /api/v1/playback-test/info`, `POST .../setup`, `GET/POST .../channels/{name}`, `GET .../channels/{name}/{run_id}[/{file}.png]` | **Playback test** panel (`PlaybackTestPanel.vue`, headless mode). `info` reports installed player SDKs/Chrome/ffmpeg; `setup` spawns `player-lab setup` (job); `POST channels/{name}` spawns `player-lab run --channel <toml> --out-dir outputs/player-lab/channels/<name>/<run_id>` (job + `run_id`; body: `players`, `formats`, `boundaries`, `duration_s`, `max_seconds`, `ffmpeg_s`, `hls_query`, `dash_query` -- the query strings of the override/timeshift URLs built in the panel above, passed as `--hls-query`/`--dash-query`); `GET channels/{name}` lists past run summaries + the active job; `{run_id}` returns `report.json`, `{file}.png` a failure screenshot. Needs Chrome on the Igor host. **In-browser mode:** `GET .../harness/{path}` serves the player-lab harness page and vendor SDKs same-origin (path-traversal guarded); `POST .../channels/{name}/browser-results` takes the raw snapshots a driver tab measured, judges them with `player-lab judge` and stores `report.json` (`mode: "browser"`), returning the report plus `run_id`. The panel opens `harness/browser.html` in a new tab, loading the override URLs as built (with their query on the `timeline` URL too; see `player-lab/AGENTS.md`) |
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
