# Using Igor

Igor is the web console for Dr. Strangeloop. It is a UI over the same CLIs
described in [CLI & API reference](./cli-reference.md): every operation it
performs shells out to `franken-ts`, `its-a-live/channel.py`, `grave-robber`
or `cdk`. Nothing it does is unavailable from the command line.

The header has one entry per area: **Playlists**, **Archives**, **Manifests**,
**Channels** and **Docs**.

## Running it

```bash
./dev.sh                                   # dev: backend + Vite with reload
cd igor/frontend && npm run build          # prod: build the SPA...
uv run --project igor python igor/server.py   # ...served at /admin/, API at /api/v1/
```

No toolchain installed? See [Running igor locally via Docker](../DOCKER_LOCAL.md).
Hosting it for a team: [off localhost](../igor/CLOUD_DEPLOYMENT.md) and
[on Kubernetes](../igor/K8S_DEPLOYMENT.md). Igor has no authentication; do not
expose it beyond localhost without adding some.

## Playlists

A visual editor for franken-ts YAML. The form is generated from the franken-ts
configuration schema, so it cannot drift from what the tool validates. From a
playlist you can duplicate it, preview the marker layout and numbering, and
**Build** it (a background job whose live log you can follow). Builds land in
`outputs/`.

Marker semantics: [SCTE-35 marker rules](../SCTE35_MARKER_RULES.md).

## Archives

An import wizard for HAR / Proxyman captures: pick the manifest URL, see each
variant's captured coverage, choose a time range, and import it with
`grave-robber ingest`. The result is a segment list a channel can loop.

## Manifests

Save a VOD HLS/DASH manifest URL, inspect its rendition ladder, choose which
renditions and audio to keep (`all`, `best`, or specific ones) and download it
with `grave-robber ingest-url`.

## Channels

A channel is an its-a-live TOML plus its runtime state. Its `[input]
source_kind` is `playlist`, `archive` or `manifest`.

**List.** Rows appear instantly from local TOML, then fill in with live stack
and running state as each channel's summary arrives.

**New.** A form whose groups are the TOML tables of the channel config
(`[deploy]`, `[input]`, `[infrastructure.*]`, `[timeline]`, `[packaging]`,
`[timeshift]`, `[markers]`). The same groups appear in the read-only
configuration panel and the edit form. Field-by-field reference:
[its-a-live AGENTS](../its-a-live/AGENTS.md).

**Detail page.**

- *Lifecycle buttons*: infrastructure (create, redeploy, terminate; hidden for
  `local-docker`, which has no stack) and stream (spark, start, stop,
  refresh, update). Every action is a job with a pollable log.
- *Players*: HLS and DASH playback of the live channel, with copyable URLs.
- *Timeline panel*: the channel's current window as drawn from
  `/timeline.json`, with an **API docs** button that opens the channel's own
  API documentation.
- *Startover & catchup*: build and preview past-range URLs (`?start=&end=`)
  in UTC or local time, with presets and a "pretend now is..." offset.
  Background: [loop-dee-loop README](../loop-dee-loop/README.md).
- *Schedule*: on-air windows for AWS-backed channels.
- *Configuration*: read-only view, **Edit**, and **Duplicate to ecs-express**.
- *Health*: live loop position for `ecs-express` channels.

## Docs

This section. Pages are the Markdown files of the repository, rendered on
demand, so they are always in step with the code. The API entries open the
interactive OpenAPI documentation for Igor itself and for the channel API.

## Under the hood

[Igor AGENTS](../igor/AGENTS.md) holds the backend API table, the config-section
rules and the known gaps. [Igor README](../igor/README.md) has the layout and
run instructions.
