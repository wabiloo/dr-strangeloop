"""What to play: a channel (via its-a-live status) or plain URLs."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass
class Target:
    name: str
    hls_url: str | None
    dash_url: str | None
    # Service root of a loop-dee-loop serve.py (has /timeline.json); None for
    # backends without one (aws-media) or plain manifest URLs.
    timeline_url: str | None = None

    def url_for(self, fmt: str) -> str | None:
        return self.hls_url if fmt == "hls" else self.dash_url

    def formats(self) -> list[str]:
        return [f for f in ("hls", "dash") if self.url_for(f)]


def _root_of(manifest_url: str) -> str:
    return manifest_url.rsplit("/", 1)[0]


def from_manifest_urls(hls: str | None, dash: str | None, timeline: str | None = None) -> Target:
    if not hls and not dash:
        raise ValueError("give at least one of an HLS or a DASH manifest URL")
    root = _root_of(hls or dash)
    return Target(name=root, hls_url=hls, dash_url=dash, timeline_url=timeline or f"{root}/timeline.json")


def resolve_channel_config(channel: str) -> Path:
    p = Path(channel)
    if p.suffix == ".toml" and p.is_file():
        return p.resolve()
    cand = paths.channels_dir() / f"{channel}.toml"
    if cand.is_file():
        return cand
    raise FileNotFoundError(f"no channel config {channel!r} (looked for {cand})")


def from_channel(channel: str) -> Target:
    cfg = resolve_channel_config(channel)
    cmd = ["uv", "run", "--project", str(paths.ITS_A_LIVE_DIR), "python", "channel.py", "--config", str(cfg), "--json", "status"]
    res = subprocess.run(cmd, cwd=paths.ITS_A_LIVE_DIR, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"channel.py status failed:\n{res.stderr or res.stdout}")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    status = json.loads(lines[-1])
    hls, dash = status.get("hls_url"), status.get("dash_url")
    if not hls and not dash:
        raise RuntimeError(f"channel {cfg.stem!r} has no playback URLs (status: {status.get('status')})")
    backend = status.get("backend")
    root = _root_of(hls or dash)
    return Target(
        name=cfg.stem,
        hls_url=hls,
        dash_url=dash,
        timeline_url=f"{root}/timeline.json" if backend in ("local-docker", "ecs-express") else None,
    )
