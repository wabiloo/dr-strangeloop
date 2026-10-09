# CLI & API reference

A one-page map of every command-line tool and HTTP API in the project. Each
section gives the commands you will use most and links to the authoritative
reference. For the complete flag list of any tool, run it with `--help`.

Unless stated otherwise, run from the repo root. The repo-root `.venv`
(`uv sync --all-packages`) serves `franken-ts`, `inspector-krogh`,
`loop-dee-loop`, `grave-robber` and `igor`; `its-a-live` has its **own**
venv (`cd its-a-live && uv sync`).

## `galvanise.py`: all three phases, interactively

```bash
uv run python galvanise.py [--backend aws-media|ecs-express] [--clear-cache] config.yaml
```

Builds the playlist, generates `data/channels/<name>.toml`, deploys and
starts the channel, prompting at each step. `--clear-cache` empties the
franken-ts clip cache first. For `local-docker`, use `channel.py` directly.

## `franken-ts`: build a `.ts` with SCTE-35

```bash
uv run franken-ts [OPTIONS] CONFIG
```

| Option | Meaning |
|---|---|
| `-o, --output FILE` | Override the output path from the config |
| `--normalize` | Pre-transcode non-conforming inputs to match the output spec |
| `--cache-dir DIR`, `--no-cache` | Location of / opt out of the normalisation cache |
| `--temp-dir DIR` | Temporary files directory |
| `--stream-concurrency N` | Parallel fragment downloads per HLS/DASH asset (default 4) |
| `--dry-run` | Print the commands without running them |
| `--skip-transcode`, `--skip-inject` | Reuse an existing TS / stop before SCTE-35 injection |
| `--verify` | Run tsduck extraction after injection and write an HTML report |
| `--report-only` | Verify an existing output and write its report, no rebuild |
| `--debug`, `-v/-vv` | Keep temp files and raise log verbosity |

Reference: [franken-ts README](../franken-ts/README.md),
[franken-ts AGENTS](../franken-ts/AGENTS.md) (YAML schema, marker types).

## `its-a-live/channel.py`: deploy and run a channel

```bash
cd its-a-live
uv run python channel.py -c <channel.toml> [--json] <command> [args]
```

| Command | Effect |
|---|---|
| `create` | First-time setup: shared stack (ecs-express), `spark`, `cdk deploy`, `start` (local-docker: `spark` + `start`) |
| `spark` | Stage the input: bake and push the loop package (ecs-express / local-docker) or upload the raw `.ts` (aws-media) |
| `start` / `stop` | Go live / stop paying for compute. `start --epoch-utc now\|<ISO8601>` resets the epoch (forces a redeploy) |
| `refresh` | Pick up newly sparked content on a running channel (aws-media: a real stop/start) |
| `update` | `spark` then `refresh` |
| `status` | Current state; `--json` for programmatic use |
| `outputs` | CloudFormation outputs (not for local-docker) |
| `redeploy` | Apply a config change or recover a broken stack (local-docker: alias for `refresh`) |
| `terminate` | `cdk destroy` the stack (not for local-docker; staged S3 content is kept) |
| `list [dir\|channel.toml]` | Channels under a directory with stack and live state |
| `schedule add [--start ISO] [--end ISO]` / `remove <id>` / `list` | Scheduled on-air windows (aws-media / ecs-express only) |

`--json` makes `status`, `outputs`, `list` and `schedule` print a single JSON
document. Reference: [its-a-live README](../its-a-live/README.md),
[its-a-live AGENTS](../its-a-live/AGENTS.md) (full TOML schema).

## `loop-dee-loop`: bake and serve

Normally invoked *by* `channel.py`; run directly for local dev and testing.

```bash
uv run python loop-dee-loop/bake.py INPUT --output PKG_DIR [options]
uv run python loop-dee-loop/serve.py PKG_DIR --epoch-utc 2026-01-01T00:00:00Z [options]
```

`bake.py` accepts a franken-ts `.ts`, a rendition-ladder directory, or a
grave-robber segment-list `manifest.json`. Notable options:
`--segment-duration`, `--hls-format cmaf|ts`, `--daterange-mode`,
`--cue-tags`, `--increment-event-ids`, `--dash-signal-format`,
`--allow-missing-segments`.

`serve.py` notable options: `--port`, `--channel-name`,
`--dvr-window-seconds`, `--continuous-timeline`, `--dash-addressing number|time`, `--period-on-segmentation`,
`--timeshift` (+ `--timeshift-start-param`, `--timeshift-end-param`,
`--timeshift-max-span-seconds`).

Reference: [loop-dee-loop README](../loop-dee-loop/README.md),
[loop-dee-loop AGENTS](../loop-dee-loop/AGENTS.md),
[design scope](../loop-dee-loop/SCOPE.md).

## `grave-robber`: derive a loop from captures or VOD manifests

```bash
uv run grave-robber ingest ARCHIVE MANIFEST_URL --output DIR [--format hls|dash] [--start ISO --end ISO] [--no-audio]
uv run grave-robber ingest-url MANIFEST_URL --output DIR [--renditions all|best|720,360|#1,#3] [--no-audio] [--allow-missing-segments] [--workers N]
uv run grave-robber coverage ARCHIVE
```

`ingest` works on a HAR / Proxyman capture, `ingest-url` downloads a VOD
HLS/DASH ladder directly, `coverage` prints each variant's captured
wall-clock coverage. The output `manifest.json` is baked by `bake.py` and
referenced by a channel with `source_kind = "archive"` or `"manifest"`.
Reference: [grave-robber README](../grave-robber/README.md).

## `krogh` and `frame-extractor`: inspect a built `.ts`

```bash
uv run krogh file.ts [--expected markers.json] [--skip-frames] [--skip-html] [--before N --after N]
uv run frame-extractor file.ts [--width 320] [--workers 4] [--no-overlay]
```

`krogh` scans the SCTE-35 markers *actually present* in the file, independent
of franken-ts, and compares them with `markers.json` when found next to the
`.ts`. `frame-extractor` writes every frame with I/P/B overlay and a GOP
timeline. Reference: [inspector-krogh README](../inspector-krogh/README.md).

## HTTP APIs

| API | Served by | Where to read it |
|---|---|---|
| **Igor API** (`/api/v1/...`): playlists, archives, manifests, channels, jobs, files, docs | Igor's FastAPI backend | Interactive OpenAPI docs: **Docs → Igor API** in the header (Swagger UI at `/api/docs`, ReDoc at `/api/redoc`, schema at `/api/openapi.json`). Behaviour notes in [Igor AGENTS](../igor/AGENTS.md) |
| **Channel API**: `/index.m3u8`, `/stream.mpd`, `/timeline.json`, `/health`, startover/catchup query parameters | `loop-dee-loop/serve.py` on each running ecs-express / local-docker channel | **Docs → Channel API** in the header (the spec, `loop-dee-loop/openapi.yaml`), or a running channel's own `/docs`. Igor proxies a live channel's at `/api/v1/channels/{name}/docs` |

Igor's UI is a client of its own API; scripting against it is possible but
for one-off tasks the CLIs above are the supported route.
