# Agent Brief: Live Looping HLS/DASH Stream with SCTE-35 (AWS CDK, Python)

## Objective

A CDK Python project that:
1. Uploads a local `.ts` file (with SCTE-35 cues) to an **existing S3 bucket**
2. Provisions **MediaLive + MediaPackage v1** to produce looping live HLS and DASH
   streams with **working SCTE-35 ad markers**
3. Can be fully torn down with `cdk destroy`

> The MediaPackage **v2** design (CMAF ingest, DATERANGE, CloudFront/OAC) is kept
> separately in [`MEDIAPACKAGE_V2.md`](./MEDIAPACKAGE_V2.md) as future work. It is
> not currently wired up.

---

## Architecture

```
local .ts file
  └─► S3 bucket (existing)
        └─► MediaLive (TS_FILE input, SINGLE_PIPELINE, sourceEndBehavior=LOOP)
        │     • native MediaPackage output group (MediaPackageGroupSettings)
        │     • destination = MediaPackage v1 channel by ChannelId
        │     • pure SCTE-35 passthrough (no avail/global config)
        └─► MediaPackage v1 channel
              ├─► HLS endpoint  (SCTE35_ENHANCED, AdTriggers, 6s segments)
              └─► DASH endpoint (AdTriggers, PeriodTriggers=ADS, 6s segments)
```

This mirrors the known-good reference channel `bpkio_default_live_scte35`.

---

## The two things that make SCTE-35 actually work

1. **Use MediaPackage v1 fed by the native MediaPackage output group.** MediaLive
   passes SCTE-35 through automatically; MediaPackage v1 turns it into ad markers
   via the endpoint `AdTriggers`. (MediaPackage v2 needs CMAF ingest + extra wiring —
   see the v2 doc.)
2. **The source `.ts` must contain _timed_ cues.** `splice_immediate_flag=0` /
   `time_specified_flag=1`, real `splice_time` PTS on an IDR boundary, ~2 s pre-roll.
   Immediate cues only pass through as raw `EXT-OATCLS-SCTE35` with no
   `CUE-OUT`/`CUE-IN`; `time_signal` immediate cues produce nothing. Pre-roll is what
   lets MediaLive insert an IDR and align the segment boundary to the splice point.
   Looping works fine with timed cues.

---

## CDK stack requirements

### Tooling
- Python CDK app (`aws-cdk-lib`, `constructs`), `cdk.json` → `python3 app.py`.
- Config via `config.toml` (`aws.region`, `s3.bucket_name`, `s3.folder`, `input.ts_file`),
  with `-c ts_file=...` override.

### S3
- Reference an existing bucket via `s3.Bucket.from_bucket_name()` (do **not** create one).
- Upload the `.ts` out-of-band via `channel.py spark`.
- MediaLive source URL: `s3ssl://<bucket>/<key>`.

### IAM role for MediaLive
- Trusted principal `medialive.amazonaws.com`; managed policy `AmazonSSMReadOnlyAccess`.
- Inline: `s3:GetObject`/`s3:ListBucket` on the bucket; **`mediapackage:DescribeChannel`**
  (MediaLive resolves the v1 channel's ingest endpoints by ChannelId); CloudWatch Logs;
  `cloudwatch:PutMetricData`.

### MediaPackage v1
- `mediapackage.CfnChannel` with a stable `id` (e.g. `scte-loop-channel`).
- HLS `CfnOriginEndpoint`: `HlsPackageProperty(ad_markers="SCTE35_ENHANCED",
  ad_triggers=[...], ads_on_delivery_restrictions="BOTH", segment_duration_seconds=6,
  playlist_window_seconds=60, program_date_time_interval_seconds=1)`.
- DASH `CfnOriginEndpoint`: `DashPackageProperty(ad_triggers=[...],
  ads_on_delivery_restrictions="BOTH", period_triggers=["ADS"],
  segment_duration_seconds=6, manifest_window_seconds=60, ...)`.
- `AdTriggers`: `SPLICE_INSERT`, `PROVIDER_ADVERTISEMENT`, `DISTRIBUTOR_ADVERTISEMENT`,
  `PROVIDER_PLACEMENT_OPPORTUNITY`, `DISTRIBUTOR_PLACEMENT_OPPORTUNITY`.
- Endpoints are **public** — no CloudFront/OAC/SigV4. Output their `attr_url` directly.

### MediaLive input
- `type="TS_FILE"`, single source `s3ssl://...`, role attached, input security group `0.0.0.0/0`.

### MediaLive channel
- `channel_class="SINGLE_PIPELINE"`.
- Input attachment: `source_end_behavior="LOOP"`.
- Encoder settings (raw dict):
  - `timecodeConfig.source = "SYSTEMCLOCK"` (gives correct `EXT-X-PROGRAM-DATE-TIME`;
    not epoch locking).
  - **No** `availConfiguration`, **no** `globalConfiguration` — pure passthrough.
  - One audio (AAC 128k) + one video (H.264 1080p, GOP 50) description.
  - One output group: `MediaPackageGroupSettings` → destination ref; one output with
    `MediaPackageOutputSettings: {}`.
- Destination: `media_package_settings=[MediaPackageOutputDestinationSettingsProperty(channel_id=<v1 channel id>)]`.
- Because the destination references the channel by **string id** (not a token), add an
  explicit `ml_channel.node.add_dependency(mp_channel)`.
- Channel is created **IDLE** (do not auto-start).

### Outputs
- `S3BucketName`, `S3TsKey`, `MediaLiveChannelId`, `MediaPackageChannelId`,
  `HlsPlaybackUrl` (endpoint `attr_url`), `DashPlaybackUrl` (endpoint `attr_url`),
  `MediaLiveRoleArn`.

---

## Helper script: `channel.py`

`boto3` subcommands split into Infrastructure (`create`, `redeploy`,
`terminate`, `outputs`, `list`) and Stream (`spark`, `start`, `stop`,
`refresh`, `update` [= `spark`+`refresh`], `status`). Stack name
`ItsALiveStack-<name>-aws-media`; region from `config.toml`.

---

## Lifecycle

```bash
uv sync
cdk bootstrap
uv run python channel.py spark
cdk deploy
uv run python channel.py start   # prints public HLS + DASH URLs
uv run python channel.py stop
cdk destroy
```

---

## Gotchas / notes

- **SCTE-35 timing is the #1 source of "no markers"** — see the timed-cue rule above.
  Generate with `franken-ts` (timed by default) and verify with
  `tsp ... -P splicemonitor` (`immediate: no`, non-zero pre-roll).
- **Diagnosing SCTE end-to-end:** CloudWatch `AWS/MediaLive` metrics
  `Scte35InputMessage` (detected on input) and
  `Scte35OutputMessageAllOutputsTotalEmitted` (emitted to outputs); the egress
  manifest is the ground truth for markers.
- **Markers land on segment boundaries:** with timed cues, MediaLive inserts an IDR
  at the splice PTS and MediaPackage truncates the current segment so `CUE-OUT`/
  `CUE-IN` sit exactly on a boundary (you'll see short `#EXTINF` segments around the
  break).
- **Removal policy:** the stack does not own the S3 bucket; the uploaded `.ts` is left
  in place on `cdk destroy`.
- **Cost:** MediaLive SINGLE_PIPELINE HD ~$0.50–0.65/hour while RUNNING; stop when idle.
