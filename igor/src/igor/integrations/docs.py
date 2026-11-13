"""Catalog of the repository's Markdown documentation, served by Igor's
Docs section. Pages are read from the repo at request time (no copies), and
only catalogued paths are ever served."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from igor.paths import REPO_ROOT


@dataclass(frozen=True)
class DocEntry:
    slug: str
    title: str
    summary: str
    path: str  # relative to REPO_ROOT, POSIX separators


@dataclass(frozen=True)
class DocSection:
    id: str
    title: str
    entries: tuple[DocEntry, ...]


CATALOG: tuple[DocSection, ...] = (
    DocSection("start", "Getting started", (
        DocEntry("overview", "Overview", "The pipeline, backends and core concepts.", "docs/overview.md"),
        DocEntry("igor-guide", "Using Igor", "A tour of this console.", "docs/igor-guide.md"),
        DocEntry("cli-reference", "CLI & API reference", "Every command-line tool and HTTP API on one page.", "docs/cli-reference.md"),
        DocEntry("readme", "Project README", "Repository introduction.", "README.md"),
        DocEntry("agents", "Agent entrypoint", "Pipeline architecture, config locations and per-tool pointers.", "AGENTS.md"),
        DocEntry("docker-local", "Run Igor via Docker", "No toolchain install: everything in a container.", "DOCKER_LOCAL.md"),
    )),
    DocSection("low-latency", "Low latency", (
        DocEntry("low-latency", "Low latency (DASH and HLS)", "How it works, how to turn it on, per-request overrides, clock sync, backends, limits.", "docs/low-latency.md"),
        DocEntry("player-lab-agents", "player-lab", "Play a channel in several players and measure latency, stalls and errors.", "player-lab/AGENTS.md"),
    )),
    DocSection("franken-ts", "Content authoring: franken-ts", (
        DocEntry("franken-ts-readme", "franken-ts", "Build a .ts with SCTE-35 from a YAML playlist.", "franken-ts/README.md"),
        DocEntry("playlist-format", "Playlist format (YAML)", "Guide to the playlist file: output, assets, markers, optional on-screen display.", "docs/playlist-format.md"),
        DocEntry("generator-assets", "Generated assets (presets)", "Build a loop with no source media: ffmpeg test patterns and colours, with a picture of each preset.", "docs/generator-assets.md"),
        DocEntry("franken-ts-agents", "franken-ts reference", "YAML schema, marker types and CLI.", "franken-ts/AGENTS.md"),
        DocEntry("scte35-marker-rules", "SCTE-35 marker rules", "How Igor and franken-ts model and validate markers.", "SCTE35_MARKER_RULES.md"),
        DocEntry("scte35-numbering", "SCTE-35 segment numbering", "Cross-standard reference for segment numbering.", "scte35-segment-numbering-spec-comparison.md"),
    )),
    DocSection("its-a-live", "Deploy and run: its-a-live", (
        DocEntry("its-a-live-readme", "its-a-live", "Channel lifecycle across the four backends.", "its-a-live/README.md"),
        DocEntry("its-a-live-agents", "its-a-live reference", "Channel TOML schema and every channel.py command.", "its-a-live/AGENTS.md"),
        DocEntry("its-a-live-brief", "Architecture brief", "Original design brief for the AWS stacks.", "its-a-live/AGENT_BRIEF.md"),
        DocEntry("its-a-live-lambda", "aws-lambda backend", "Design and trade-offs of the Lambda + CloudFront backend.", "its-a-live/LAMBDA_BACKEND.md"),
        DocEntry("its-a-live-k8s", "Kubernetes backend", "Scope for a generic-Kubernetes backend.", "its-a-live/K8S_BACKEND.md"),
        DocEntry("its-a-live-mpv2", "MediaPackage v2", "Future work, not wired up.", "its-a-live/MEDIAPACKAGE_V2.md"),
    )),
    DocSection("loop-dee-loop", "Serving: loop-dee-loop", (
        DocEntry("loop-dee-loop-readme", "loop-dee-loop", "Bake and serve a loop as live HLS/DASH; startover and catchup.", "loop-dee-loop/README.md"),
        DocEntry("loop-dee-loop-agents", "loop-dee-loop reference", "When and how to run bake.py and serve.py directly.", "loop-dee-loop/AGENTS.md"),
        DocEntry("loop-dee-loop-scope", "Design scope", "Detailed design, section by section.", "loop-dee-loop/SCOPE.md"),
        DocEntry("loop-dee-loop-dash", "DASH conformance", "MPD conformance review.", "loop-dee-loop/DASH_CONFORMANCE.md"),
        DocEntry("loop-dee-loop-perfs", "Performance & sizing", "Capacity and sizing notes.", "loop-dee-loop/PERFS.md"),
    )),
    DocSection("grave-robber", "Capture and import: grave-robber", (
        DocEntry("grave-robber-readme", "grave-robber", "Derive a loop from a HAR/Proxyman capture or a VOD manifest.", "grave-robber/README.md"),
        DocEntry("grave-robber-agents", "grave-robber reference", "Commands and manifest format.", "grave-robber/AGENTS.md"),
        DocEntry("grave-robber-scope", "Design scope", "Detailed design.", "grave-robber/SCOPE.md"),
    )),
    DocSection("inspector-krogh", "Inspection: inspector-krogh", (
        DocEntry("inspector-krogh-readme", "inspector-krogh", "krogh and frame-extractor: verify a built .ts.", "inspector-krogh/README.md"),
    )),
    DocSection("igor", "Igor", (
        DocEntry("igor-readme", "Igor README", "Layout and run instructions.", "igor/README.md"),
        DocEntry("igor-agents", "Igor reference", "Backend API table, config-section rules, known gaps.", "igor/AGENTS.md"),
        DocEntry("igor-cloud", "Hosting off localhost", "Scoping for running Igor on a network.", "igor/CLOUD_DEPLOYMENT.md"),
        DocEntry("igor-k8s", "Igor on Kubernetes", "Scoping for running Igor itself on Kubernetes.", "igor/K8S_DEPLOYMENT.md"),
    )),
)

_BY_SLUG: dict[str, DocEntry] = {e.slug: e for s in CATALOG for e in s.entries}

IMAGES_DIR = REPO_ROOT / "docs" / "images"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
CHANNEL_API_SPEC = REPO_ROOT / "loop-dee-loop" / "openapi.yaml"


def _exists(entry: DocEntry) -> bool:
    return (REPO_ROOT / entry.path).is_file()


def catalog() -> list[dict]:
    """Sections with only the entries whose file exists in this checkout."""
    out = []
    for section in CATALOG:
        entries = [
            {"slug": e.slug, "title": e.title, "summary": e.summary, "path": e.path}
            for e in section.entries
            if _exists(e)
        ]
        if entries:
            out.append({"id": section.id, "title": section.title, "entries": entries})
    return out


def read_page(slug: str) -> dict | None:
    entry = _BY_SLUG.get(slug)
    if entry is None or not _exists(entry):
        return None
    path: Path = REPO_ROOT / entry.path
    return {
        "slug": entry.slug,
        "title": entry.title,
        "path": entry.path,
        "markdown": path.read_text(encoding="utf-8"),
    }


_REDOC_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Channel API (loop-dee-loop)</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><redoc spec-url="openapi.yaml"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script></body></html>
"""


