# Agent reference — its-a-live

Phase 3 of the pipeline (see repo-root [`AGENTS.md`](../AGENTS.md)):
deploys a `franken-ts` output as a live-looping HLS/DASH channel, on one
of three backends chosen per-channel. One CLI (`channel.py`), one config
schema (`config.toml`), identical commands regardless of backend.

For architecture/CDK internals see [`AGENT_BRIEF.md`](./AGENT_BRIEF.md)
(currently `aws-media`-focused; `ecs-express` internals live in
[`loop-dee-loop/AGENTS.md`](../loop-dee-loop/AGENTS.md), which also
covers `local-docker` since both run the same `loop-dee-loop` code) and
[`README.md`](./README.md) for full setup/lifecycle prose. This file is
the condensed reference + the questions to resolve before writing a
config.

## The one decision that shapes everything else: `[deploy].backend`

| `backend` | What it deploys | Chosen when |
|---|---|---|
| `"ecs-express"` | `loop-dee-loop` (self-hosted remux, no transcode) on ECS Express Mode + CloudFront | cheap/simple, content is already in final delivery format, no need for AWS-managed live transcoding |
| `"aws-media"` | MediaLive + MediaPackage v1 (real live transcoding) | need MediaLive/MediaPackage-specific features, or transcoding from a non-final format |
| `"local-docker"` | `loop-dee-loop` in a `docker run` container on this machine — no AWS resources, no CloudFormation stack at all | local dev/demo/testing of a channel before spending any AWS money, or when there's no AWS account in play |

This must be decided **before** writing the franken-ts config, because it
determines whether `output.file` (single-rendition) or `output.dir` +
`output.renditions` (ABR ladder) is required there — see
`franken-ts/AGENTS.md`. `local-docker` follows the same rule as
`ecs-express` here (it runs the same `bake.py`).

## Config (`config.toml`) — required fields to gather from the user

```toml
[deploy]
name = "..."          # required — drives stack name + all AWS resource names
backend = "..."        # required — "ecs-express" | "aws-media" | "local-docker"

[aws]
region = "..."         # required (ignored by local-docker)

[s3]
bucket_name = "..."    # required (ignored by local-docker) — must already exist, never created/destroyed by the stack
content_folder = "..." # required (ignored by local-docker) — content lives under <content_folder>/<name>/

[input]
source_path = "..."    # required — franken-ts output (.ts file, or ladder dir for ecs-express/local-docker), or a grave-robber segment-list manifest.json (from `grave-robber ingest`/`ingest-url`; ecs-express/local-docker only)
source_kind = "playlist" # optional metadata written by igor: "playlist" | "archive" | "manifest" -- not read by channel.py
allow_missing_segments = false # optional -- pass --allow-missing-segments to bake.py (segment-list manifests with holes)

# ecs-express/local-docker only (ignored by aws-media):
[bake]
local_output_dir = "..."   # optional, defaults to ./.local-loop-package/<name>

# Shape of the HLS/DASH SCTE-35 signaling loop-dee-loop's bake.py renders
# (see loop-dee-loop/scte35_signaling.py) -- fixed per bake, read back by
# serve.py from loop_descriptor.json, not re-decided per request.
[markers]
daterange_mode = "shared"        # "shared" (default) | "narrowed" | "grouped" -- one DATERANGE per descriptor with the full shared payload, per descriptor narrowed to just that event, or one per group of coincident descriptors
cue_tags = "none"                # "none" (default) | "alongside" | "only" -- also emit EXT-X-CUE-OUT/-CONT/-IN next to DATERANGE, or instead of it entirely ("only" requires every marker to be a bare splice_insert). Both modes only ever build CUE-OUT/-CONT/-IN from bare splice_insert markers -- "alongside" still DATERANGE-tags every marker regardless of splice_type, but silently skips CUE-OUT/-IN for non-splice_insert ones, since nested/overlapping time_signal types (e.g. Break containing PPO containing Ad) have no well-formed single CUE-OUT/-IN pair the way a flat splice_insert avail does
increment_event_ids = false      # bump every event id by (loop number * step) each iteration instead of repeating it every loop -- step is the smallest power of 10 above the channel's largest base event id (e.g. base ids 100-190 -> step 1000, so loop 1 emits 1100/1190, loop 2 emits 2100/2190, ...), so each id's original base stays recognizable as its low-order remainder, and the id at any moment is predictable purely from wall-clock time against the channel's epoch (no runtime counter). Wraps the loop-number component back to 0 at the 32-bit SCTE-35 ceiling.
daterange_id_format = "{segcode}-{eventid}-{loop}" # HLS DATERANGE ID template, ecs-express/local-docker only

[packaging]
segment_duration = 4.0
dvr_window_seconds = 30
hls_format = "cmaf"          # "cmaf" (default) | "ts"; HLS only, DASH remains CMAF
hls_ts_mux_audio = true      # TS only: true muxes audio with each video rendition; false uses a separate audio TS playlist
continuous_timeline = true   # default true -- serve.py rewrites each segment's own timestamps per request (header patch, never a re-mux -- loop-dee-loop/SCOPE.md §12) so the channel has no #EXT-X-DISCONTINUITY/DASH Period restart at the loop wrap. false falls back to the honestly-signaled discontinuity/Period-restart default (e.g. if a package's CMAF fragments have a 32-bit tfdt, which continuity mode's startup check refuses). ecs-express/local-docker only -- aws-media (MediaLive/MediaPackage) doesn't run loop-dee-loop at all.
period_on_segmentation = []   # optional list of SCTE-35 segmentation_type_ids (e.g. [0x22, 0x23, 0x30, 0x31]) that force a new DASH Period / #EXT-X-DISCONTINUITY at matching markers; signal only, so timestamps stay continuous when continuous_timeline = true (loop-dee-loop/SCOPE.md §14)

# Startover/catchup (loop-dee-loop/SCOPE.md §13) -- ecs-express/local-docker only. The
# channel's normal HLS/DASH URLs accept start/end/full-loops/timeline query
# params. Defaults shown; the whole table is optional (missing = enabled with these).
[timeshift]
enabled = true
start_param = "start"                     # names are configurable; ALSO the CloudFront manifest cache-key allow-list (redeploy after changing)
end_param = "end"
# Three more query params have FIXED names: `full-loops=true` (widen the range to whole loops),
# `timeline=default|continuous|periodic` (overrides [packaging].continuous_timeline per request, live or ranged) and
# `offset=-PT1H` / `offset=-3600` (pretend "now" is earlier/later; negative or positive)
max_span_seconds = 21600                  # longest range (also caps an open-ended startover)

# `port` lives with whichever backend-specific section already exists for
# that backend, not a shared section:
[express]
port = 8080    # ecs-express only
cpu = 256      # 0.25 vCPU units, Fargate convention -- ecs-express only, ignored by local-docker
memory = 512   # MB
# local-docker instead gets, in place of [express]:
# [docker]
# port = "auto"  # default -- auto-picks a free host port (8080-8179, skipping
#                # ports already bound, e.g. by other local-docker channels)
#                # and remembers it across start/refresh/status via the
#                # container itself (no state file). Set an explicit
#                # port = 8080 (int) instead to pin it.
```

