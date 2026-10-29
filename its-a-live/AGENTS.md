# Agent reference — its-a-live

Phase 3 of the pipeline (see repo-root [`AGENTS.md`](../AGENTS.md)):
deploys a `franken-ts` output as a live-looping HLS/DASH channel, on one
of three backends chosen per-channel. One CLI (`channel.py`), one config
schema (`config.toml`), identical commands regardless of backend.

For architecture/CDK internals see [`AGENT_BRIEF.md`](./AGENT_BRIEF.md)
(currently `aws-media`-focused; `ecs-express` internals live in
[`loop-dee-loop/AGENTS.md`](../loop-dee-loop/AGENTS.md), which also
covers `local-docker` and `aws-lambda` since all three run the same `loop-dee-loop` code; the Lambda design is in [`LAMBDA_BACKEND.md`](./LAMBDA_BACKEND.md)) and
[`README.md`](./README.md) for full setup/lifecycle prose. This file is
the condensed reference + the questions to resolve before writing a
config.

## The one decision that shapes everything else: `[deploy].backend`

| `backend` | What it deploys | Chosen when |
|---|---|---|
| `"ecs-express"` | `loop-dee-loop` (self-hosted remux, no transcode) on ECS Express Mode + CloudFront | cheap/simple, content is already in final delivery format, no need for AWS-managed live transcoding |
| `"aws-lambda"` | `loop-dee-loop` in a Lambda function behind CloudFront, package read lazily from S3 — design and trade-offs in [`LAMBDA_BACKEND.md`](./LAMBDA_BACKEND.md) | rarely-watched channels: no idle cost, no ECS cluster/ALB, the first viewer wakes it up. Many always-watched channels are cheaper on `ecs-express` |
| `"aws-media"` | MediaLive + MediaPackage v1 (real live transcoding) | need MediaLive/MediaPackage-specific features, or transcoding from a non-final format |
| `"local-docker"` | `loop-dee-loop` in a `docker run` container on this machine — no AWS resources, no CloudFormation stack at all | local dev/demo/testing of a channel before spending any AWS money, or when there's no AWS account in play |

This must be decided **before** writing the franken-ts config, because it
determines whether `output.file` (single-rendition) or `output.dir` +
`output.renditions` (ABR ladder) is required there — see
`franken-ts/AGENTS.md`. `local-docker` follows the same rule as
`ecs-express` here (it runs the same `bake.py`); so does `aws-lambda`.

## Config (`config.toml`) — required fields to gather from the user