def channel_api_html() -> str:
    return _REDOC_HTML


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
# A scan over the catalogued pages (a few dozen small files), split into
# sections at ATX headings. Each section's `anchor` is the heading id the
# frontend renderer assigns (utils/markdown.ts: GitHub-style slug, repeated
# slugs suffixed -1, -2...), so a result can open the page at that section.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_SNIPPET_BEFORE = 70
_SNIPPET_LENGTH = 190
MAX_RESULTS = 40
MAX_PER_PAGE = 6


def _heading_text(raw: str) -> str:
    """Visible text of a heading (what the renderer slugifies)."""
    text = _LINK_RE.sub(r"\1", raw)
    return text.replace("`", "").replace("*", "").strip()


def _slugify(text: str) -> str:
    kept = "".join(c for c in text.strip().lower() if c.isalnum() or c in "_-" or c.isspace())
    return re.sub(r"\s", "-", kept)


@dataclass
class _Section:
    heading: str
    anchor: str
    lines: list[str]


def _sections(markdown: str) -> list[_Section]:
    out = [_Section("", "", [])]
    seen: dict[str, int] = {}
    in_fence = False
    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            text = _heading_text(m.group(2))
            base = _slugify(text) or "section"
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.append(_Section(text, base if n == 0 else f"{base}-{n}", []))
        else:
            out[-1].lines.append(line)
    return out


