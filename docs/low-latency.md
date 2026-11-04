# Low-latency DASH

A looping channel normally sits two to four segments behind the live edge. With
low latency on, `loop-dee-loop` delivers the segment *in progress* chunk by
chunk, each chunk released at the moment it would have existed on a real live
encoder, and the MPD tells players they may ask for it early. A player can then
sit about a second or two behind live.

**Status.** DASH only (HLS low latency is planned). Off by default. Supported on
the `ecs-express` and `local-docker` backends; `aws-lambda` and `aws-media` are
not (see [Backends and CDN](#backends-and-cdn)).

## What you get

Measured on one machine, headless Chrome, 2 s segments with 0.5 s chunks, one
70-100 s run each. Latency is wall-clock: `Date.now()` minus the wall-clock time
of the playhead, the same definition for every player (see
[Measuring it](#measuring-it)).

| Player | Low latency off | Low latency on |
|---|---|---|
| dash.js 5.2 | 4.5 s | **2.0 s** (the MPD's target) |
| Shaka 5.2 | 3.1 s | **1.0 s** |

No stalls or errors in either, including across the loop wrap. The numbers are a
sanity check of the mechanism, not a benchmark: they are local, single runs, and
each player's own live-edge logic decides where it settles.

## How it works

```
a 2 s segment, baked as four chunks (moof+mdat), each with its own tfdt

 segment start                                           segment end
 |-----0.52 s-----|-----0.52 s-----|-----0.52 s-----|---0.44 s---|
                  ^ chunk 1        ^ chunk 2        ^ chunk 3    ^ chunk 4
                    released here    released here    released     released with
                                                                   the segment end
```

1. **Bake.** `bake.py --chunk-duration 0.5` makes GPAC cut every segment into
   several `moof`+`mdat` chunks, each carrying its own `tfdt`. Chunk sizes are
   whatever GPAC produces (frame boundaries), so the server works from the real
   timestamps and never from the nominal size.
2. **Serve.** The segment that is *in progress* on the wall clock is streamed
   with chunked transfer encoding, each chunk released at its own end. A
   completed segment is served at once as before; a request for the *upcoming*
   segment (including across the loop wrap) is held until its first chunk is
   due. Release times are integer ticks derived from the channel epoch, so
   nothing is stored and nothing drifts.
3. **Advertise.** The MPD carries `availabilityTimeOffset` (segment minus
   chunk) with `availabilityTimeComplete="false"`, a `ServiceDescription` with
   the latency target and playback-rate band, a shorter presentation delay, and a
   clock source.
4. **Sync the clock.** See [Clock sync](#clock-sync-utctiming).

Segment bytes never change: only *when* they are sent, and what the MPD says.

## Turn it on

### In a channel config

```toml
[packaging]
segment_duration = 2          # shorter segments sit better with chunks

[packaging.dash]
low_latency = "on"            # "off" | "on" | "optin"
chunk_duration = 0.5          # seconds, shorter than segment_duration
# target_latency = 2          # optional; default 4 chunks, at least 1 s
# utc_timing = "direct"       # "direct" (default) | "http" | "none"
```

It decides how the loop is **baked**, so run `spark` again after turning it on or
off (nothing detects a stale bake). Then `create`/`redeploy` and `start` as
usual. Every key is documented in [its-a-live AGENTS](../its-a-live/AGENTS.md)
and the [its-a-live README](../its-a-live/README.md#low-latency-dash).

### In Igor

The **New channel** form and the **Edit** form on a channel's page have a
low-latency setting in the *Packaging · DASH* group (only for backends that can
do it). The read-only configuration panel shows it, and the settings that a
request can override carry the `&` badge. See [Using Igor](./igor-guide.md#channels).

### By hand

```bash
uv run python loop-dee-loop/bake.py outputs/x.ts --output PKG --segment-duration 2 --chunk-duration 0.5
uv run python loop-dee-loop/serve.py PKG --epoch-utc 2026-01-01T00:00:00Z --ll-dash
```

Flags and the full list are in the
[loop-dee-loop README](../loop-dee-loop/README.md#low-latency-dash-cmaf-chunked-delivery)
and the [CLI reference](./cli-reference.md#loop-dee-loop-bake-and-serve).

## Settings

| Config key `[packaging.dash]` | Meaning | Default | Query parameter | Flag |
|---|---|---|---|---|
| `low_latency` | `off`; `on` (every MPD advertises it); `optin` (delivered, advertised only on `?low_latency=true`) | `off` | `low_latency` | `--ll-dash` / `--ll-optin` |
| `chunk_duration` | Seconds per baked chunk; shorter than the segment | `0.5` | no (baked) | `bake.py --chunk-duration` |
| `target_latency` | Latency the MPD asks players to hold (at most 120 s) | 4 chunks, at least 1 s | `ll_target_latency` | `--ll-target-latency` |
| `utc_timing` | Clock sync: `direct`, `http`, `none` | `direct` | `ll_utc_timing` | `--ll-utc-timing` |
| `utc_timing_url` | Absolute URL of `/time` for `http` | set automatically | **never** | `--ll-utc-timing-url` |

`optin` needs `[query] overrides = true`, since that is how a viewer asks.

## Per-request overrides

With `[query] overrides` on, the MPD accepts three parameters (the channel's
prefix, if any, goes in front):

```
…/index.mpd?low_latency=false            a plain MPD from a low-latency channel
…/index.mpd?low_latency=true             opt in on an `optin` channel
…/index.mpd?ll_target_latency=3          ask players to hold 3 s
…/index.mpd?ll_utc_timing=none           no clock source
```

`low_latency=true` is a `400` on a channel that does not deliver low latency.
Segment delivery is a *server* property (segment URLs carry no query string), so
`low_latency=false` only changes what the MPD advertises; an in-progress segment
is still streamed in chunks.

Two things are deliberately **not** overridable: the chunk size (it is baked into
the files), and the `UTCTiming` URL. If a link could set that URL, anyone could
craft one that points viewers' clocks at a server of their choosing (stalls, and
a request to the attacker's server). Parameter reference:
[loop-dee-loop README](../loop-dee-loop/README.md#query-parameters-startover-catchup-and-per-request-overrides-scopemd-13)
and the channel API's [OpenAPI spec](../loop-dee-loop/openapi.yaml).

## Clock sync (UTCTiming)

Players use a shared clock to decide when a segment exists. Three options:

| `utc_timing` | What the MPD says | Trade-off |
|---|---|---|
| `direct` (default) | The current time, written into the MPD | Needs no URL. The MPD is served `Cache-Control: no-store` (a cached copy would be a wrong clock), so a CDN cannot cache it. |
| `http` | A URL to poll for the time (`/time`, never cached) | The MPD stays cacheable. Needs the channel's own absolute URL. |
| `none` | Nothing | Players use their own clock; dash.js falls back to the response `Date` header. |

Measured with a deliberately wrong client clock (±3 s), a wrong clock barely
moved either player's real latency in any mode, within the noise of single runs
(one dash.js `direct` run did have its own latency estimate off by 0.8 s). dash.js
has built-in fallbacks (the MPD response's `Date` header, then a public time
server), and Shaka made no `/time` requests at all in these runs. So the choice
mostly matters for stricter clients (set-top boxes, ExoPlayer) that were **not**
tested.

**The `http` URL is configuration, set automatically.** DASH requires an
absolute `http(s)` URL (relative values are not valid in ISO/IEC 23009-1, though
dash.js tolerates them). its-a-live fills it in at deploy time:

- `local-docker`: `http://localhost:<port>/time`. Behind a tunnel or on another
  host, set `utc_timing_url`.
- `ecs-express`: the CloudFront domain (or the service endpoint without a CDN).
  It only exists once the stack does, so `create` deploys a **second time**, once,
  to set it (a CloudFormation cycle prevents knowing it earlier). Later deploys
  reuse it.
- Without a configured URL, the server derives one from the request **only for a
  localhost `Host`**. A `Host` header is client-controlled and a shared cache
  could keep a forged one, so for any other host no `UTCTiming` is written.

## Backends and CDN

| Backend | Low latency | Notes |
|---|---|---|
| `local-docker` | yes | Fewest moving parts; start here. |
| `ecs-express` | yes | Behind CloudFront: segments cached as usual, `/time` and (with `direct`) the MPD are not. The container runs gunicorn `gthread` workers. |
| `aws-lambda` | **no** | A held response occupies an invocation and bills while it waits. A config error. |
| `aws-media` | **no** | MediaPackage v1 has no low-latency output (v2 would be a separate project). A config error. |

Held responses need a threaded or async server. `docker-entrypoint.sh` switches
to gunicorn `gthread` (`GUNICORN_THREADS`, default 32) when low latency is on,
and the Flask dev server runs threaded. CloudFront collapses identical in-flight
requests, so the origin sees roughly one held request per rendition per edge, not
one per viewer. Sizing notes: [loop-dee-loop PERFS](../loop-dee-loop/PERFS.md).

## Measuring it

`player-lab` plays a channel in dash.js, hls.js, Shaka and Video.js in headless
Chrome and reports latency alongside stalls and errors:

```bash
uv run player-lab run --dash http://localhost:8080/index.mpd --hls http://localhost:8080/index.m3u8 \
    --no-timeline --boundaries 0 --duration 100 --players dashjs,shaka --max-latency 3
```

The table gains `latency`, `target`, `jitter` and `rate` columns. The first 30 s
after startup are ignored (players take a while to catch up), so run at least a
minute. Details, thresholds and the definition of wall-clock latency:
[player-lab AGENTS](../player-lab/AGENTS.md#how-it-works).

To check that nothing between you and the origin buffers the chunks (a proxy
that does turns chunked delivery back into whole segments, silently), fetch a
segment from the live edge and watch the arrival times: chunks should land about
one chunk apart, not all at the segment end. A tunnel, for instance, should add
tens of milliseconds, not a segment.

## Known limits

- **DASH only.** LL-HLS (parts, blocking reload) is not implemented; the HLS
  output is unchanged.
- **Source GOP length** decides where segments can start (a remux, no transcode).
  Chunks work with any GOP, but only a chunk that begins on a keyframe is a
  random-access point, so join time depends on the source's GOP.
- **Marker-shortened segments.** `availabilityTimeOffset` is one value per MPD
  (segment minus chunk); a segment shortened by an ad marker makes a player ask a
  little early, and the server simply holds the request.
- **`direct` makes every MPD uncacheable at the CDN.** The extra origin load was
  not measured.
- **Not tested against a real AWS deployment.** The `ecs-express` two-pass deploy
  is covered by tests with a mocked `cdk` and real CDK synthesis, not by a live
  stack.
- **One reproducibility note.** A single Shaka run with four players sharing one
  Chrome showed a 1.25 s stall; two-player runs of the same stream (low latency
  on and off) did not. It was attributed to load, not reproduced.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `low_latency=true` returns `400` | The channel does not deliver low latency: it is off, or the package was baked without chunks. Re-spark with it on. |
| `serve.py` refuses to start with "needs a package baked with `--chunk-duration`" | Same: bake with `--chunk-duration` first. |
| Latency stays at the old value | The player is ignoring `ServiceDescription` (not every player reads it), or the MPD is cached: look for `availabilityTimeOffset` in the response. |
| Chunks all arrive at the segment end | Something in the path buffers responses; test the origin directly. |
| Config error "not supported on the aws-lambda backend" | Low latency needs a server that can hold responses open. |
| A bad `UTCTiming` URL after moving behind a proxy or tunnel | Set `utc_timing_url` (the automatic one is `localhost` or the CloudFront domain). |

## Where the details live

- [loop-dee-loop README](../loop-dee-loop/README.md#low-latency-dash-cmaf-chunked-delivery): flags, the delivery model, query parameters.
- [its-a-live AGENTS](../its-a-live/AGENTS.md) and [its-a-live README](../its-a-live/README.md#low-latency-dash): the channel config keys and deploy behaviour.
- [loop-dee-loop AGENTS](../loop-dee-loop/AGENTS.md): module map (`lowlatency.py`) and what an agent must not get wrong.
- [DASH conformance review](../loop-dee-loop/DASH_CONFORMANCE.md): the MPD checks this builds on.
- [player-lab AGENTS](../player-lab/AGENTS.md): measuring latency.
- [Igor AGENTS](../igor/AGENTS.md): the form fields and the config-section rules.
