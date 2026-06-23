#!/usr/bin/env python3
import os
import tomllib
import aws_cdk as cdk
from scte_loop_stack import ScteLoopStack

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

# -c ts_file=...  overrides [input].ts_file in the config file
ts_file_override = app.node.try_get_context("ts_file")
if ts_file_override:
    config.setdefault("input", {})["ts_file"] = ts_file_override

name = config.get("deploy", {}).get("name", "default")

ScteLoopStack(
    app,
    f"ScteLoopStack-{name}",
    config=config,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=config["aws"]["region"],
    ),
)
app.synth()
