import os
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_s3 as s3,
    aws_iam as iam,
    aws_medialive as medialive,
    aws_mediapackage as mediapackage,
)
from constructs import Construct


# SCTE-35 ad triggers to act on (mirrors the known-good reference channel).
AD_TRIGGERS = [
    "SPLICE_INSERT",
    "PROVIDER_ADVERTISEMENT",
    "DISTRIBUTOR_ADVERTISEMENT",
    "PROVIDER_PLACEMENT_OPPORTUNITY",
    "DISTRIBUTOR_PLACEMENT_OPPORTUNITY",
]

MP_CHANNEL_ID = "loop-test-channel"


class MediaLiveLoopStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ts_file    = config.get("input", {}).get("ts_file", "")
        bucket_name = config.get("s3", {}).get("bucket_name", "")
        s3_folder  = config.get("s3", {}).get("folder", "").strip("/")

        if not ts_file or not bucket_name:
            # During cdk bootstrap no config values are required — return empty stack.
            return

        ts_filename = os.path.basename(ts_file)
        s3_key      = f"{s3_folder}/{ts_filename}" if s3_folder else ts_filename

        # ── S3 (existing bucket, referenced only — upload via channel.py upload) ──
        bucket = s3.Bucket.from_bucket_name(self, "ExistingBucket", bucket_name)

        # ── IAM role for MediaLive ────────────────────────────────────────────
        medialive_role = iam.Role(
            self,
            "MediaLiveRole",
            assumed_by=iam.ServicePrincipal("medialive.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMReadOnlyAccess")
            ],
        )

        medialive_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/*"],
            )
        )

        # MediaPackage v1: MediaLive looks up the channel's ingest endpoints by
        # ChannelId, so it needs DescribeChannel.
        medialive_role.add_to_policy(
            iam.PolicyStatement(
                actions=["mediapackage:DescribeChannel"],
                resources=["*"],
            )
        )

        medialive_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            )
        )

        medialive_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # ── MediaPackage v1 channel ───────────────────────────────────────────
        # The reference SCTE-35 channel uses MediaPackage v1 fed by MediaLive's
        # native MediaPackage output group. v1 reads SCTE-35 from the ingested
        # stream and emits ad markers per the endpoint AdTriggers — no CMAF
        # ingest, no avail config, no CloudFront/SigV4 needed (endpoints are
        # publicly reachable).
        mp_channel = mediapackage.CfnChannel(
            self,
            "MpChannel",
            id=MP_CHANNEL_ID,
            description="Loop test SCTE-35 channel",
        )

        hls_endpoint = mediapackage.CfnOriginEndpoint(
            self,
            "HlsEndpointV1",
            channel_id=MP_CHANNEL_ID,
            id="loop-test-hls",
            manifest_name="index",
            startover_window_seconds=0,
            hls_package=mediapackage.CfnOriginEndpoint.HlsPackageProperty(
                ad_markers="SCTE35_ENHANCED",
                ad_triggers=AD_TRIGGERS,
                ads_on_delivery_restrictions="BOTH",
                segment_duration_seconds=6,
                playlist_window_seconds=60,
                program_date_time_interval_seconds=1,
            ),
        )
        hls_endpoint.add_dependency(mp_channel)

        dash_endpoint = mediapackage.CfnOriginEndpoint(
            self,
            "DashEndpointV1",
            channel_id=MP_CHANNEL_ID,
            id="loop-test-dash",
            manifest_name="index",
            startover_window_seconds=0,
            dash_package=mediapackage.CfnOriginEndpoint.DashPackageProperty(
                ad_triggers=AD_TRIGGERS,
                ads_on_delivery_restrictions="BOTH",
                period_triggers=["ADS"],
                segment_duration_seconds=6,
                manifest_window_seconds=60,
                min_buffer_time_seconds=10,
                min_update_period_seconds=2,
                suggested_presentation_delay_seconds=20,
                segment_template_format="TIME_WITH_TIMELINE",
            ),
        )
        dash_endpoint.add_dependency(mp_channel)

        # ── MediaLive input security group ────────────────────────────────────
        input_sg = medialive.CfnInputSecurityGroup(
            self,
            "InputSecurityGroup",
            whitelist_rules=[
                medialive.CfnInputSecurityGroup.InputWhitelistRuleCidrProperty(
                    cidr="0.0.0.0/0"
                )
            ],
        )

        # ── MediaLive input ───────────────────────────────────────────────────
        ml_input = medialive.CfnInput(
            self,
            "TsFileInput",
            type="TS_FILE",
            name="loop-test-input",
            role_arn=medialive_role.role_arn,
            input_security_groups=[input_sg.ref],
            sources=[
                medialive.CfnInput.InputSourceRequestProperty(
                    url=f"s3ssl://{bucket_name}/{s3_key}"
                )
            ],
        )

        # ── MediaLive channel ─────────────────────────────────────────────────
        # Pure SCTE-35 passthrough to MediaPackage v1 (no availConfiguration, no
        # globalConfiguration), mirroring the known-good reference channel.
        encoder_settings = {
            "timecodeConfig": {"source": "SYSTEMCLOCK"},
            "audioDescriptions": [
                {
                    "name": "audio_1",
                    "audioSelectorName": "default",
                    "codecSettings": {
                        "aacSettings": {
                            "bitrate": 128000,
                            "sampleRate": 48000,
                            "spec": "MPEG4",
                            "profile": "LC",
                            "rawFormat": "NONE",
                            "codingMode": "CODING_MODE_2_0",
                            "inputType": "NORMAL",
                            "rateControlMode": "CBR",
                        }
                    },
                }
            ],
            "videoDescriptions": [
                {
                    "name": "video_1",
                    "width": 1920,
                    "height": 1080,
                    "respondToAfd": "NONE",
                    "sharpness": 50,
                    "scalingBehavior": "DEFAULT",
                    "codecSettings": {
                        "h264Settings": {
                            "bitrate": 5000000,
                            "maxBitrate": 5000000,
                            "framerateControl": "SPECIFIED",
                            "framerateNumerator": 25,
                            "framerateDenominator": 1,
                            "rateControlMode": "QVBR",
                            "profile": "HIGH",
                            "level": "H264_LEVEL_AUTO",
                            "gopSize": 50,
                            "gopSizeUnits": "FRAMES",
                            "gopBLength": 2,
                            "gopNumBFrames": 2,
                            "scanType": "PROGRESSIVE",
                            "entropyEncoding": "CABAC",
                            "flickerAq": "ENABLED",
                            "spatialAq": "ENABLED",
                            "temporalAq": "ENABLED",
                            "adaptiveQuantization": "HIGH",
                            "lookAheadRateControl": "HIGH",
                            "afdSignaling": "NONE",
                            "colorMetadata": "INSERT",
                            "parControl": "SPECIFIED",
                            "parNumerator": 1,
                            "parDenominator": 1,
                        }
                    },
                }
            ],
            "outputGroups": [
                medialive.CfnChannel.OutputGroupProperty(
                    name="MediaPackageGroup",
                    output_group_settings=medialive.CfnChannel.OutputGroupSettingsProperty(
                        media_package_group_settings=medialive.CfnChannel.MediaPackageGroupSettingsProperty(
                            destination=medialive.CfnChannel.OutputLocationRefProperty(
                                destination_ref_id="mp-dest"
                            )
                        )
                    ),
                    outputs=[
                        medialive.CfnChannel.OutputProperty(
                            output_name="output_1",
                            video_description_name="video_1",
                            audio_description_names=["audio_1"],
                            caption_description_names=[],
                            output_settings=medialive.CfnChannel.OutputSettingsProperty(
                                media_package_output_settings=medialive.CfnChannel.MediaPackageOutputSettingsProperty()
                            ),
                        )
                    ],
                )
            ],
        }

        ml_channel = medialive.CfnChannel(
            self,
            "MediaLiveChannel",
            name="loop-test-channel",
            channel_class="SINGLE_PIPELINE",
            role_arn=medialive_role.role_arn,
            input_attachments=[
                medialive.CfnChannel.InputAttachmentProperty(
                    input_attachment_name="ts-file-input",
                    input_id=ml_input.ref,
                    input_settings=medialive.CfnChannel.InputSettingsProperty(
                        source_end_behavior="LOOP",
                        input_filter="AUTO",
                        filter_strength=1,
                        deblock_filter="DISABLED",
                        denoise_filter="DISABLED",
                    ),
                )
            ],
            destinations=[
                medialive.CfnChannel.OutputDestinationProperty(
                    id="mp-dest",
                    media_package_settings=[
                        medialive.CfnChannel.MediaPackageOutputDestinationSettingsProperty(
                            channel_id=MP_CHANNEL_ID
                        )
                    ],
                ),
            ],
            encoder_settings=encoder_settings,
        )
        ml_channel.node.add_dependency(mp_channel)

        # ── CloudFormation outputs ────────────────────────────────────────────
        cdk.CfnOutput(self, "S3BucketName", value=bucket_name)
        cdk.CfnOutput(self, "S3TsKey", value=s3_key)
        cdk.CfnOutput(self, "MediaLiveChannelId", value=ml_channel.ref)
        cdk.CfnOutput(self, "MediaPackageChannelId", value=MP_CHANNEL_ID)
        cdk.CfnOutput(self, "HlsPlaybackUrl", value=hls_endpoint.attr_url)
        cdk.CfnOutput(self, "DashPlaybackUrl", value=dash_endpoint.attr_url)
        cdk.CfnOutput(self, "MediaLiveRoleArn", value=medialive_role.role_arn)
