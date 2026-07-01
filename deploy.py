#!/usr/bin/env python3
"""
deploy.py — Full pipeline runner for live-scte-loop-generator.

Usage:
  uv run python deploy.py <config.yaml>

The config.yaml should be a franken-ts config (e.g. franken-ts/configs/foo.yaml).
Paths may be absolute or relative to the current working directory.

Pipeline:
  1. Run franken-ts to build the .ts file
  2. Generate configs/<basename>.toml at the repo root
  3. Run CDK deploy (cdk deploy)
  4. Upload .ts to S3
  5. Start the MediaLive channel

Each step prompts for confirmation before running.
All channel management commands (upload/start/stop) can be run from the
repo root using the generated TOML in configs/.
"""

import os
import sys
import subprocess
import textwrap

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

_TOML_TEMPLATE = """\
[deploy]
name = "{name}"

[aws]
region = "us-east-1"

[s3]
bucket_name = "bpkio-cs-demos"
folder = "fabre/ts-files-with-scte"          # prefix inside the bucket

[input]
ts_file = "{ts_file}"
"""


def _generate_toml(name: str, ts_file_abs: str) -> str:
    return _TOML_TEMPLATE.format(name=name, ts_file=ts_file_abs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 2:
        prog = os.path.basename(sys.argv[0])
        print(textwrap.dedent(f"""\
            Usage: uv run python {prog} <config.yaml>

            Example:
              uv run python {prog} franken-ts/configs/break-and-popos.yaml
        """))
        sys.exit(1)

    config_yaml = os.path.abspath(sys.argv[1])
    if not os.path.isfile(config_yaml):
        sys.exit(f"Config file not found: {config_yaml}")

    repo_root    = _repo_root()
    franken_dir  = os.path.join(repo_root, "franken-ts")
    push_dir     = os.path.join(repo_root, "push-to-aws-media")
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
    print(f"  Basename    : {basename}")
    print(f"  Output .ts  : {ts_file_abs}")
    print(f"  TOML config : {toml_path}")

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
    toml_content = _generate_toml(name=basename, ts_file_abs=ts_file_abs)
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
    # Step 3 — CDK deploy
    # ------------------------------------------------------------------
    _header(3, "Deploy AWS stack (cdk deploy)")
    cdk_cmd = [
        "cdk", "deploy",
        "--require-approval", "never",
        "-c", f"config={toml_path}",
    ]
    print(f"  cwd        : {push_dir}")
    print(f"  command    : {' '.join(cdk_cmd)}")
    if not _confirm("Run cdk deploy now?"):
        print("Skipped.")
    else:
        _run(cdk_cmd, cwd=push_dir)
        print(f"{GREEN}CDK deploy complete.{RESET}")

    # ------------------------------------------------------------------
    # Step 4 — Upload
    # ------------------------------------------------------------------
    _header(4, "Upload .ts to S3")
    upload_cmd = [
        "uv", "run", "python", channel_py,
        "-c", toml_path,
        "upload",
    ]
    print(f"  command    : {' '.join(upload_cmd)}")
    upload_skipped = not _confirm("Run upload now?")
    if upload_skipped:
        print("Skipped.")
    else:
        _run(upload_cmd)
        print(f"{GREEN}Upload complete.{RESET}")

    # ------------------------------------------------------------------
    # Step 5 — Start channel
    # ------------------------------------------------------------------
    _header(5, "Start the MediaLive channel")
    start_cmd = [
        "uv", "run", "python", channel_py,
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
    stack_name = f"ScteLoopStack-{basename}"

    # Display-friendly versions using paths relative to the repo root.
    _channel_py_rel  = os.path.relpath(channel_py, repo_root)
    _toml_rel        = os.path.relpath(toml_path, repo_root)
    _push_dir_rel    = os.path.relpath(push_dir, repo_root)
    channel_display  = f"uv run python {_channel_py_rel} -c {_toml_rel}"

    print(f"\n{BOLD}{GREEN}All steps complete.{RESET}")
    print(f"{BOLD}(All commands below can be run from the repo root){RESET}")

    if upload_skipped or start_skipped:
        print(f"\n{BOLD}To finish manually:{RESET}")
        if upload_skipped:
            print(f"  Upload  : {channel_display} upload")
        if start_skipped:
            print(f"  Start   : {channel_display} start")

    print(f"\n{BOLD}To stop the channel:{RESET}")
    print(f"  {channel_display} stop")

    print(f"\n{BOLD}To destroy the stack (stops billing):{RESET}")
    print(f"  {channel_display} stop           # stop the channel first if running")
    print(f"  cd {_push_dir_rel}")
    print(f"  cdk destroy {stack_name} -c config={_toml_rel}")
    print()


if __name__ == "__main__":
    main()
