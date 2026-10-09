"""Non-browser reference: ffmpeg demuxes the manifest for N seconds and we count its complaints."""

from __future__ import annotations

import re
import shutil
import subprocess

_BAD = re.compile(r"error|invalid|corrupt|non[- ]monoton|missing|failed|discontinuity", re.I)
_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def ffmpeg_check(url: str, seconds: float) -> dict:
    exe = shutil.which("ffmpeg")
    if not exe:
        return {"url": url, "skipped": "ffmpeg not found"}
    cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "warning", "-stats", "-t", str(seconds),
           "-i", url, "-c", "copy", "-f", "null", "-"]
    limit = seconds * 1.5 + 30  # live input is read in real time once it catches up with the edge
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, errors="replace")
    timed_out = False
    try:
        err = proc.communicate(timeout=limit)[1]
    except subprocess.TimeoutExpired:
        proc.kill()
        err = proc.communicate()[1]
        timed_out = True
    chunks = err.replace("\r", "\n").splitlines()
    progress = [m for m in (_TIME.search(c) for c in chunks) if m]
    last = None
    if progress:
        h, m, s = progress[-1].groups()
        last = round(int(h) * 3600 + int(m) * 60 + float(s), 1)
    lines = [c for c in chunks if c.strip() and not c.startswith("frame=") and "duplicated MOOV" not in c]
    problems = [c for c in lines if _BAD.search(c)]
    if timed_out:
        problems.insert(0, f"did not finish within {limit:.0f}s; last progress time={last}s of {seconds:g}s")
    return {"url": url, "exitCode": proc.returncode, "timedOut": timed_out, "lastProgressS": last,
            "warnings": len(lines), "problems": problems[:10],
            "pass": proc.returncode == 0 and not problems and not timed_out}
