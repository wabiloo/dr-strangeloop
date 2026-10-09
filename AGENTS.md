# Agent entrypoint — dr-strangeloop

This repo is a three-phase pipeline that turns a list of source video
assets + ad-break definitions into a live-looping HLS/DASH channel
running in AWS. If you are an agent asked to "build a loop", "add an ad
break", "deploy a channel", "stop/redeploy a stream", etc., start here.

```
franken-ts            loop-dee-loop / its-a-live
(define content   →    (build the .ts    →    (deploy + run
 + ad breaks)            /loop package)          the channel)
```

Each phase is a **separate tool with its own `AGENTS.md`** (or
`AGENT_BRIEF.md`) containing the schema/CLI/architecture reference for
that phase. Read the relevant one(s) before acting — do not guess field
names or CLI flags from this file alone.

| Phase | Tool | Reference |
|---|---|---|
| 1. Define content + ad breaks | `franken-ts/` | [`franken-ts/AGENTS.md`](./franken-ts/AGENTS.md) |
| 2. Build (.ts + SCTE-35) | `franken-ts/` (same tool, same command) | [`franken-ts/AGENTS.md`](./franken-ts/AGENTS.md) |
| 3. Deploy + run in AWS | `its-a-live/` | [`its-a-live/AGENTS.md`](./its-a-live/AGENTS.md), [`its-a-live/AGENT_BRIEF.md`](./its-a-live/AGENT_BRIEF.md) |
| (3a) Self-hosted backend internals | `loop-dee-loop/` | [`loop-dee-loop/AGENTS.md`](./loop-dee-loop/AGENTS.md) |
| Inspect/verify a built `.ts` | `inspector-krogh/` (`frame-extractor` CLI: every frame + GOP/I-P-B timeline; `krogh` CLI: scans the file's *actual* SCTE-35 markers, independent of franken-ts, optionally compared against `markers.json`) | [`inspector-krogh/README.md`](./inspector-krogh/README.md) |
| Validate playback of a running channel in several players (hls.js, dash.js, Shaka, Video.js): startup, stalls, errors, Period/discontinuity counts vs `/timeline.json` | `player-lab/` (`player-lab run --channel <name>`) | [`player-lab/AGENTS.md`](./player-lab/AGENTS.md), [`player-lab/DESIGN.md`](./player-lab/DESIGN.md) |
| (alt. source) Derive a loop from a captured HAR/Proxyman session, or from a VOD HLS/DASH manifest URL (`ingest-url`, multi-rendition ladder), instead of authoring one | `grave-robber/` (feeds `loop-dee-loop`'s sparse segment-list `bake.py` mode, not `franken-ts`) | [`grave-robber/AGENTS.md`](./grave-robber/AGENTS.md) |
| Startover / catchup (past-range playback via `?start=&end=` on the normal manifest URLs) | `loop-dee-loop/` (`serve.py --timeshift`; on by default via `[timeshift]` in the its-a-live TOML) | [`loop-dee-loop/README.md`](./loop-dee-loop/README.md) "Startover & catchup", `loop-dee-loop/SCOPE.md` §13 |
| Web UI over all of the above (playlists, archive imports, VOD manifest imports, channels); its **Docs** menu browses all the documentation and API docs | `igor/` | [`igor/AGENTS.md`](./igor/AGENTS.md) |
| Overview, Igor tour, CLI & API cheatsheet | `docs/` | [`docs/overview.md`](./docs/overview.md), [`docs/igor-guide.md`](./docs/igor-guide.md), [`docs/cli-reference.md`](./docs/cli-reference.md) |

## Architecture

```
data/playlists/*.yaml
        │  (asset list, markers -- ad breaks/PPOs/etc, single- or multi-rendition)
        ▼
   franken-ts CLI            →  outputs/<name>.ts (+ .markers.json)
        │                       or outputs/<name>/ (rendition ladder + shared markers.json)
        ▼
   data/channels/<name>.toml   (its-a-live channel config: backend, S3, AWS region)
        │
        ▼
   its-a-live/channel.py        →  create (Infrastructure: deploy) → spark/start/stop/refresh (Stream)
        │
        ├── backend = "aws-media"     → cdk deploy → MediaLive + MediaPackage v1
        │                               (real live transcoding)
        │
        ├── backend = "ecs-express"   → cdk deploy → loop-dee-loop/bake.py
        │                               (GPAC remux, no transcode)
        │                               → ECS Express Mode + CloudFront
        │
        └── backend = "local-docker"  → no cdk, no AWS at all →
                                        loop-dee-loop/bake.py → `docker run`
                                        on your own machine
```

Three backends exist for phase 3, chosen per-channel via
`[deploy].backend` in the its-a-live TOML config — **not** different
tools an agent needs to pick between at the code level; `its-a-live`
exposes one CLI (`channel.py`) regardless of backend. Only `aws-media`/
`ecs-express` involve `cdk`/CloudFormation; `local-docker` has no stack at
all. `loop-dee-loop` is only directly relevant if the backend is
`ecs-express` or `local-docker`, and even then its `bake.py`/`serve.py` are
normally invoked *by* `its-a-live` (`channel.py spark`/`start` / the
deployed ECS task), not by hand — see `loop-dee-loop/AGENTS.md` for when
running it directly is actually appropriate (local dev/testing).

## Orchestration

[`galvanise.py`](./galvanise.py) runs all three phases from a single
franken-ts YAML playlist, prompting for confirmation at each step:

```bash
uv run python galvanise.py data/playlists/my-stream.yaml --backend ecs-express
```

It generates `data/channels/<name>.toml` for you (its-a-live config) — you
don't hand-write that file for the common case. The individual phases
can also be run/inspected/torn down independently afterward (see
`its-a-live/AGENTS.md`) — `galvanise.py` prints the exact follow-up
commands at the end of its run.

## Config section naming (its-a-live TOML ↔ Igor UI)

The its-a-live channel TOML tables (`[deploy]`, `[input]`, `[infrastructure.aws]`,
`[infrastructure.s3]`, `[infrastructure.express]`/`[infrastructure.docker]`,
`[timeline]`, `[timeshift]`, `[packaging]`, `[markers]`) and Igor's config groups (create form, read-only panel, edit form)
use the **same names, one group per table, same keys in each**. Never add,
rename or move a table/key in one place only -- see
[`igor/AGENTS.md`](./igor/AGENTS.md) "Channel config sections".

## Environments

- Repo-root `.venv` (`uv sync --all-packages`): `franken-ts`,
  `inspector-krogh`, `loop-dee-loop`, `grave-robber`, `player-lab`.
- `its-a-live/` manages its **own separate** venv (`aws-cdk-lib`/`boto3`
  don't need to resolve alongside media tooling): `cd its-a-live && uv
  sync` once, then `uv run --project its-a-live ...` from the repo root.

## Where things live

Defaults below; all of them are overridable via an optional
`~/.dr-strangeloop/config.toml` (or the file `$DR_STRANGELOOP_CONFIG` points
at). With no such file the repo-relative layout below applies; with one,
unset keys default to folders inside its directory (`~/.dr-strangeloop/data`,
`outputs`, `packages`). Format:
[`dr-strangeloop.config.example.toml`](./dr-strangeloop.config.example.toml)
(sits next to `data/` and `outputs/`, so
`DR_STRANGELOOP_CONFIG=$PWD/dr-strangeloop.config.example.toml` reproduces the
repo layout). The code is [`dr_strangeloop_config.py`](./dr_strangeloop_config.py)
(stdlib only; igor, `galvanise.py` and `its-a-live` all read it). **Never
hardcode `data/...` / `outputs/...` in new code -- call `get_paths()`.**

- `data/playlists/*.yaml` — content/marker (ad break/PPO/etc) definitions
  (source of truth for what a loop contains).
- `data/channels/*.toml` — its-a-live per-channel deploy config (backend,
  AWS region, S3 location).
- `outputs/` — everything franken-ts/frame-extractor produce (`.ts`,
  `.markers.json`, HTML reports, frame timelines).
- `its-a-live/configs/*.toml` — alternate location some existing configs
  use (functionally identical to `data/channels/*.toml`; either is fine,
  `-c` takes a path).