`daterange_id_format` accepts static text and these `{placeholder}` fields.
It defaults to `{segcode}-{eventid}-{loop}`. Loop packages baked before the
setting existed continue to be served with their original DATERANGE ID format:
`{loop}` (loop number), `{eventid}` (emitted SCTE-35 event ID in decimal,
including loop increment when enabled), `{segid}` (segmentation type ID in
decimal, or splice_insert command type 5), `{seghex}` (hex type ID, or
`0x05` for splice_insert), `{segcode}` (stable pair type code with `s`/`e`
suffix; splice_insert uses `SPIs`/`SPIe`), `{segname}` (full lower-case
segmentation name with hyphens, ending in `-start` or `-end`, e.g.
`provider-advertisement-start`), `{epoch}` (marker Unix time in
milliseconds), and `{pd}` (marker program date-time as ISO-8601). Paired
start/end types use an explicit code when the type-code table has one (e.g.
`0x14` uses `PRS`); unmapped end types inherit their paired start code.
Unknown/reserved values use the raw hex type. In shared/narrowed modes the
code is based on the marker's type. Grouped mode
uses the first start marker's values, or the first marker with an `e` suffix
if the group has only ends.
Unknown placeholders fail validation. Double quotes and Unicode control
characters are removed from the expanded ID per RFC 8216 quoted-string and
playlist text constraints. MediaPackage's aws-media backend does not author
these IDs, so this setting applies only to ecs-express and local-docker.

## Questions to ask before deploying (if not already answered)

1. Backend (`ecs-express` vs `aws-media` vs `local-docker`) — see table above.
2. Channel `name` and target AWS `region` (region N/A for `local-docker`).
3. Existing S3 bucket name + content folder prefix (never create a new
   bucket for this) — N/A for `local-docker`.
