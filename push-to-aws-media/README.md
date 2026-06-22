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

Edit `config.toml`:

```toml
[aws]
region = "eu-west-1"

[s3]
bucket_name = "my-existing-bucket"
folder = ""              # prefix inside the bucket, empty = root

[input]
ts_file = "../output_with_markers_test.ts"
```

## Workflow

```bash
# 1. Upload the .ts to S3
uv run python channel.py upload

# 2. Provision the infrastructure
cdk deploy

# 3. Start the channel (prints public HLS + DASH URLs when RUNNING)
uv run python channel.py start

# 4. Stop when done (always stop before destroy)
uv run python channel.py stop

# 5. Tear down
cdk destroy
```

If you change the source `.ts`, re-run `upload` then **restart** the channel
(`stop` + `start`) so MediaLive re-pulls the file.

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

| Command | Description |
|---|---|
| `upload` | Upload the `.ts` from `config.toml` to S3 |
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
uv run python channel.py stop
cdk destroy
```

The uploaded `.ts` object is **not** deleted (we don't own the bucket). Remove it
manually if needed:

```bash
aws s3 rm s3://<bucket_name>/<folder>/<filename>.ts
```
