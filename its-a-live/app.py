#!/usr/bin/env python3
import os
import tomllib
import aws_cdk as cdk
from _infra_cfg import infra_table, reject_legacy_tables
from _paths_cfg import resolve_source_path
from media_stack import MediaStack
from loop_stack import LoopStack
from loop_shared_stack import LoopSharedStack
from scheduler_stack import SchedulerStack

app = cdk.App()

# -c config=./path/to/config.toml  (default: config.toml next to this file)
config_path = app.node.try_get_context("config") or os.path.join(
    os.path.dirname(__file__), "config.toml"
)
with open(config_path, "rb") as f:
    config = tomllib.load(f)
reject_legacy_tables(config, config_path)
if config.get("input", {}).get("source_path"):
    config["input"]["source_path"] = resolve_source_path(config["input"]["source_path"], config_path)

# -c name=...  overrides [deploy].name in the config file
name_override = app.node.try_get_context("name")
if name_override:
    config.setdefault("deploy", {})["name"] = name_override

# -c backend=...  overrides [deploy].backend in the config file
backend_override = app.node.try_get_context("backend")
if backend_override:
    config.setdefault("deploy", {})["backend"] = backend_override

# -c source_path=...  overrides [input].source_path in the config file
source_path_override = app.node.try_get_context("source_path")
if source_path_override:
    config.setdefault("input", {})["source_path"] = source_path_override

name = config.get("deploy", {}).get("name", "default")
backend = config.get("deploy", {}).get("backend")
if backend not in ("aws-media", "ecs-express"):
    raise SystemExit(
        f"[deploy].backend must be 'aws-media' or 'ecs-express', got {backend!r}"
    )

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=infra_table(config, "aws")["region"],
)

# Stack naming: ItsALiveStack-<name>-<backend> -- the backend suffix keeps
# e.g. a "demo" channel on aws-media and a "demo" channel on ecs-express
# from colliding if you ever want both side by side.
stack_name = f"ItsALiveStack-{name}-{backend}"


# Shared prerequisite for `channel.py schedule add/remove/list` -- not
# backend-specific (covers both aws-media and ecs-express). Only included
# with `-c scheduler=true`: its Lambda asset is Docker-bundled, and CDK
# stages every asset of every stack in the app on every command, so
# including it always made every channel deploy/destroy pay for the bundling.
# Deploy it explicitly, once per account/region, before scheduling any
# channel's start/stop:
#   cdk deploy ItsALiveSharedStack-scheduler -c scheduler=true
if str(app.node.try_get_context("scheduler")).lower() in ("true", "1", "yes"):
    SchedulerStack(app, "ItsALiveSharedStack-scheduler", env=env)

if backend == "ecs-express":
    # Shared prerequisite -- one ECS cluster reused by every ecs-express
    # channel (see loop_shared_stack.py). Always present in the
    # synthesized app so `cdk list`/`cdk synth` show it and LoopStack can
    # resolve its CLUSTER_NAME reference; deploy it explicitly with
    # `cdk deploy ItsALiveSharedStack-ecs-express` before any channel.
    LoopSharedStack(app, "ItsALiveSharedStack-ecs-express", env=env)
    LoopStack(app, stack_name, config=config, env=env)
else:
    MediaStack(app, stack_name, config=config, env=env)

app.synth()