```toml
[deploy]
name = "..."          # required — drives stack name + all AWS resource names
backend = "..."        # required — "ecs-express" | "aws-lambda" | "aws-media" | "local-docker"

# The deployment target lives under [infrastructure.*]: aws, s3, express, docker.
# (The old top-level [aws]/[s3]/[express]/[docker] tables are rejected.)
[infrastructure.aws]
region = "..."         # required (ignored by local-docker)

[infrastructure.s3]
bucket_name = "..."    # required (ignored by local-docker) — must already exist, never created/destroyed by the stack
content_folder = "..." # required (ignored by local-docker) — content lives under <content_folder>/<name>/

[input]
source_path = "..."    # required — franken-ts output (.ts file, or ladder dir for ecs-express/aws-lambda/local-docker), or a grave-robber segment-list manifest.json (from `grave-robber ingest`/`ingest-url`; ecs-express/aws-lambda/local-docker only). Relative paths resolve against this TOML's directory (legacy `../outputs/x.ts`, relative to the cwd, still works if nothing exists at the TOML-relative spot); prefer absolute
source_kind = "playlist" # optional metadata written by igor: "playlist" | "archive" | "manifest" -- not read by channel.py
allow_missing_segments = false # optional -- pass --allow-missing-segments to bake.py (segment-list manifests with holes)

# ecs-express/aws-lambda/local-docker only (ignored by aws-media):
[bake]
local_output_dir = "..."   # optional, defaults to <local_package_dir>/<name> (see root AGENTS.md; default its-a-live/.local-loop-package)

# Shape of the HLS/DASH SCTE-35 signaling loop-dee-loop's bake.py renders
# (see loop-dee-loop/scte35_signaling.py). These are the channel's defaults,
# recorded in loop_descriptor.json at bake time and read back by serve.py;
# every setting that is also a query parameter of the same name (see [query])
# can be overridden per request. HLS- and DASH-only settings live in their own
# sub-table.
[markers]
increment_event_ids = false      # bump every event id by (loop number * step) each iteration instead of repeating it every loop -- step is the smallest power of 10 above the channel's largest base event id (e.g. base ids 100-190 -> step 1000, so loop 1 emits 1100/1190, loop 2 emits 2100/2190, ...), so each id's original base stays recognizable as its low-order remainder, and the id at any moment is predictable purely from wall-clock time against the channel's epoch (no runtime counter). Wraps the loop-number component back to 0 at the 32-bit SCTE-35 ceiling.
split_on = []   # optional list of SCTE-35 segmentation_type_ids (e.g. [0x22, 0x30]; a Start implies its End) that force a new DASH Period / #EXT-X-DISCONTINUITY at matching markers; signal only, so timestamps stay continuous when [timeline] continuous = true (loop-dee-loop/SCOPE.md §14) Applies to HLS and DASH alike (also the `split_on` query parameter).

[markers.hls]
descriptor_grouping = "shared"   # "shared" (default) | "narrowed" | "grouped" -- one DATERANGE per descriptor with the full shared payload, per descriptor narrowed to just that event, or one per group of coincident descriptors
cue_tags = "none"                # "none" (default) | "alongside" | "only" -- also emit EXT-X-CUE-OUT/-CONT/-IN next to DATERANGE, or instead of it entirely ("only" requires every marker to be a bare splice_insert). Both modes only ever build CUE-OUT/-CONT/-IN from bare splice_insert markers -- "alongside" still DATERANGE-tags every marker regardless of splice_type, but silently skips CUE-OUT/-IN for non-splice_insert ones, since nested/overlapping time_signal types (e.g. Break containing PPO containing Ad) have no well-formed single CUE-OUT/-IN pair the way a flat splice_insert avail does
id_template = "{segcode}-{eventid}-{loop}" # HLS DATERANGE ID template, ecs-express/aws-lambda/local-docker only

[markers.dash]
descriptor_grouping = "shared"   # "shared" (default) | "narrowed" -- the DASH counterpart of [markers.hls] descriptor_grouping (no "grouped" form); the two are independent
signal_format = "binary"         # "binary" (default; <scte35:Binary> splice_info_section) | "xml" (full decoded SpliceInfoSection) -- EventStream encoding of the SCTE-35 message

# Table names are shared vocabulary: igor's New/Edit forms and read-only config
# panel show one group per table, titled with the table name (see igor/AGENTS.md
# "Channel config sections"). Keep a setting in the table where it belongs and
# update igor in the same change when you add/rename/move a table or key.

[packaging]
segment_duration = 4.0
dvr_window = 30              # seconds -- serve.py sliding-window size (also the `dvr_window` query parameter)

[packaging.hls]              # decided at bake time: TOML only, no query parameter
container = "cmaf"           # "cmaf" (default) | "ts"; HLS only, DASH remains CMAF
mux_audio = true             # TS only: true muxes audio with each video rendition; false uses a separate audio TS playlist

[packaging.dash]             # also query parameters of the same name; ecs-express/aws-lambda/local-docker, serve.py flags
timeline_style = "full"      # "full" (default; every <S> has @t and @d, a SegmentTemplate per representation) | "compact" (implicit @t where contiguous + @r for equal @d, one shared SegmentTemplate)
addressing = "number"        # "number" (default; $Number$ + startNumber) | "time" ($Time$ = each <S t>); applied on create/redeploy

[timeline]                  # ecs-express/aws-lambda/local-docker only
epoch = "2026-01-01T00:00:00Z"   # optional (this is the default) -- loop 0's start and the DASH availabilityStartTime; UTC, exactly this form. A recent epoch keeps loop numbers small. ecs-express/aws-lambda/local-docker only; applied on create/redeploy (local-docker: redeploy/refresh), not by a plain `start`. Changing it on a running channel restarts loop numbering.
continuous = true   # default true -- serve.py rewrites each segment's own timestamps per request (header patch, never a re-mux -- loop-dee-loop/SCOPE.md §12) so the channel has no #EXT-X-DISCONTINUITY/DASH Period restart at the loop wrap. false falls back to the honestly-signaled discontinuity/Period-restart default (e.g. if a package's CMAF fragments have a 32-bit tfdt, which continuity mode's startup check refuses). ecs-express/aws-lambda/local-docker only -- aws-media (MediaLive/MediaPackage) doesn't run loop-dee-loop at all.

# Query parameters on the channel's normal HLS/DASH URLs (loop-dee-loop/SCOPE.md §13)
# -- ecs-express/aws-lambda/local-docker only. Startover/catchup: start, end, full_loops,
# offset. Per-request overrides of how the manifest is rendered: timeline_mode
# (default|continuous|periodic, overrides [timeline].continuous), epoch, dvr_window,
# split_on, timeline_style, addressing, cue_tags, descriptor_grouping,
# increment_event_ids, signal_format, id_template, max_height, max_bitrate. Only
# manifests change, never segment bytes. Two independent, TOML-only switches (they gate
# the parameters, so they are not parameters themselves): `timeshift` and `overrides`. The
# whole table is optional (missing = both on, with these defaults).
[query]
timeshift = true                          # startover/catchup: start, end, full_loops, offset. false: those are ignored (the channel is live only)
overrides = true                          # the per-request OVERRIDES (every parameter except start, end, full_loops, offset). false: ignored -- not rejected, not forwarded -- and the channel's own settings always apply. Independent of `timeshift`; with only one switch on, fewer names enter the CloudFront cache key
prefix = ""                               # prepended to EVERY parameter name ("dsl_" -> dsl_start, dsl_cue_tags): keeps them from clashing with a caller's own, e.g. an SSAI service using the channel as its source
start_param = "start"                     # the two names you can change; ALL names (prefixed) are also the CloudFront manifest cache key (redeploy after changing)
end_param = "end"

[timeshift]
max_span_seconds = 21600                  # longest startover/catchup range (also caps an open-ended startover and `dvr_window`)

# `port` lives with whichever backend-specific section already exists for
# that backend, not a shared section:
[infrastructure.express]
port = 8080    # ecs-express only
cpu = 256      # 0.25 vCPU units, Fargate convention -- ecs-express only, ignored by local-docker
memory = 512   # MB
cdn = true     # ecs-express only: CloudFront in front of the service (default true). false skips it
               # (~3 min faster deploy) but loses edge caching, and the playback URLs then point at the
               # service endpoint -- changing it on a deployed channel changes those URLs
# aws-lambda instead gets, in place of [infrastructure.express] (no port: CloudFront + a
# Function URL only CloudFront may call own the endpoint):
# [infrastructure.lambda]
# memory = 1024               # MB; CPU scales with it. Use 1769 (one full vCPU) for hls container "ts" + continuous timeline
# timeout = 30                # s
# reserved_concurrency = 10   # cap on concurrent invocations (cost/blast radius); -1 = no reservation, for accounts whose
#                             # Lambda concurrency quota is too small to reserve any. `stop` sets it to 0, `start` restores it. Never 0 here.
# cache_mb = 512              # /tmp cache of segment bytes fetched from S3 (ephemeral storage is sized to fit)
# architecture = "arm64"      # or "x86_64"
# local-docker instead gets, in place of [infrastructure.express]:
# [infrastructure.docker]
# port = "auto"  # default -- auto-picks a free host port (8080-8179, skipping
#                # ports already bound, e.g. by other local-docker channels)
#                # and remembers it across start/refresh/status via the
#                # container itself (no state file). Set an explicit
#                # port = 8080 (int) instead to pin it.
# bundle_content = false  # default -- the container bind-mounts the baked loop
#                # package from this machine. true copies the package INTO a
#                # per-channel image (its-a-live-<name>:baked) and runs it with no
#                # mount, so the image is self-contained and shareable. See
#                # "local-docker: volume vs bundled content" below.
```

