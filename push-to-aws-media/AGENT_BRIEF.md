# Agent Brief: Live Looping HLS/DASH Stream via AWS CDK (Python)

## Objective

Build a CDK Python project that:
1. Uploads a local `.ts` file to an **existing S3 bucket**
2. Provisions MediaLive + MediaPackage v2 to produce looping live HLS and DASH streams with SCTE-35 passthrough
3. Can be fully torn down with `cdk destroy`

---

## Architecture

```
local .ts file
  └─► S3 bucket (existing, provided by user)
        └─► MediaLive (TS_FILE input, SINGLE_PIPELINE, sourceEndBehavior=LOOP)
              └─► MediaPackage v2 channel
                    ├─► HLS origin endpoint  (DATERANGE ad markers, 6s segments)
                    └─► DASH origin endpoint (XML/EventStream ad markers, 6s segments)
```

---

## CDK Stack Requirements

### Language & tooling
- Python CDK app (`aws-cdk-lib`, `constructs`)
- CDK CLI via Node.js (`npm install -g aws-cdk`)
- `cdk.json` with `"app": "python3 app.py"`
- `requirements.txt` with `aws-cdk-lib>=2.100.0` and `constructs>=10.0.0`

### Runtime parameters (passed via `cdk deploy -c key=value`)
- `ts_file` — local path to the `.ts` file to upload
- `bucket_name` — name of the **existing** S3 bucket to upload into

### S3
- Do **not** create a bucket — reference an existing one via `s3.Bucket.from_bucket_name()`
- Upload the `.ts` file using `aws_s3_deployment.BucketDeployment` with `prune=False`
- S3 URL format for MediaLive: `s3ssl://<bucket_name>/<filename>`

### IAM role for MediaLive
- Trusted principal: `medialive.amazonaws.com`
- Inline policies needed:
  - `s3:GetObject`, `s3:ListBucket` on the existing bucket ARN
  - `mediapackagev2:PutObject`, `mediapackagev2:ListChannels`, `mediapackagev2:DescribeChannel` on `*`
  - CloudWatch Logs: `CreateLogGroup`, `CreateLogStream`, `PutLogEvents`, `DescribeLogStreams`
  - `cloudwatch:PutMetricData` on `*`
- Also attach managed policy `AmazonSSMReadOnlyAccess`

### MediaPackage v2
Create in order (each depends on the previous):

1. **CfnChannelGroup** — `channel_group_name="loop-test-group"`
2. **CfnChannel** — `channel_name="loop-test-channel"`, references channel group
3. **HLS CfnOriginEndpoint**:
   - `container_type="TS"`
   - `manifest_window_seconds=60`
   - `program_date_time_interval_seconds=1` (required for DATERANGE)
   - `ad_marker_hls="DATERANGE"`
   - `segment_duration_seconds=6`
   - SCTE filter list (see below)
4. **DASH CfnOriginEndpoint**:
   - `container_type="CMAF"`
   - `manifest_window_seconds=60`
   - `ad_marker_dash="XML"`
   - `segment_duration_seconds=6`
   - SCTE filter list (see below)

**SCTE filter list** (apply to both endpoints):
```python
[
    "SPLICE_INSERT",
    "TIME_SIGNAL_PLACEMENT_OPPORTUNITY",
    "TIME_SIGNAL_PROGRAM",
    "PROVIDER_ADVERTISEMENT",
    "DISTRIBUTOR_ADVERTISEMENT",
    "PROVIDER_PLACEMENT_OPPORTUNITY",
    "DISTRIBUTOR_PLACEMENT_OPPORTUNITY",
]
```

### MediaLive input
- `type="TS_FILE"`
- `sources=[{"url": "s3ssl://<bucket>/<filename>"}]` — single source (SINGLE_PIPELINE)
- `role_arn` = MediaLive IAM role
- Attach an `CfnInputSecurityGroup` with `cidr="0.0.0.0/0"`

### MediaLive channel
- `channel_class="SINGLE_PIPELINE"`
- `role_arn` = MediaLive IAM role
- **Input attachment**:
  - `source_end_behavior="LOOP"` — this is the key setting that loops the file
  - `input_filter="AUTO"`
  - `filter_strength=1`
  - `deblock_filter="DISABLED"`, `denoise_filter="DISABLED"`
