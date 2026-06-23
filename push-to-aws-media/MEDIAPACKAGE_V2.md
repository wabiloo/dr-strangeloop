# MediaPackage v2 path (future work — not currently wired up)

The shipped stack uses **MediaPackage v1** because that is what the known‑good
reference channel (`bpkio_default_live_scte35`) uses and it was the fastest path
to working SCTE‑35 ad markers. This document captures everything needed to switch
to **MediaPackage v2**, including the gotchas that cost a lot of time.

> **Status:** the v2 wiring below was built and deployed, but SCTE‑35 was never
> verified end‑to‑end on v2 because of the `InputType` gotcha (see below). It is
> expected to work once the channel is created with `InputType=CMAF`. The SCTE‑35
> **source‑file** requirement (timed cues + pre‑roll, see main README) is identical
> for v1 and v2 — that part is already solved.

---

## Why v2 is different from v1

| | v1 (shipped) | v2 (this doc) |
|---|---|---|
| MediaLive → packager | native **MediaPackage output group** (`MediaPackageGroupSettings`, by `ChannelId`) | **CMAF Ingest output group** (`CmafIngestGroupSettings`, HTTP PUT of fMP4) |
| SCTE‑35 transport | automatic passthrough in the ingested TS | dedicated SCTE‑35 track via `scte35Type=SCTE_35_WITHOUT_SEGMENTATION` |
| HLS ad markers | `EXT-X-CUE-OUT` / `CUE-IN` (`SCTE35_ENHANCED`) | `EXT-X-DATERANGE` |
| DASH ad markers | EventStream (`AdTriggers` + `PeriodTriggers=ADS`) | EventStream (`ScteDash.AdMarkerDash=XML`) |
| Egress auth | **public** (open URLs) | **SigV4 only** → needs CloudFront + OAC |

---

## The gotchas (in priority order)

### 1. The channel `InputType` MUST be `CMAF`  ← this is the one that bit us
`AWS::MediaPackageV2::Channel` defaults to `InputType=HLS`. If you feed a CMAF
ingest output group to an `HLS` channel, MediaPackage rejects every PUT with
**HTTP 4xx**, the ingest session never goes live, and the egress serves a stale /
`#EXT-X-ENDLIST` manifest. It can *look* like it briefly works (a lingering prior
session) and then stall.

Diagnose with CloudWatch `AWS/MediaLive`, dimensioned by `OutputGroupName`:
- `Output4xxErrors` high (we saw ~225 per 5 min), `Output5xxErrors` = 0
- `ActiveOutputs` = 0 for the CMAF output group

Fix: create the channel with `input_type="CMAF"`.

### 2. You cannot flip `InputType` in place on a custom‑named channel
CloudFormation refuses: *“cannot update a stack when a custom‑named resource
requires replacing. Rename …”*. `InputType` change forces replacement, and the
channel has a custom `ChannelName`. So you must create a **freshly named** channel
(or drop/recreate) with `InputType=CMAF` from the start — not patch the existing one.

### 3. CMAF ingest output group rules
- Destination URL = the channel ingest endpoint (`attr_ingest_endpoint_urls[0]`) and
  the path **must end with `/`** (CFN error otherwise:
  *“CMAF output group destination URL path must end with '/'”*).
- Each CMAF ingest **output carries only one media type** — split video and audio
  into separate `OutputProperty` entries (CFN error otherwise:
  *“outputs can only contain a video description, 1 audio description, or 1 caption description.”*).
- Set `scte35_type="SCTE_35_WITHOUT_SEGMENTATION"` or **no SCTE‑35 reaches MediaPackage**
  (the track is created but empty). `NONE` = no SCTE.

### 4. Egress needs SigV4 → CloudFront + OAC
v2 origin endpoints are not anonymously reachable. Put a CloudFront distribution in
front with an Origin Access Control of type `mediapackagev2` (sigv4), and attach a
per‑endpoint resource policy allowing that distribution
(`mediapackagev2:GetObject` for principal `cloudfront.amazonaws.com`, condition
`AWS:SourceArn = <distribution arn>`).

---

## Reference CDK snippets

Imports: `aws_mediapackagev2 as mediapackagev2`, `aws_cloudfront as cloudfront`.

### Channel group + channel (note `input_type`)
```python
channel_group = mediapackagev2.CfnChannelGroup(
    self, "ChannelGroup", channel_group_name="scte-loop-group")

mpv2_channel = mediapackagev2.CfnChannel(
    self, "Channel",
    channel_group_name=channel_group.channel_group_name,
    channel_name="scte-loop-channel",
    input_type="CMAF",          # <-- REQUIRED for CMAF ingest
)
mpv2_channel.add_dependency(channel_group)
```