def _plain(line: str) -> str:
    """Markdown noise out of a line, for display in a snippet."""
    line = _LINK_RE.sub(r"\1", line)
    line = line.replace("`", "").replace("**", "")
    line = re.sub(r"^\s*(?:[-*+]|\d+\.|>)\s+", "", line)
    line = re.sub(r"\s*\|\s*", "  ", line.strip().strip("|"))
    return re.sub(r"\s+", " ", line).strip()


def _snippet(body: list[str], terms: list[str]) -> list[dict]:
    """~190 characters of the first body line holding a term (starting a little
    before it), as [{"t": text, "hit": bool}] segments for highlighting."""
    lines = [_plain(ln) for ln in body]
    lines = [ln for ln in lines if ln and not set(ln) <= set("-: ")]
    chosen = next((ln for ln in lines if any(t in ln.lower() for t in terms)), lines[0] if lines else "")
    low = chosen.lower()
    first = min((i for i in (low.find(t) for t in terms) if i >= 0), default=0)
    start = max(0, first - _SNIPPET_BEFORE)
    if start:
        space = chosen.find(" ", start)
        start = space + 1 if 0 <= space < first else start
    piece = chosen[start : start + _SNIPPET_LENGTH]
    prefix = "…" if start else ""
    suffix = "…" if start + _SNIPPET_LENGTH < len(chosen) else ""
    pattern = re.compile("(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + ")", re.I)
    segments = []
    for i, part in enumerate(pattern.split(prefix and prefix + piece or piece)):
        if part:
            segments.append({"t": part, "hit": bool(i % 2)})
    if suffix:
        segments.append({"t": suffix, "hit": False})
    return segments


def search(query: str) -> list[dict]:
    """Sections containing every whitespace-separated term (case-insensitive),
    best first: a match in the section heading or page title outranks one in
    the body, then more occurrences."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    results: list[dict] = []
    for section in CATALOG:
        for entry in section.entries:
            path = REPO_ROOT / entry.path
            if not path.is_file():
                continue
            for sec in _sections(path.read_text(encoding="utf-8")):
                body = "\n".join(sec.lines).lower()
                heading = sec.heading.lower()
                haystack = f"{heading}\n{body}"
                if not all(t in haystack for t in terms):
                    continue
                # Repeats count up to 5 per term, so a long section can't beat
                # a page that is actually about the term.
                score = sum(min(haystack.count(t), 5) for t in terms)
                score += 20 * sum(t in heading for t in terms)
                score += 25 * sum(t in entry.title.lower() for t in terms)
                results.append({
                    "slug": entry.slug,
                    "page_title": entry.title,
                    "path": entry.path,
                    "section_title": sec.heading,
                    "anchor": sec.anchor,
                    "snippet": _snippet(sec.lines, terms),
                    "score": score,
                })
    results.sort(key=lambda r: -r["score"])
    per_page: dict[str, int] = {}
    capped = []
    for r in results:
        if per_page.get(r["slug"], 0) >= MAX_PER_PAGE:
            continue
        per_page[r["slug"]] = per_page.get(r["slug"], 0) + 1
        capped.append(r)
    return capped[:MAX_RESULTS]
