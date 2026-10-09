"""Judge results collected by the in-browser driver (harness/browser.js).

The browser only measures; thresholds are applied here so a browser run and a
headless run are judged by the same code and produce the same report shape.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .profile import Profile, judge


def judge_payload(payload: dict, profile: Profile, out_dir: Path | None = None) -> dict:
    boundaries = dict(payload.get("boundaries") or {})
    crossed_by = boundaries.pop("crossed", None) or {}
    cases = []
    for item in payload["cases"]:
        player, fmt = item["player"], item["format"]
        crossed = crossed_by.get(fmt)
        case = dict(item.get("snapshot") or {})
        case.update(player=player, format=fmt, crossed=crossed, network=None)
        case["failures"] = judge(case, crossed, profile.for_player(player, fmt))
        case["pass"] = not case["failures"]
        cases.append(case)
    target = payload.get("target") or {}
    report = {
        "generatedAt": payload.get("startedAt") or dt.datetime.now(dt.timezone.utc).isoformat(),
        "durationS": payload.get("durationS"),
        "mode": "browser",
        "userAgent": payload.get("userAgent"),
        "target": {"name": target.get("name"), "hls": target.get("hls"), "dash": target.get("dash"),
                   "timeline": target.get("timeline")},
        "boundaries": boundaries,
        "cases": cases,
        "pass": all(c["pass"] for c in cases),
        "outDir": str(out_dir) if out_dir else None,
    }
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    return report