### HLS (DATERANGE) + DASH (XML) endpoints
```python
hls = mediapackagev2.CfnOriginEndpoint(
    self, "HlsEndpointCmaf",
    channel_group_name=channel_group.channel_group_name,
    channel_name=mpv2_channel.channel_name,
    origin_endpoint_name="hls-endpoint",
    container_type="TS",
    hls_manifests=[mediapackagev2.CfnOriginEndpoint.HlsManifestConfigurationProperty(
        manifest_name="index", manifest_window_seconds=60,
        program_date_time_interval_seconds=1,
        scte_hls=mediapackagev2.CfnOriginEndpoint.ScteHlsProperty(ad_marker_hls="DATERANGE"),
    )],
    segment=mediapackagev2.CfnOriginEndpoint.SegmentProperty(segment_duration_seconds=6),
)

dash = mediapackagev2.CfnOriginEndpoint(
    self, "DashEndpoint",
    channel_group_name=channel_group.channel_group_name,
    channel_name=mpv2_channel.channel_name,
    origin_endpoint_name="dash-endpoint",
    container_type="CMAF",
    dash_manifests=[mediapackagev2.CfnOriginEndpoint.DashManifestConfigurationProperty(
        manifest_name="index", manifest_window_seconds=60,
        scte_dash=mediapackagev2.CfnOriginEndpoint.ScteDashProperty(ad_marker_dash="XML"),
    )],
    segment=mediapackagev2.CfnOriginEndpoint.SegmentProperty(segment_duration_seconds=6),
)
```

### MediaLive: CMAF ingest output group + destination
```python
# output group
medialive.CfnChannel.OutputGroupProperty(
    name="CmafToMPv2",
    output_group_settings=medialive.CfnChannel.OutputGroupSettingsProperty(
        cmaf_ingest_group_settings=medialive.CfnChannel.CmafIngestGroupSettingsProperty(
            destination=medialive.CfnChannel.OutputLocationRefProperty(destination_ref_id="mpv2-dest"),
            scte35_type="SCTE_35_WITHOUT_SEGMENTATION",   # <-- REQUIRED for SCTE
            segment_length=6, segment_length_units="SECONDS",
            nielsen_id3_behavior="NO_PASSTHROUGH",
        )
    ),
    outputs=[  # one media type per output
        medialive.CfnChannel.OutputProperty(
            output_name="video_out", video_description_name="video_1",
            audio_description_names=[], caption_description_names=[],
            output_settings=medialive.CfnChannel.OutputSettingsProperty(
                cmaf_ingest_output_settings=medialive.CfnChannel.CmafIngestOutputSettingsProperty(name_modifier="_video"))),
        medialive.CfnChannel.OutputProperty(
            output_name="audio_out", audio_description_names=["audio_1"], caption_description_names=[],
            output_settings=medialive.CfnChannel.OutputSettingsProperty(
                cmaf_ingest_output_settings=medialive.CfnChannel.CmafIngestOutputSettingsProperty(name_modifier="_audio"))),
    ],
)

# destination — note the trailing "/"
medialive.CfnChannel.OutputDestinationProperty(
    id="mpv2-dest",
    settings=[medialive.CfnChannel.OutputDestinationSettingsProperty(
        url=cdk.Fn.join("", [cdk.Fn.select(0, mpv2_channel.attr_ingest_endpoint_urls), "/"]))],
)
```

### IAM (role for MediaLive)
```python
medialive_role.add_to_policy(iam.PolicyStatement(
    actions=["mediapackagev2:PutObject", "mediapackagev2:ListChannels", "mediapackagev2:DescribeChannel"],
    resources=["*"]))
```

### CloudFront OAC + per‑endpoint policy
- `cloudfront.CfnOriginAccessControl` with
  `origin_access_control_origin_type="mediapackagev2"`, `signing_behavior="always"`,
  `signing_protocol="sigv4"`.
- Distribution origin = the MediaPackage egress domain, `custom_origin_config`
  (`https-only`), `origin_access_control_id=oac.attr_id`, CachingDisabled policy.
- After deploy, attach an endpoint policy per endpoint
  (`mediapackagev2.put_origin_endpoint_policy`) allowing principal
  `cloudfront.amazonaws.com` with `AWS:SourceArn = <distribution arn>`. (There is a
  `cmd_policy` shape for this in git history of `channel.py`.)

---

## Useful diagnostics (apply to both v1 and v2)

CloudWatch `AWS/MediaLive` (dimensions `ChannelId`, `Pipeline`, and for output
metrics `OutputGroupName`):
- `Scte35InputMessage` — did MediaLive **detect** SCTE on the input?
- `Scte35OutputMessageAllOutputsTotalEmitted` — did MediaLive **emit** SCTE?
- `Output4xxErrors` / `Output5xxErrors` / `ActiveOutputs` — is the ingest delivery healthy?

MediaLive **as‑run** CloudWatch log stream shows per‑output timecodes and confirms a
`SCTE35` track exists. The egress manifest is the ground truth for whether markers
actually surface.

If the AWS CLI lacks `mediapackagev2`, update it (the bundled v2.8 era CLI cannot
even deserialize `CmafIngestGroupSettings` in `describe-channel`, showing `{}`).
