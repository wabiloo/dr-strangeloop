# Agent entrypoint — live-scte-loop-generator

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
| Inspect/verify a built `.ts` | `frame-extractor/` | [`frame-extractor/README.md`](./frame-extractor/README.md) |

## Architecture

```
franken-ts/configs/*.yaml
        │  (asset list, ad breaks, single- or multi-rendition)
        ▼
   franken-ts CLI            →  outputs/<name>.ts (+ .markers.json)
        │                       or outputs/<name>/ (rendition ladder + shared markers.json)
        ▼
   configs/<name>.toml         (its-a-live channel config: backend, S3, AWS region)
        │
        ▼
   its-a-live/channel.py + cdk  →  spark (stage input) → deploy → start
        │
        ├── backend = "aws-media"    → MediaLive + MediaPackage v1 (real live transcoding)
        │
        └── backend = "ecs-express"  → loop-dee-loop/bake.py (GPAC remux, no transcode)
                                        → ECS Express Mode + CloudFront
```

Two backends exist for phase 3, chosen per-channel via
`[deploy].backend` in the its-a-live TOML config — **not** two different
tools an agent needs to pick between at the code level; `its-a-live`
exposes one CLI (`channel.py`) regardless of backend. `loop-dee-loop` is
only directly relevant if the backend is `ecs-express`, and even then
its `bake.py`/`serve.py` are normally invoked *by* `its-a-live`
(`channel.py spark` / the deployed ECS task), not by hand — see
`loop-dee-loop/AGENTS.md` for when running it directly is actually
appropriate (local dev/testing).

## Orchestration

[`galvanise.py`](./galvanise.py) runs all three phases from a single
franken-ts YAML config, prompting for confirmation at each step:

```bash
uv run python galvanise.py franken-ts/configs/my-stream.yaml --backend ecs-express
```

It generates `configs/<name>.toml` for you (its-a-live config) — you
don't hand-write that file for the common case. The individual phases
can also be run/inspected/torn down independently afterward (see
`its-a-live/AGENTS.md`) — `galvanise.py` prints the exact follow-up
commands at the end of its run.

## Environments

- Repo-root `.venv` (`uv sync --all-packages`): `franken-ts`,
  `frame-extractor`, `loop-dee-loop`.
- `its-a-live/` manages its **own separate** venv (`aws-cdk-lib`/`boto3`
  don't need to resolve alongside media tooling): `cd its-a-live && uv
  sync` once, then `uv run --project its-a-live ...` from the repo root.

## Where things live

- `franken-ts/configs/*.yaml` — content/ad-break definitions (source of
  truth for what a loop contains).
- `configs/*.toml` — its-a-live per-channel deploy config (backend, AWS
  region, S3 location).
- `outputs/` — everything franken-ts/frame-extractor produce (`.ts`,
  `.markers.json`, HTML reports, frame timelines).
- `its-a-live/configs/*.toml` — alternate location some existing configs
  use (functionally identical to `configs/*.toml`; either is fine, `-c`
  takes a path).