`[markers.hls] id_template` accepts static text and these `{placeholder}` fields.
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
these IDs, so this setting applies only to ecs-express, aws-lambda and local-docker.

### `config_version`, renamed keys and migrating old files

Every channel config carries a top-level integer `config_version` (the layout of
the file; **absent means 1**, i.e. written before 2.0; current is **2**). `channel.py` and `app.py` refuse a config
of another version -- an older one with the exact changes and the migration command, a newer one as written by
a newer release -- and also a "version 2" file that still has an old key. Igor writes the marker in every file it
generates and warns on a channel page whose file is older. Migrations chain (`_legacy_cfg._MIGRATIONS[n]` upgrades
n to n+1), so a file of any age is brought forward step by step; to add one, bump `CONFIG_VERSION` (here, in
`igor/.../its_a_live.py` and `channelConfigLayout.ts` -- a test keeps the three equal), write the step and extend the
text migrator if it moves keys between tables.

Version 1 -> 2: several keys were renamed or moved (HLS/DASH-specific settings now sit in
`.hls` / `.dash` sub-tables, the query-parameter settings in `[query]`).
`channel.py` and `app.py` refuse a config that still has an old key (they would
silently fall back to the new key's default) and print the exact renames.
Rewrite a file -- keeping its comments -- with:

```bash
uv run --project its-a-live python its-a-live/migrate_config.py data/channels   # files or directories; only channel configs ([deploy]) older than the current version are touched
uv run --project its-a-live python its-a-live/migrate_config.py --check FILE    # report only, exit 1 if the version is older
uv run --project its-a-live python its-a-live/migrate_config.py --dry-run FILE  # show the diff
```

| Old | New |
|---|---|
| `[markers] daterange_mode` | `[markers.hls] descriptor_grouping` |
| `[markers] dash_descriptor_mode` | `[markers.dash] descriptor_grouping` |
| `[markers] cue_tags` | `[markers.hls] cue_tags` |
| `[markers] daterange_id_format` | `[markers.hls] id_template` |
| `[markers] dash_signal_format` | `[markers.dash] signal_format` |
| `[markers] period_on_segmentation` | `[markers] split_on` |
| `[markers] period_on_segmentation_apply` | removed (a forced Period/discontinuity applies to HLS and DASH alike; the migration warns if it was not `"both"`) |
| `[packaging] dvr_window_seconds` | `[packaging] dvr_window` |
| `[packaging] hls_format` / `hls_ts_mux_audio` | `[packaging.hls] container` / `mux_audio` |
| `[packaging] dash_timeline` / `dash_addressing` | `[packaging.dash] timeline_style` / `addressing` |
| `[timeline] epoch_utc` | `[timeline] epoch` |
| `[timeshift] enabled` | `[query] timeshift` **and** `[query] overrides` (the old single switch gated every query parameter, so both new ones inherit its value; `[query] enabled` from 2.0 development builds migrates the same way) |
| `[timeshift] start_param` / `end_param` | `[query] start_param` / `end_param` (`[timeshift] max_span_seconds` stays) |

The query parameters were renamed too: `full-loops` -> `full_loops`,
`timeline` -> `timeline_mode`. Nothing reads the old names. The code that maps
TOML keys onto bake.py/serve.py flags is `_markers_cfg.py`, `_packaging_cfg.py`,
`_query_cfg.py` and `_epoch_cfg.py`; the old-key list lives in `_legacy_cfg.py`
(`tests/test_channel_config.py` covers both). bake.py/serve.py keep their own
flag names (`--daterange-mode`, `--dash-timeline`, ...); the `[query]` names
reach serve.py as `--timeshift`, `--timeshift-start-param`, `--query-prefix`, ....

## Questions to ask before deploying (if not already answered)

1. Backend (`ecs-express` vs `aws-lambda` vs `aws-media` vs `local-docker`) — see table above.
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
7. For `local-docker`: will anyone other than this machine need to run the
   channel? If yes, they need the *content* too, not just the image — see
   "local-docker: volume vs bundled content" below, and set
   `bundle_content = true` (or run `channel.py image --save`). Warn the user
   that the image then contains the whole loop.

## Commands (identical across all three backends)

Split into two groups: **Infrastructure** (does the stack/container exist
at all) and **Stream** (is content actually playing).

Infrastructure:

```bash
uv sync                                        # once, its-a-live has its own venv
cdk bootstrap                                  # once per account/region -- N/A for local-docker
cdk deploy ItsALiveSharedStack-ecs-express      # once per account/region, ecs-express only (aws-lambda has no shared stack)
cdk deploy ItsALiveSharedStack-scheduler -c scheduler=true        # once per account/region -- only needed if you'll
                                                 # use `channel.py schedule` (aws-media/ecs-express/aws-lambda only)

uv run python channel.py -c <config.toml> create      # first-time bootstrap: spark + deploy + start (spark + start only for local-docker)
# ...or the manual equivalent, for finer-grained control:
uv run python channel.py -c <config.toml> spark    # stage input (bake or upload) -- do this BEFORE first deploy
cdk deploy ItsALiveStack-<name>-<backend> -c config=<config.toml>   # N/A for local-docker (no stack)
uv run python channel.py -c <config.toml> start    # go live, prints playback URLs

uv run python channel.py -c <config.toml> redeploy    # apply a config change, or recover a broken stack -- N/A for local-docker (aliases refresh)
uv run python channel.py -c <config.toml> outputs     # print stack outputs -- N/A for local-docker
uv run python channel.py -c <config.toml> list        # list channels + stack status under a config directory (or `list <channel.toml>` for just one)

uv run python channel.py -c <config.toml> stop     # stop paying for compute (or stop the local container)
uv run python channel.py -c <config.toml> terminate   # tear the stack down for good -- N/A for local-docker
# ...or the manual equivalent:
cdk destroy ItsALiveStack-<name>-<backend> -c config=<config.toml>   # N/A for local-docker
```

Scheduling (aws-media/ecs-express/aws-lambda only -- local-docker has no AWS
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
uv run python channel.py -c <config.toml> image    # local-docker only: build the self-contained image
                                                    # (code + baked loop package), regardless of
                                                    # `bundle_content`. [--epoch-utc ...] [--save [PATH]]
                                                    # --save exports a tarball for `docker load`
                                                    # (default <outputs_dir>/images/<name>.tar)
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
error instead of crash-looping since there's no ECS service to loop; `aws-lambda` doesn't crash-loop either, its first requests just fail until the package is staged).
`refresh` has different cost/interruption characteristics per backend
(fast re-sync for `ecs-express`, recycle of warm Lambda environments plus a CDN invalidation for `aws-lambda`, full stop/start cycle for `aws-media`,
container recreate — a few seconds — for `local-docker`) — see
`README.md` → "Updating content on a running channel" before promising a
hot reload.

### local-docker: volume vs bundled content

The `loop-dee-loop:local` image holds only code (GPAC, ffmpeg, `serve.py`…)
— never the video. Where the loop package comes from is the one choice
`[infrastructure.docker] bundle_content` makes:

| | `bundle_content = false` (default) | `bundle_content = true` |
|---|---|---|
| Content | bind-mounted read-only from `bake.local_output_dir` at `docker run` | copied into `its-a-live-<name>:baked` (`loop-dee-loop/Dockerfile.baked`); no mount |
| Run by someone else | **Won't work** — the image alone has no content | `docker run -p 8080:8080 <image>` works anywhere; no S3/AWS/volume |
| Updating content | `update` (spark + fast container recreate) | `update` also **rebuilds the image** (slower; image ≈ size of the loop) |
| `spark` alone | changes what the next start/refresh serves | changes nothing until `start`/`refresh`/`image` rebuilds the image |
| Server | `serve.py` (Flask) on the mounted dir | gunicorn via `docker-entrypoint.sh`, `LOOP_PACKAGE_BAKED=1` |

`channel.py image [--save]` builds the bundled image on demand even with
`bundle_content = false` (that setting only decides what `start`/`refresh`
run locally), and `--save` exports it for another machine. The image bakes
in the channel's epoch (default Unix epoch, `--epoch-utc` to change), DVR
window and continuous-timeline setting as defaults; the recipient can
override any `serve.py` flag after the image name. **Always warn the user**
before bundling: the image contains the entire loop (anyone with it can
extract the video), it is a snapshot (later content needs a rebuild), and
old images pile up until `docker image prune`. `ecs-express` is unaffected
— it keeps content in S3. igor exposes this as a checkbox on the channel
form, an "Export shareable image" action, and a download link.

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
