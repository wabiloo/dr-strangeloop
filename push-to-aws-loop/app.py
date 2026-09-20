#!/usr/bin/env python3
import os
import tomllib
import aws_cdk as cdk
from loop_channel_stack import LoopChannelStack
from shared_stack import LoopSharedStack

app = cdk.App()

# -c config=./path/to/config.toml  (default: config.toml next to this file)
config_path = app.node.try_get_context("config") or os.path.join(
    os.path.dirname(__file__), "config.toml"
)
with open(config_path, "rb") as f:
    config = tomllib.load(f)

# -c name=...  overrides [deploy].name in the config file
name_override = app.node.try_get_context("name")
if name_override:
    config.setdefault("deploy", {})["name"] = name_override

# -c source_path=...  overrides [input].source_path in the config file
source_path_override = app.node.try_get_context("source_path")
if source_path_override:
    config.setdefault("input", {})["source_path"] = source_path_override

name = config.get("deploy", {}).get("name", "default")

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=config["aws"]["region"],
)

# Shared prerequisite for every channel (one ECS cluster, "loop-dee-loop",
# shared by name) -- deploy explicitly once per account/region with
# `cdk deploy LoopSharedStack`, before deploying any LoopChannelStack.
# Always present in the synthesized app (so `cdk list`/`cdk synth` show it
# and CDK can resolve LoopChannelStack's reference to CLUSTER_NAME), but
# `cdk deploy` without an explicit stack name only deploys the stack that
# matches the current config -- see channel.py's `redeploy`, which always
# targets f"LoopChannelStack-{name}" explicitly for exactly this reason.
LoopSharedStack(app, "LoopSharedStack", env=env)

LoopChannelStack(
    app,
    f"LoopChannelStack-{name}",
    config=config,
    env=env,
)
app.synth()
