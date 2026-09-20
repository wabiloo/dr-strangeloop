#!/usr/bin/env python3
"""
galvanise.py — Full pipeline runner for dr-strangeloop.

Usage:
  uv run python galvanise.py <config.yaml> [--backend aws-media|ecs-express]

The config.yaml should be a franken-ts config (e.g. franken-ts/configs/foo.yaml).
Paths may be absolute or relative to the current working directory.

Pipeline (its-a-live backend selected by --backend, default aws-media):
  1. Run franken-ts to build the .ts file
  2. Generate configs/<basename>.toml at the repo root
  3. (ecs-express only) Ensure the shared ECS cluster stack is deployed
  4. Run CDK deploy (cdk deploy) for the channel stack
  5. Spark: stage the input for the chosen backend (upload .ts for
     aws-media; bake it locally via GPAC + push to S3 for ecs-express)
  6. Start the channel

Each step prompts for confirmation before running.
All channel management commands (spark/start/stop/refresh) can be run
from the repo root using the generated TOML in configs/.
"""

import os
import shutil
import sys
import subprocess

# PyYAML is available via franken-ts; load lazily so the import error is clear.
try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required. Run: uv sync --all-packages")


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"


def _header(step: int, title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  Step {step}: {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")


def _confirm(prompt: str) -> bool:
    """Ask y/n; return True if the user confirms."""
    while True:
        answer = input(f"{BOLD}{YELLOW}{prompt} [y/n]: {RESET}").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _run(cmd: list[str], cwd: str | None = None) -> None:
    """Run a command, streaming output. Exits the script on non-zero return code."""
    display = " ".join(cmd)
    if cwd:
        display += f"  (in {os.path.relpath(cwd)})"
    print(f"{GREEN}$ {display}{RESET}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"{RED}Command failed with exit code {result.returncode}.{RESET}")
        sys.exit(result.returncode)


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# TOML generation
# ---------------------------------------------------------------------------

_AWS_MEDIA_TOML_TEMPLATE = """\
[deploy]
name = "{name}"
backend = "aws-media"

[aws]
region = "us-east-1"

[s3]
bucket_name = "bpkio-cs-demos"
content_folder = "fabre/ts-files-with-scte"  # prefix inside the bucket

[input]
source_path = "{ts_file}"
"""

_ECS_EXPRESS_TOML_TEMPLATE = """\
[deploy]
name = "{name}"
backend = "ecs-express"

[aws]
region = "eu-west-1"

[s3]
bucket_name = "bpkio-cs-demos"
content_folder = "fabre/its-a-live"  # prefix inside the bucket

[input]
source_path = "{ts_file}"

[channel]
segment_duration   = 4.0
dvr_window_seconds = 30
port               = 8080

[express]
cpu    = 256   # 0.25 vCPU
memory = 512   # 0.5 GB
"""

_TOML_TEMPLATES = {
    "aws-media": _AWS_MEDIA_TOML_TEMPLATE,
    "ecs-express": _ECS_EXPRESS_TOML_TEMPLATE,
}


def _generate_toml(backend: str, name: str, ts_file_abs: str) -> str:
    return _TOML_TEMPLATES[backend].format(name=name, ts_file=ts_file_abs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="galvanise.py",
        description="Full pipeline runner for dr-strangeloop.",
    )
    parser.add_argument("config_yaml", metavar="config.yaml",
                        help="franken-ts YAML config file.")
    parser.add_argument("--backend", choices=["aws-media", "ecs-express"],
                        default="aws-media",
                        help="its-a-live backend to deploy to: 'aws-media' "
                             "(MediaLive + MediaPackage, default) or "
                             "'ecs-express' (loop-dee-loop on ECS Express "
                             "Mode + CloudFront).")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Delete the franken-ts clip cache (~/.cache/franken_ts) "
                             "before running Step 1.")
    args = parser.parse_args()
    backend = args.backend

    config_yaml = os.path.abspath(args.config_yaml)
    if not os.path.isfile(config_yaml):
        sys.exit(f"Config file not found: {config_yaml}")

    repo_root    = _repo_root()
    franken_dir  = os.path.join(repo_root, "franken-ts")
    push_dir     = os.path.join(repo_root, "its-a-live")
    configs_dir  = os.path.join(repo_root, "configs")

    # Derive basename (e.g. "break-and-popos") from the YAML filename.
    basename = os.path.splitext(os.path.basename(config_yaml))[0]

    # Parse the YAML to discover the output .ts path.
    with open(config_yaml) as f:
        cfg = yaml.safe_load(f)

    ts_file_raw = cfg.get("output", {}).get("file", "")
    if not ts_file_raw:
        sys.exit("Could not find output.file in the YAML config.")

    # franken-ts runs with cwd=franken-ts/, so output.file is resolved there.
    ts_file_abs = os.path.normpath(os.path.join(franken_dir, ts_file_raw))

    # Config path relative to franken-ts/ for the franken-ts invocation.
    config_rel_to_franken = os.path.relpath(config_yaml, franken_dir)

    toml_path = os.path.join(configs_dir, f"{basename}.toml")

    # channel.py and cdk both receive the absolute TOML path so they work
    # regardless of the working directory.
    channel_py = os.path.join(push_dir, "channel.py")

    # Print a summary before starting.
    print(f"\n{BOLD}Pipeline summary{RESET}")
    print(f"  YAML config : {config_yaml}")
    print(f"  Backend     : {backend}")
    print(f"  Basename    : {basename}")
    print(f"  Output .ts  : {ts_file_abs}")
    print(f"  TOML config : {toml_path}")

    # ------------------------------------------------------------------
    # Optional: clear franken-ts clip cache
    # ------------------------------------------------------------------
    if args.clear_cache:
        cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "franken_ts")
        if os.path.isdir(cache_dir):
            print(f"\n{BOLD}{YELLOW}Clearing cache: {cache_dir}{RESET}")
            shutil.rmtree(cache_dir)
            print(f"{GREEN}Cache cleared.{RESET}")
        else:
            print(f"\n{YELLOW}Cache directory not found (nothing to clear): {cache_dir}{RESET}")

    # ------------------------------------------------------------------
    # Step 1 — franken-ts
    # ------------------------------------------------------------------
    _header(1, "Build .ts with SCTE-35 markers (franken-ts)")
    print(f"  cwd        : {franken_dir}")
    print(f"  command    : uv run franken-ts {config_rel_to_franken}")
    if not _confirm("Run franken-ts now?"):
        print("Skipped.")
    else:
        _run(["uv", "run", "franken-ts", config_rel_to_franken], cwd=franken_dir)
        print(f"{GREEN}franken-ts complete.{RESET}")

    # ------------------------------------------------------------------
    # Step 2 — Generate TOML config
    # ------------------------------------------------------------------
    _header(2, f"Generate TOML config (configs/{basename}.toml)")
    toml_content = _generate_toml(backend=backend, name=basename, ts_file_abs=ts_file_abs)
    print(f"  Will write : {toml_path}\n")
    print("  Content preview:")
    for line in toml_content.splitlines():
        print(f"    {line}")
    print()
    if not _confirm("Write TOML config?"):
        print("Skipped.")
    else:
        os.makedirs(configs_dir, exist_ok=True)
        with open(toml_path, "w") as f:
            f.write(toml_content)
        print(f"{GREEN}TOML written to {toml_path}{RESET}")

    # ------------------------------------------------------------------
    # Step 3 — (ecs-express only) ensure the shared ECS cluster stack
    # ------------------------------------------------------------------
    step = 3
    if backend == "ecs-express":
        _header(step, "Ensure shared ECS cluster stack is deployed (ItsALiveSharedStack-ecs-express)")
        shared_cmd = [
            "cdk", "deploy",
            "--require-approval", "never",
            "-c", f"config={toml_path}",
            "ItsALiveSharedStack-ecs-express",
        ]
        print(f"  cwd        : {push_dir}")
        print(f"  command    : {' '.join(shared_cmd)}")
        if not _confirm("Deploy the shared cluster stack now? (safe/idempotent if already deployed)"):
            print("Skipped.")
        else:
            _run(shared_cmd, cwd=push_dir)
            print(f"{GREEN}Shared stack ready.{RESET}")
        step += 1

    # ------------------------------------------------------------------
    # Step — CDK deploy (channel stack)
    # ------------------------------------------------------------------
    _header(step, "Deploy AWS stack (cdk deploy)")
    stack_name = f"ItsALiveStack-{basename}-{backend}"
    cdk_cmd = [
        "cdk", "deploy",
        "--require-approval", "never",
        "-c", f"config={toml_path}",
        stack_name,
    ]
    print(f"  cwd        : {push_dir}")
    print(f"  command    : {' '.join(cdk_cmd)}")
    if not _confirm("Run cdk deploy now?"):
        print("Skipped.")
    else:
        _run(cdk_cmd, cwd=push_dir)
        print(f"{GREEN}CDK deploy complete.{RESET}")
    step += 1

    # ------------------------------------------------------------------
    # Step — Spark (stage the input for the chosen backend)
    # ------------------------------------------------------------------
    spark_title = ("Spark: upload .ts to S3" if backend == "aws-media"
                   else "Spark: bake locally (GPAC) and push loop package to S3")
    _header(step, spark_title)
    spark_cmd = [
        "uv", "run", "--project", push_dir, "python", channel_py,
        "-c", toml_path,
        "spark",
    ]
    print(f"  command    : {' '.join(spark_cmd)}")
    spark_skipped = not _confirm("Run spark now?")
    if spark_skipped:
        print("Skipped.")
    else:
        _run(spark_cmd)
        print(f"{GREEN}Spark complete.{RESET}")
    step += 1

    # ------------------------------------------------------------------
    # Step — Start channel
    # ------------------------------------------------------------------
    start_title = ("Start the MediaLive channel" if backend == "aws-media"
                   else "Start the channel (scale ECS Express service to 1 task)")
    _header(step, start_title)
    start_cmd = [
        "uv", "run", "--project", push_dir, "python", channel_py,
        "-c", toml_path,
        "start",
    ]
    print(f"  command    : {' '.join(start_cmd)}")
    start_skipped = not _confirm("Start the channel now?")
    if start_skipped:
        print("Skipped.")
    else:
        _run(start_cmd)
        print(f"{GREEN}Channel started.{RESET}")

    # ------------------------------------------------------------------
    # Follow-up instructions
    # ------------------------------------------------------------------
    # (stack_name was set above)

    # Display-friendly versions using paths relative to the repo root.
    _channel_py_rel  = os.path.relpath(channel_py, repo_root)
    _toml_rel        = os.path.relpath(toml_path, repo_root)
    _push_dir_rel    = os.path.relpath(push_dir, repo_root)
    # ... and relative to push_dir, for the "cd its-a-live" destroy step below.
    _toml_rel_from_push_dir = os.path.relpath(toml_path, push_dir)
    channel_display  = f"uv run --project {_push_dir_rel} python {_channel_py_rel} -c {_toml_rel}"

    print(f"\n{BOLD}{GREEN}All steps complete.{RESET}")
    print(f"{BOLD}(All commands below can be run from the repo root){RESET}")

    if spark_skipped or start_skipped:
        print(f"\n{BOLD}To finish manually:{RESET}")
        if spark_skipped:
            print(f"  Spark   : {channel_display} spark")
        if start_skipped:
            print(f"  Start   : {channel_display} start")

    print(f"\n{BOLD}To update content on a running channel:{RESET}")
    print(f"  {channel_display} spark      # re-bake/re-upload new content to S3")
    print(f"  {channel_display} refresh    # pick it up on the running channel")

    print(f"\n{BOLD}To stop the channel:{RESET}")
    print(f"  {channel_display} stop")

    print(f"\n{BOLD}To destroy the stack (stops billing):{RESET}")
    print(f"  {channel_display} stop           # stop the channel first if running")
    print(f"  cd {_push_dir_rel}")
    print(f"  cdk destroy {stack_name} -c config={_toml_rel_from_push_dir}")
    if backend == "ecs-express":
        print(f"  # Note: ItsALiveSharedStack-ecs-express is shared across every")
        print(f"  # ecs-express channel -- leave it deployed if you have others.")
    print()


if __name__ == "__main__":
    main()
