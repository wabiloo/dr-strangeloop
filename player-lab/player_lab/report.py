"""Terminal summary of a run report."""

from __future__ import annotations


def _cell(v) -> str:
    return "-" if v is None else str(v)


def format_table(report: dict) -> str:
    rows = [("player", "fmt", "ver", "start", "stalls", "stall s", "errors", "periods", "expect", "result")]
    for c in report["cases"]:
        rows.append((
            c["player"], c["format"], _cell(c.get("version")), _cell(c.get("startedAfterS")),
            _cell(c.get("stallCount")), _cell(c.get("stallSeconds")), _cell(c.get("fatalErrors")),
            _cell(c.get("periodTransitions")), _cell(c.get("crossed")),
            "PASS" if c["pass"] else "FAIL",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    for c in report["cases"]:
        for why in c["failures"]:
            lines.append(f"  {c['player']}/{c['format']}: {why}")
        for w in c.get("warnings", []):
            lines.append(f"  {c['player']}/{c['format']}: (warning) {w}")
        for e in c.get("errors", [])[:3]:
            lines.append(f"  {c['player']}/{c['format']}: error: {e.get('message', '')[:140]}")
    for r in report.get("ffmpeg", []):
        if "skipped" in r:
            lines.append(f"  ffmpeg: skipped ({r['skipped']})")
            continue
        lines.append(f"  ffmpeg (info) {'ok' if r['pass'] else 'PROBLEMS'}: {r['url']} exit={r['exitCode']} warnings={r['warnings']}")
        lines += [f"    {ln[:140]}" for ln in r["problems"][:3]]
    b = report["boundaries"]
    lines.append("")
    lines.append(f"boundaries: new periods={_cell(b['newPeriods'])} new discontinuities={_cell(b['newDiscontinuities'])} "
                 f"(source: {b['source']}, {b['timelinePolls']} polls)")
    lines.append(f"{'PASS' if report['pass'] else 'FAIL'} in {report['durationS']}s; report: {report['outDir']}/report.json")
    return "\n".join(lines)