- **Encoder settings** (as a raw dict passed to `encoder_settings`):
  - One audio description: AAC, 128kbps, 48kHz, stereo, CBR
  - One video description: H.264, 1920x1080, 5Mbps, 25fps, QVBR, HIGH profile, GOP=50 frames
  - One output group: `mediaPackageGroupSettings` pointing to destination ref `"mpv2-dest"`
  - One output within the group: `mediaPackageOutputSettings: {}`
  - `timecodeConfig.source = "SYSTEMCLOCK"` — provides correct UTC `EXT-X-PROGRAM-DATE-TIME` without epoch locking
  - Do **not** use epoch locking — it disables SCTE-35 passthrough to MediaPackage
  - `globalConfiguration.inputEndAction = "SWITCH_AND_LOOP_INPUTS"` — reinforces looping behaviour
  - `availConfiguration.availSettings.scte35SpliceInsert` with `adAvailOffset=0`, `webDeliveryAllowedFlag="FOLLOW"`, `noRegionalBlackoutFlag="FOLLOW"`
- **Destination**: id=`"mpv2-dest"`, settings url = `mpv2_channel.attr_ingest_endpoint_urls` (index 0)
- Channel is created in **IDLE** state — do not auto-start

### CloudFormation outputs
- `S3BucketName`
- `S3TsKey`
- `MediaLiveChannelId`
- `MediaPackageChannelArn`
- `HlsPlaybackUrl` — from `hls_endpoint.attr_hls_manifest_urls`
- `DashPlaybackUrl` — from `dash_endpoint.attr_dash_manifest_urls`
- `MediaLiveRoleArn`

---

## Helper script: `channel.py`

A standalone Python script (using `boto3`) with subcommands:
- `start` — starts the channel, waits for RUNNING, prints playback URLs
- `stop` — stops the channel, waits for IDLE
- `status` — prints current channel state
- `outputs` — prints all CloudFormation stack outputs

Stack name is hardcoded as `"MediaLiveLoopStack"`. Region from `boto3.session.Session().region_name`.

---

## Lifecycle

```bash
# Setup
npm install -g aws-cdk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap   # once per account/region

# Deploy
cdk deploy -c ts_file=./your_content.ts -c bucket_name=my-existing-bucket

# Start stream
python channel.py start   # prints HLS + DASH URLs when RUNNING

# Stop stream (do before destroy to avoid errors)
python channel.py stop

# Destroy all resources
cdk destroy
# Note: the .ts object in the existing S3 bucket is NOT deleted on destroy
# (we don't own the bucket)
```

---

## Key constraints and gotchas

### SCTE-35 passthrough
- MediaPackage v2 always passes SCTE-35 through — no explicit setting needed
- Do **not** enable epoch locking (`timecodeConfig.source != "ZEROBASED"` or `"EMBEDDED"`) — AWS automatically disables SCTE-35 passthrough to MediaPackage when epoch locking is active
- `SYSTEMCLOCK` is the correct timecode source: gives accurate `EXT-X-PROGRAM-DATE-TIME` in HLS without interfering with SCTE-35

### SCTE-35 on loop boundaries
The `.ts` file contains SCTE-35 `time_signal` messages with `pts_time` values baked in at file creation time. On each loop, MediaLive offsets the output PTS upward for continuity, but the `pts_time` inside the SCTE-35 packets still references the original file PTS — making them stale on loop 2+.

The markers in this file are frame-accurate (tied to specific video frames), so they cannot be stripped and re-injected via the MediaLive schedule. Mitigation options (not implemented in the stack, but document in README):

1. **Re-author with `time_specified_flag=0`**: set the `time_specified_flag` bit to 0 in `splice_time()` within each `time_signal` message. The cue fires on receipt rather than at a scheduled PTS. Since the SCTE-35 packet is muxed at the correct position in the TS, timing remains frame-accurate. Tool: `tsduck` (`tsp` with SCTE-35 table rewriter).
2. **Single pass**: use `sourceEndBehavior="CONTINUE"` instead of `LOOP`, let MediaLive hold on last frame after file ends, restart manually per test pass.
3. **Pre-baked long file**: repeat the content N times in a single file, with `pts_time` values pre-offset by `n × file_duration` for each repetition. No loop boundary is hit within the test window.

### MediaPackage v2 CDK attribute names
`CfnChannel.attr_ingest_endpoint_urls` returns a list token. Use index `[0]` for SINGLE_PIPELINE. If CDK version changes cause attribute name issues, verify against the CloudFormation resource docs for `AWS::MediaPackageV2::Channel`.

### Removal policy
The stack does **not** own the S3 bucket, so no `RemovalPolicy` is set on it. The uploaded `.ts` object is left in place after `cdk destroy`. Add a note in the README to delete it manually if needed.

### Channel billing
MediaLive SINGLE_PIPELINE HD costs ~$0.50–0.65/hour while RUNNING (region-dependent). Always stop the channel before destroying or leaving idle.
