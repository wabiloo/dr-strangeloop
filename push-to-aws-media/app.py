#!/usr/bin/env python3
import os
import tomllib
import aws_cdk as cdk
from scte_loop_stack import ScteLoopStack

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.toml")

with open(CONFIG_FILE, "rb") as f:
    config = tomllib.load(f)

app = cdk.App()

# -c ts_file=... overrides config.toml
ts_file_override = app.node.try_get_context("ts_file")
if ts_file_override:
    config.setdefault("input", {})["ts_file"] = ts_file_override

ScteLoopStack(
    app,
    "ScteLoopStack",
    config=config,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=config["aws"]["region"],
    ),
)
app.synth()
