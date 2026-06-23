# Live Looping HLS/DASH Stream with SCTE-35 (AWS CDK)

Provisions a **MediaLive + MediaPackage v1** pipeline that loops a local `.ts`
file and serves live HLS and DASH streams with **SCTE-35 ad markers**
(`EXT-X-CUE-OUT` / `CUE-IN` in HLS, EventStream periods in DASH).

> Looking for the MediaPackage **v2** approach (CMAF ingest, DATERANGE, CloudFront)?
> See [`MEDIAPACKAGE_V2.md`](./MEDIAPACKAGE_V2.md). It is not currently wired up.

## Architecture

```
local .ts file
  └─► S3 bucket (existing, you provide)
        └─► MediaLive (TS_FILE input, SINGLE_PIPELINE, sourceEndBehavior=LOOP,
        │             native MediaPackage output group, SCTE-35 passthrough)
        └─► MediaPackage v1 channel
              ├─► HLS endpoint   (SCTE35_ENHANCED ad markers, 6s segments) — public URL
              └─► DASH endpoint  (ADS period triggers, 6s segments)        — public URL
```

The MediaLive channel feeds MediaPackage v1 by **ChannelId** via the native
MediaPackage output group. SCTE-35 is passed through automatically; MediaPackage
generates the ad markers from its endpoint `AdTriggers`. No avail config, no CMAF
ingest, no CloudFront — the v1 endpoints are publicly reachable.

## ⚠️ SCTE-35 source-file requirement (read this)

Whether markers actually appear depends on **how the cues are authored in the
`.ts`**, not just on the AWS config.

- Cues **must be timed**: `splice_insert` with `splice_immediate_flag=0` and a real
  `splice_time` PTS (or `time_signal` with `time_specified_flag=1`), placed on an
  IDR boundary with ~2 s pre-roll.
- Timed cues give MediaLive time to insert an IDR at the splice point and align the
  **segment boundary** to it; MediaPackage then emits a full
  `EXT-X-CUE-OUT` / `CUE-OUT-CONT` / `CUE-IN` break (and a DASH period split).
- **Do NOT** author immediate cues (`splice_immediate_flag=1` /
  `time_specified_flag=0`). MediaPackage passes those through only as raw
  `EXT-OATCLS-SCTE35` with **no** ad-break markers, and `time_signal` cues produce
  nothing at all.
- Looping is fine with timed cues — the splice PTS is consistent within each loop,
  so each loop re-fires correctly. (The previous "set `time_specified_flag=0` for
  loop safety" advice was wrong and is what suppressed the markers.)

Generate a conforming file with the repo's `franken-ts` tool (its default emits
timed cues):

```bash
uv run franken-ts test1.yaml -o output_with_markers_test.ts
```

Verify the cues are timed (look for `immediate: no` and a non-zero pre-roll):

```bash
tsp -I file output_with_markers_test.ts -P splicemonitor --all-commands -O drop
```

## Prerequisites

- AWS CLI **v2** (recent — older bundles lack some MediaLive/MediaPackage shapes)
- Node.js (for CDK CLI): `npm install -g aws-cdk`
- [uv](https://docs.astral.sh/uv/)
- `tsduck` + `ffmpeg` (for generating/inspecting the `.ts`)

## Setup

```bash
uv sync
cdk bootstrap   # once per account/region
```

## Configure

Each deployment has its own config file. A config file specifies the deployment
`name` (which becomes the CloudFormation stack ID and all resource names), the
source `.ts` file, and the S3 bucket to upload it to.

```toml
# configs/my-campaign.toml
[deploy]
name = "my-campaign"       # stack becomes ScteLoopStack-my-campaign

[aws]
region = "eu-west-1"

[s3]
bucket_name = "my-existing-bucket"
folder = "path/in/bucket"  # leave empty for root

[input]
ts_file = "../my_content.ts"
```

The `name` field drives every AWS resource name:
`scte-loop-<name>-channel`, `scte-loop-<name>-hls`, `scte-loop-<name>-dash`, etc.

## Single deployment (default config.toml)

```bash
uv run python channel.py upload
cdk deploy
uv run python channel.py start
uv run python channel.py stop
cdk destroy
```

## Multiple deployments

Each config file is an independent, parallel deployment — its own MediaLive
channel, MediaPackage channel, and endpoints.

```bash
# Deploy two independent stacks
cdk deploy -c config=./configs/campaign_a.toml
cdk deploy -c config=./configs/campaign_b.toml

# Upload source files for each
uv run python channel.py --config ./configs/campaign_a.toml upload
uv run python channel.py --config ./configs/campaign_b.toml upload

# Start / stop independently
uv run python channel.py --config ./configs/campaign_a.toml start
uv run python channel.py --config ./configs/campaign_b.toml start

uv run python channel.py --config ./configs/campaign_a.toml stop

# Tear down one without touching the other
cdk destroy ScteLoopStack-campaign_a
```

You can also override the name at deploy time without creating a separate config:

```bash
cdk deploy -c name=quick-test
uv run python channel.py -c config.toml start   # reads [deploy].name from config.toml
```

## Playback

MediaPackage **v1** endpoints are public — open the URLs directly (no SigV4, no
CloudFront):

```bash
curl -s "<HlsPlaybackUrl>" | head
```

Check for ad markers (markers appear once per loop, at each authored break):

```bash
# follow the child playlist and grep for the break
curl -s "<child .m3u8>" | grep -E "CUE-OUT|CUE-OUT-CONT|CUE-IN"
```

## channel.py reference

```
python channel.py [--config path/to/config.toml] <command>
```

`--config` (short: `-c`) selects the config file; defaults to `config.toml` in
the same directory. All commands target the stack derived from that config's
`[deploy].name`.

| Command | Description |
|---|---|
| `upload` | Upload the `.ts` from the config to S3 |
| `start` | Start the channel, wait for RUNNING, print playback URLs |
| `stop` | Stop the channel, wait for IDLE |
| `status` | Print current channel state |
| `outputs` | Print all CloudFormation stack outputs |
| `policy` | No-op (v1 endpoints are public; kept for compatibility) |
| `redeploy` | Delete a broken stack if needed, then `cdk deploy` |

> **Cost:** MediaLive SINGLE_PIPELINE HD is ~$0.50–0.65/hour while RUNNING. Stop the
> channel when idle.

## Destroy

```bash
uv run python channel.py [--config path/to/config.toml] stop
cdk destroy ScteLoopStack-<name>
```

The uploaded `.ts` object is **not** deleted (we don't own the bucket). Remove it
manually if needed:

```bash
aws s3 rm s3://<bucket_name>/<folder>/<filename>.ts
```
