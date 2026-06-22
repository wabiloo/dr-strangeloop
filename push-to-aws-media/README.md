# Live Looping HLS/DASH Stream via AWS CDK

Provisions a MediaLive + MediaPackage v2 pipeline that loops a local `.ts` file
and serves live HLS and DASH streams with SCTE-35 ad-marker passthrough.

## Architecture

```
local .ts file
  └─► S3 bucket (existing, you provide)
        └─► MediaLive (TS_FILE input, SINGLE_PIPELINE, sourceEndBehavior=LOOP)
              └─► MediaPackage v2 channel
                    ├─► HLS origin endpoint  (DATERANGE ad markers, 6s segments)
                    └─► DASH origin endpoint (XML/EventStream ad markers, 6s segments)
```

## Prerequisites

- AWS CLI configured with appropriate credentials
- Node.js (for CDK CLI): `npm install -g aws-cdk`
- [uv](https://docs.astral.sh/uv/) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Setup

```bash
uv sync
cdk bootstrap   # once per account/region
```

## Configure

Edit `config.toml` before deploying:

```toml
[aws]
region = "us-east-1"

[s3]
bucket_name = "my-existing-bucket"
folder = ""          # prefix inside the bucket, e.g. "media/ts" — leave empty for root

[input]
ts_file = "./your_content.ts"
```

## Full workflow

### First deploy

```bash
# 1. Upload the .ts file to S3
uv run python channel.py upload

# 2. Provision the AWS infrastructure
cdk deploy
```

### Redeploy (after a failed/stuck deploy)

`redeploy` handles any broken stack state automatically — it force-deletes stacks
stuck in `CREATE_IN_PROGRESS`, `ROLLBACK_COMPLETE`, `CREATE_FAILED`, etc., waits
for deletion, then runs `cdk deploy`.

```bash
uv run python channel.py redeploy
```

### Override the input file at deploy time

```bash
uv run python channel.py upload
cdk deploy -c ts_file=./other_content.ts
```

---

## channel.py reference

| Command | Description |
|---|---|
| `upload` | Upload the `.ts` file from `config.toml` to S3 |
| `start` | Start the MediaLive channel, wait for RUNNING, print playback URLs |
| `stop` | Stop the channel, wait for IDLE |
| `status` | Print current channel state |
| `outputs` | Print all CloudFormation stack outputs |
| `policy` | Grant the current AWS account access to both HLS and DASH endpoints |
| `redeploy` | Delete broken stack if needed, then run `cdk deploy` + apply policy |

```bash
uv run python channel.py <command>
```

> **Cost**: MediaLive SINGLE_PIPELINE HD costs ~$0.50–0.65/hour while RUNNING
> (region-dependent). Stop the channel when not in use.

### Accessing the playback URLs

MediaPackage v2 does not support anonymous public access. All requests to the HLS/DASH
URLs must be signed with AWS SigV4. Use `awscurl` for quick testing:

```bash
awscurl --service mediapackagev2 --region eu-west-1 "<HlsPlaybackUrl>"
```

For unauthenticated browser or player access, put a CloudFront distribution in front
of the endpoints (CloudFront handles SigV4 signing on behalf of viewers).

---

## Destroy

```bash
uv run python channel.py stop     # must be IDLE before destroying
cdk destroy
```

The `.ts` object uploaded to the S3 bucket is **not** deleted on destroy (we do
not own the bucket). Delete it manually if needed:

```bash
aws s3 rm s3://<bucket_name>/<folder>/<filename>.ts
```

---

## SCTE-35 notes

### Why SYSTEMCLOCK (not epoch locking)

`timecodeConfig.source = "SYSTEMCLOCK"` gives accurate `EXT-X-PROGRAM-DATE-TIME`
tags in the HLS manifest without enabling epoch locking. When epoch locking is
active (`ZEROBASED` or `EMBEDDED`), AWS automatically disables SCTE-35 passthrough
to MediaPackage — so `SYSTEMCLOCK` is the correct choice here.

### SCTE-35 on loop boundaries

The `.ts` file contains SCTE-35 `time_signal` messages with `pts_time` values
baked in at file-creation time. On each loop, MediaLive offsets the output PTS
upward for continuity, but the `pts_time` inside the SCTE-35 packets still
references the original file PTS — making them stale on loop 2+.

Because the markers are frame-accurate (muxed at specific video frames), they
cannot be stripped and re-injected via the MediaLive schedule. Mitigation options:

1. **Re-author with `time_specified_flag=0`** *(recommended)*  
   Set the `time_specified_flag` bit to `0` in `splice_time()` within each
   `time_signal` message. The cue fires on receipt rather than at a scheduled
   PTS. Because the SCTE-35 packet is still muxed at the correct position in the
   TS, timing remains frame-accurate.  
   Tool: `tsduck` (`tsp` with SCTE-35 table rewriter).

2. **Single pass**  
   Use `sourceEndBehavior="CONTINUE"` instead of `LOOP`. MediaLive holds on the
   last frame after the file ends. Restart manually for each test pass.

3. **Pre-baked long file**  
   Repeat the content N times in a single file, with `pts_time` values
   pre-offset by `n × file_duration` for each repetition. No loop boundary is
   hit within the test window.
