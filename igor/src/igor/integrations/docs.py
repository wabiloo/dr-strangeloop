"""Catalog of the repository's Markdown documentation, served by Igor's
Docs section. Pages are read from the repo at request time (no copies), and
only catalogued paths are ever served."""

from __future__ import annotations

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
    DocSection("franken-ts", "Content authoring: franken-ts", (
        DocEntry("franken-ts-readme", "franken-ts", "Build a .ts with SCTE-35 from a YAML playlist.", "franken-ts/README.md"),
        DocEntry("franken-ts-agents", "franken-ts reference", "YAML schema, marker types and CLI.", "franken-ts/AGENTS.md"),
        DocEntry("scte35-marker-rules", "SCTE-35 marker rules", "How Igor and franken-ts model and validate markers.", "SCTE35_MARKER_RULES.md"),
        DocEntry("scte35-numbering", "SCTE-35 segment numbering", "Cross-standard reference for segment numbering.", "scte35-segment-numbering-spec-comparison.md"),
    )),
    DocSection("its-a-live", "Deploy and run: its-a-live", (
        DocEntry("its-a-live-readme", "its-a-live", "Channel lifecycle across the three backends.", "its-a-live/README.md"),
        DocEntry("its-a-live-agents", "its-a-live reference", "Channel TOML schema and every channel.py command.", "its-a-live/AGENTS.md"),
        DocEntry("its-a-live-brief", "Architecture brief", "Original design brief for the AWS stacks.", "its-a-live/AGENT_BRIEF.md"),
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
