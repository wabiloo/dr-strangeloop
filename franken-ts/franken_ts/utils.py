import re
import shutil
import subprocess
import logging
from pathlib import Path

import pytimeparse

logger = logging.getLogger(__name__)

_TIMECODE_RE = re.compile(r'^(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?$')


def parse_time(value: str | int | float) -> float:
    """Return duration in seconds from any supported format.

    Accepts:
      - int/float         → plain seconds
      - "HH:MM:SS[.mmm]"  → timecode
      - "5 min 30 sec"    → pytimeparse expression
    """
    if isinstance(value, (int, float)):
        return float(value)

    value = str(value).strip()

    m = _TIMECODE_RE.match(value)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        frac = float(f"0.{m.group(4)}") if m.group(4) else 0.0
        return h * 3600 + mn * 60 + s + frac

    parsed = pytimeparse.parse(value)
    if parsed is not None:
        return float(parsed)

    raise ValueError(f"Cannot parse time value: {value!r}")


def check_tool(name: str) -> Path:
    """Return full path of an external tool or raise RuntimeError."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"Required tool '{name}' not found on PATH. "
            f"Please install it before running scte-maker."
        )
    return Path(path)


def run_cmd(
    cmd: list[str],
    *,
    dry_run: bool = False,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess command.

    Output is captured by default so Rich spinners are not disrupted.
    On failure the captured stderr is attached to the CalledProcessError
    so callers can display it.
    """
    display = " ".join(str(c) for c in cmd)
    logger.debug("$ %s", display)

    if dry_run:
        print(f"[dry-run] {display}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = subprocess.run(cmd, capture_output=capture, text=True, check=False)

    if result.returncode != 0 and check:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )

    return result


def format_pts(pts: int) -> str:
    """Format a PTS value with commas for tsduck XML readability (e.g. 16,200,000)."""
    return f"{pts:,}"
