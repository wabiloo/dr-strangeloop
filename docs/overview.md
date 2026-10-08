# Overview

Dr. Strangeloop turns a list of source video assets plus ad-break
definitions into a **live-looping HLS/DASH channel** with SCTE-35 markers,
running either in AWS or on your own machine.

## The pipeline

```
franken-ts            loop-dee-loop / its-a-live
(define content   →    (build the .ts    →    (deploy + run
 + ad breaks)            /loop package)          the channel)
```

| Phase | Tool | What it does | Detailed docs |
|---|---|---|---|
| 1. Define content + ad breaks | `franken-ts` | A YAML **playlist** lists assets and **markers** (ad breaks, PPOs, splice inserts...). | [franken-ts README](../franken-ts/README.md), [franken-ts AGENTS](../franken-ts/AGENTS.md) |
| 2. Build | `franken-ts` | Concatenates and normalises the assets into one `.ts` (or a rendition ladder) and injects SCTE-35, writing `<name>.markers.json`. | same |
| 3. Deploy and run | `its-a-live` | Creates the channel infrastructure and starts/stops/refreshes the stream, via one CLI (`channel.py`) whatever the backend. | [its-a-live README](../its-a-live/README.md), [its-a-live AGENTS](../its-a-live/AGENTS.md) |
| 3a. Serve the loop | `loop-dee-loop` | Bakes a `.ts` into a *loop package* (GPAC remux, no transcode) and serves it as an endless live HLS/DASH channel. | [loop-dee-loop README](../loop-dee-loop/README.md) |

Around the pipeline:

- **`grave-robber`** is an alternative *source*: it derives a loop from a
  captured HAR/Proxyman session, or from a VOD HLS/DASH manifest URL,
  instead of an authored playlist. See [grave-robber README](../grave-robber/README.md).
- **`inspector-krogh`** (`krogh`, `frame-extractor`) inspects a built `.ts`:
  every frame, the GOP structure, and the SCTE-35 markers actually present.
  See [inspector-krogh README](../inspector-krogh/README.md).
- **`igor`** is this web console: a UI over all of the above. See
  [Using Igor](./igor-guide.md).

## Backends

The deploy backend is chosen per channel with `[deploy].backend` in its
channel TOML. There is a single CLI regardless of backend.

| Backend | Where it runs | Cost / needs |
|---|---|---|
| `aws-media` | MediaLive + MediaPackage v1: real live transcoding of the `.ts` | AWS account, CDK |
| `ecs-express` | `loop-dee-loop` on ECS Express Mode behind CloudFront | AWS account, CDK |
| `local-docker` | `loop-dee-loop` in a container on your machine | Docker only; no CDK, no stack |

## Concepts

- **Playlist**: a franken-ts YAML file in `data/playlists/`. The source of
  truth for what a loop contains.
- **Marker**: a SCTE-35 event attached to a playlist (ad break, placement
  opportunity, splice insert...). See [SCTE-35 marker rules](../SCTE35_MARKER_RULES.md).
- **Loop package**: the baked, ready-to-serve form of a build (segments,
  manifests templates, marker timing). Produced by `loop-dee-loop/bake.py`.
- **Channel**: a deployed (or local) instance of a loop, described by a TOML
  file in `data/channels/`. Its lifecycle verbs are *spark*, *start*, *stop*,
  *refresh*, *update*; infrastructure verbs are *create*, *redeploy*,
  *terminate*.
- **Epoch**: the instant loop 0 started. Everything is computed from
  `now - epoch`, so a channel is stateless and any number of servers agree on
  the live edge.
- **Startover / catchup**: past-range playback with `?start=&end=` on the
  normal manifest URLs. See the "Startover & catchup" section of the
  [loop-dee-loop README](../loop-dee-loop/README.md).

## Where files live

| Path | Content |
|---|---|
| `data/playlists/*.yaml` | franken-ts playlists |
| `data/channels/*.toml` | its-a-live channel configs |
| `outputs/` | builds: `.ts`, `.markers.json`, HTML reports, frame timelines |
| `data/archives/`, `data/manifests/` | captured HAR/Proxyman files and saved VOD manifest URLs |

All of these locations can be relocated with an optional
`~/.dr-strangeloop/config.toml` (or the file `$DR_STRANGELOOP_CONFIG` points
at); see `dr-strangeloop.config.example.toml` at the repo root. Code must
call `get_paths()` from `dr_strangeloop_config.py` rather than hardcode them.

## Fastest path

```bash
uv run python galvanise.py data/playlists/my-stream.yaml --backend ecs-express
```

`galvanise.py` runs all three phases, asking for confirmation at each step,
and generates the channel TOML for you. Or do the same visually in Igor:
create a playlist, build it, create a channel, start it.

Next: [CLI & API reference](./cli-reference.md).