4. `input.source_path` — the franken-ts output path (confirm it matches
   what `franken-ts/AGENTS.md`'s output-mode decision produced).
5. For `aws-media`: confirm the source `.ts` has *timed* SCTE-35 cues
   (`franken-ts`'s default output already satisfies this — see
   `AGENT_BRIEF.md`).
6. For `local-docker`: confirm a local Docker install is available
   (`docker` on `PATH`); the loop-dee-loop image is built automatically
   on first `start` (cached after that).

## Commands (identical across all three backends)

Split into two groups: **Infrastructure** (does the stack/container exist
at all) and **Stream** (is content actually playing).

Infrastructure:

```bash
uv sync                                        # once, its-a-live has its own venv
cdk bootstrap                                  # once per account/region -- N/A for local-docker
cdk deploy ItsALiveSharedStack-ecs-express      # once per account/region, ecs-express only
cdk deploy ItsALiveSharedStack-scheduler        # once per account/region -- only needed if you'll
                                                 # use `channel.py schedule` (aws-media/ecs-express only)

uv run python channel.py -c <config.toml> create      # first-time bootstrap: spark + deploy + start (spark + start only for local-docker)
# ...or the manual equivalent, for finer-grained control:
uv run python channel.py -c <config.toml> spark    # stage input (bake or upload) -- do this BEFORE first deploy
cdk deploy ItsALiveStack-<name>-<backend> -c config=<config.toml>   # N/A for local-docker (no stack)
uv run python channel.py -c <config.toml> start    # go live, prints playback URLs

uv run python channel.py -c <config.toml> redeploy    # apply a config change, or recover a broken stack -- N/A for local-docker (aliases refresh)
uv run python channel.py -c <config.toml> outputs     # print stack outputs -- N/A for local-docker
uv run python channel.py -c <config.toml> list        # list channels + stack status under a config directory

uv run python channel.py -c <config.toml> stop     # stop paying for compute (or stop the local container)
uv run python channel.py -c <config.toml> terminate   # tear the stack down for good -- N/A for local-docker
# ...or the manual equivalent:
cdk destroy ItsALiveStack-<name>-<backend> -c config=<config.toml>   # N/A for local-docker
```

Scheduling (aws-media/ecs-express only -- local-docker has no AWS
presence to schedule against):

```bash
uv run python channel.py -c <config.toml> schedule add [--start ISO8601] [--end ISO8601]
                                                    # on-air window; omit --start to start now
                                                    # (this also runs `start`), omit --end to run
                                                    # until a manual `stop`. Windows may not overlap.
uv run python channel.py -c <config.toml> schedule remove <window-id>
uv run python channel.py -c <config.toml> schedule list
```

Backed by one-time EventBridge Scheduler schedules targeting a single
shared Lambda (see `scheduler_stack.py`/`_scheduler_lambda.py`) that
calls the exact same `start`/`stop` functions this CLI itself uses --
firing is AWS-native and does not depend on `channel.py`/igor being run
again at the scheduled time. Requires `ItsALiveSharedStack-scheduler` to
be deployed once per account/region (see Infrastructure commands above).
`terminate` best-effort cleans up any windows still scheduled for that
channel.

Stream:

```bash
uv run python channel.py -c <config.toml> spark    # stage input (bake or upload)
uv run python channel.py -c <config.toml> start    # go live, prints playback URLs
uv run python channel.py -c <config.toml> stop     # stop paying for compute (or stop the local container)
uv run python channel.py -c <config.toml> refresh  # pick up newly-sparked content on a running channel
uv run python channel.py -c <config.toml> status   # current status -- "running" infra doesn't imply
                                                    # HlsPlaybackUrl/DashPlaybackUrl are actually
                                                    # serving; status/list report a `reachable`
                                                    # true/false/null field from an actual network
                                                    # check of those URLs (null = not checked because
                                                    # the backend's own raw status already says
                                                    # stopped -- see _reachability.py). igor's UI
                                                    # surfaces `reachable: false` as a distinct
                                                    # "unreachable" phase, not "running".

# updating content on a running channel, in one step:
uv run python channel.py -c <config.toml> update   # spark, then refresh
# ...or the two steps separately, for finer-grained control:
uv run python channel.py -c <config.toml> spark
uv run python channel.py -c <config.toml> refresh
```

`spark` before the first deploy, not after — `ecs-express`'s container
hard-crashes at startup if there's no package staged yet (same failure
mode applies to `local-docker`'s `start`, which just exits with a clear
error instead of crash-looping since there's no ECS service to loop).
`refresh` has different cost/interruption characteristics per backend
(fast re-sync for `ecs-express`, full stop/start cycle for `aws-media`,
container recreate — a few seconds — for `local-docker`) — see
`README.md` → "Updating content on a running channel" before promising a
hot reload.

`local-docker` has **no CloudFormation stack**: `channel.py outputs` and
`cdk deploy`/`cdk destroy` (and hence `channel.py terminate`) don't apply
to it — use `channel.py status` instead, and `stop` alone is sufficient
teardown (removes the container; nothing else was created). `redeploy` is
a pure alias for `refresh` on this backend (there's no stack to repair),
not an error. State across separate `channel.py` invocations is tracked
via a deterministic container name (`its-a-live-<name>`), not stack
outputs. igor's UI reflects all of this by hiding the Redeploy and
Terminate actions and the stack-outputs panel entirely for local-docker
channels, rather than showing an inapplicable/erroring control.
