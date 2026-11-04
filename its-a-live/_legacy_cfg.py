"""Channel-config versioning and the migration of old files.

A channel config carries a top-level integer `config_version` (absent = 1, i.e.
every file written before 2.0). The current version is `CONFIG_VERSION`; a file
with an older one is refused with the exact changes and migrated by
`migrate_config.py`; a newer one is refused as written by a newer release.
Migrations are a chain (`_MIGRATIONS[n]` upgrades version n to n+1), so a file
of any age is brought forward step by step.

Version 1 -> 2 (the 2.0 renames):

    [markers]   daterange_mode            -> [markers.hls]   descriptor_grouping
    [markers]   dash_descriptor_mode      -> [markers.dash]  descriptor_grouping
    [markers]   cue_tags                  -> [markers.hls]   cue_tags
    [markers]   daterange_id_format       -> [markers.hls]   id_template
    [markers]   dash_signal_format        -> [markers.dash]  signal_format
    [markers]   period_on_segmentation    -> [markers]       split_on
    [markers]   period_on_segmentation_apply   (dropped: applies to HLS and DASH alike)
    [packaging] dvr_window_seconds        -> [packaging]      dvr_window
    [packaging] hls_format                -> [packaging.hls]  container
    [packaging] hls_ts_mux_audio          -> [packaging.hls]  mux_audio
    [packaging] dash_timeline             -> [packaging.dash] timeline_style
    [packaging] dash_addressing           -> [packaging.dash] addressing
    [timeline]  epoch_utc                 -> [timeline]       epoch
    [timeshift] enabled                   -> [query] timeshift AND [query] overrides
    [timeshift] start_param/end_param     -> [query]          (same names)

(`[query] enabled`, which existed only in development builds of 2.0, maps like
`[timeshift] enabled`.) Stdlib only.
"""

from __future__ import annotations

import copy
import re
import tomllib

# (old table, old key) -> (new table, new key), a list of those (the value is
# copied to each), or None when the key is dropped. Version 1 -> 2.
RENAMES: dict[tuple[str, str], tuple[str, str] | list[tuple[str, str]] | None] = {
    ("markers", "daterange_mode"): ("markers.hls", "descriptor_grouping"),
    ("markers", "dash_descriptor_mode"): ("markers.dash", "descriptor_grouping"),
    ("markers", "cue_tags"): ("markers.hls", "cue_tags"),
    ("markers", "daterange_id_format"): ("markers.hls", "id_template"),
    ("markers", "dash_signal_format"): ("markers.dash", "signal_format"),
    ("markers", "period_on_segmentation"): ("markers", "split_on"),
    ("markers", "period_on_segmentation_apply"): None,
    ("packaging", "dvr_window_seconds"): ("packaging", "dvr_window"),
    ("packaging", "hls_format"): ("packaging.hls", "container"),
    ("packaging", "hls_ts_mux_audio"): ("packaging.hls", "mux_audio"),
    ("packaging", "dash_timeline"): ("packaging.dash", "timeline_style"),
    ("packaging", "dash_addressing"): ("packaging.dash", "addressing"),
    ("timeline", "epoch_utc"): ("timeline", "epoch"),
    # The old single switch gated every query parameter; the two new switches
    # (timeshift, overrides) both inherit its value, which keeps its behavior.
    ("timeshift", "enabled"): [("query", "timeshift"), ("query", "overrides")],
    ("query", "enabled"): [("query", "timeshift"), ("query", "overrides")],
    ("timeshift", "start_param"): ("query", "start_param"),
    ("timeshift", "end_param"): ("query", "end_param"),
}

CONFIG_VERSION = 2

MIGRATE_COMMAND = "uv run --project its-a-live python its-a-live/migrate_config.py"


def _targets(new) -> list[tuple[str, str]]:
    if new is None:
        return []
    return list(new) if isinstance(new, list) else [new]


def _describe(old: tuple[str, str], new) -> str:
    if new is None:
        return f"[{old[0]}] {old[1]} -> removed (it now applies to HLS and DASH alike)"
    return f"[{old[0]}] {old[1]} -> " + " and ".join(f"[{t}] {k}" for t, k in _targets(new))


def _table(cfg: dict, dotted: str) -> dict | None:
    node = cfg
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
    return node


def find_legacy_keys(cfg: dict) -> list[str]:
    """One line per renamed/dropped key still present in `cfg`."""
    return [
        _describe(old, new)
        for old, new in RENAMES.items()
        if (t := _table(cfg, old[0])) is not None and old[1] in t
    ]


def config_version(cfg: dict) -> int:
    """The config's `config_version` (1 when absent)."""
    value = cfg.get("config_version", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SystemExit(f"config_version must be a positive integer, got {value!r}")
    return value


def check_config_version(cfg: dict, config_path: str = "config") -> None:
    """Refuse a config of another version, and one that claims the current
    version but still has an old key. A config using an old key would silently
    fall back to the new key's default (the wrong cue tags, DVR window, epoch,
    ...), so fail loudly with the exact changes instead."""
    version = config_version(cfg)
    if version > CONFIG_VERSION:
        raise SystemExit(
            f"{config_path}: config_version {version} was written by a newer release "
            f"(this one reads version {CONFIG_VERSION}) -- upgrade, or edit the file"
        )
    legacy = find_legacy_keys(cfg)
    if version < CONFIG_VERSION or legacy:
        lines = "\n  ".join(legacy) or "(only the version marker is missing)"
        raise SystemExit(
            f"{config_path}: config_version {version}, current is {CONFIG_VERSION}; changed config keys:\n"
            f"  {lines}\n"
            f"Rewrite the file with: {MIGRATE_COMMAND} {config_path}"
        )


# ── dict migration ──────────────────────────────────────────────────────────


def _migrate_1_to_2(cfg: dict, warnings: list[str]) -> None:
    for old, new in RENAMES.items():
        table = _table(cfg, old[0])
        if table is None or old[1] not in table:
            continue
        value = table.pop(old[1])
        if new is None:
            if value != "both":
                warnings.append(
                    f"[{old[0]}] {old[1]} = {value!r} was dropped: the forced Period/discontinuity "
                    f"now applies to both HLS and DASH"
                )
            continue
        for target_table, target_key in _targets(new):
            node = cfg
            for part in target_table.split("."):
                node = node.setdefault(part, {})
            node[target_key] = value
    for name in ("timeshift", "markers", "packaging", "timeline"):
        if name in cfg and cfg[name] == {}:
            del cfg[name]


# Version n -> n + 1. Add the next migration here, bump CONFIG_VERSION, and
# extend the text migrator below if it moves keys between tables.
_MIGRATIONS = {1: _migrate_1_to_2}


def migrate_config(cfg: dict) -> tuple[dict, list[str]]:
    """`cfg` brought to `CONFIG_VERSION` (with `config_version` set), plus
    warnings for anything that cannot be carried over exactly. Idempotent."""
    version = config_version(cfg)
    if version > CONFIG_VERSION:
        raise ValueError(f"config_version {version} is newer than this release's {CONFIG_VERSION}")
    out = copy.deepcopy(cfg)
    warnings: list[str] = []
    while version < CONFIG_VERSION:
        _MIGRATIONS[version](out, warnings)
        version += 1
    out["config_version"] = CONFIG_VERSION
    return out, warnings


# ── comment-preserving text migration ───────────────────────────────────────

_HEADER = re.compile(r"^\s*\[(?!\[)([^\[\]]+)\]\s*(#.*)?$")
_KEY = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")
_COMMENT = re.compile(r"^\s*#")


def _value_is_open(text: str) -> bool:
    """True when `text` (the rest of a `key = ...` line) opens a bracketed
    value or multi-line string that continues on the next line."""
    depth, quote, i = 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote in ('"""', "'''"):
                if text.startswith(quote, i):
                    quote, i = None, i + 2
            elif ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif text.startswith('"""', i) or text.startswith("'''", i):
            quote, i = text[i : i + 3], i + 2
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "#":  # a comment runs to the end of its line only
            newline = text.find("\n", i)
            if newline == -1:
                break
            i = newline
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth > 0 or quote in ('"""', "'''")


class _Entry:
    """A key plus the comment block directly above it, or loose lines."""

    def __init__(self, lines: list[str], key: str | None = None):
        self.lines = lines
        self.key = key

    def renamed(self, new_key: str) -> "_Entry":
        lines = list(self.lines)
        for n, line in enumerate(lines):
            m = _KEY.match(line)
            if m and m.group(2) == self.key:
                lines[n] = f"{m.group(1)}{new_key}{m.group(3)}{m.group(4)}"
                break
        return _Entry(lines, new_key)


class _Section:
    def __init__(self, name: str | None, header: str | None):
        self.name = name  # None: the lines before the first header
        self.header = header
        self.entries: list[_Entry] = []


def _parse_sections(text: str) -> list[_Section]:
    sections = [_Section(None, None)]
    pending: list[str] = []  # comment lines directly above the next key
    lines = text.split("\n")
    n = 0

    def flush_pending():
        for line in pending:
            sections[-1].entries.append(_Entry([line]))
        pending.clear()

    while n < len(lines):
        line = lines[n]
        header = _HEADER.match(line)
        if header:
            flush_pending()
            sections.append(_Section(header.group(1).strip(), line))
        elif _COMMENT.match(line):
            pending.append(line)
        elif (m := _KEY.match(line)) and sections[-1].name is not None:
            block = [*pending, line]
            pending.clear()
            value_text = m.group(4)
            while _value_is_open(value_text) and n + 1 < len(lines):
                n += 1
                block.append(lines[n])
                value_text += "\n" + lines[n]
            sections[-1].entries.append(_Entry(block, m.group(2)))
        else:
            flush_pending()
            sections[-1].entries.append(_Entry([line]))
        n += 1
    flush_pending()
    return sections


def _render(sections: list[_Section]) -> str:
    out: list[str] = []
    for section in sections:
        if section.header is not None:
            if out and out[-1].strip():
                out.append("")
            out.append(section.header)
        for entry in section.entries:
            out.extend(entry.lines)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n"


def _insertion_index(sections: list[_Section], new_name: str) -> int:
    """Where a brand-new table goes: right after its parent table (and the
    other sub-tables already there); `[query]` goes just before `[timeshift]`."""
    names = [s.name for s in sections]
    if new_name == "query":
        for fallback in ("timeshift",):
            if fallback in names:
                return names.index(fallback)
        for after in ("timeline",):
            if after in names:
                return names.index(after) + 1
        return len(sections)
    parent = new_name.rsplit(".", 1)[0] if "." in new_name else None
    if parent is not None:
        related = [i for i, n in enumerate(names) if n == parent or (n and n.startswith(parent + "."))]
        if related:
            return related[-1] + 1
    return len(sections)


_VERSION_LINE = re.compile(r"(?m)^config_version\s*=.*$")


def _with_version_marker(text: str) -> str:
    """`text` with `config_version = <current>` as the first line (replacing an
    existing marker in place). A top-level key has to precede every table."""
    line = f"config_version = {CONFIG_VERSION}"
    if _VERSION_LINE.search(text):
        return _VERSION_LINE.sub(line, text, count=1)
    return f"{line}\n\n{text.lstrip(chr(10))}"


def migrate_toml_text(text: str) -> tuple[str, list[str]]:
    """`text` brought to the current `config_version`: legacy keys moved to
    their new tables, comments kept (a comment block directly above a key moves
    with it) and the version marker added. Returns the new text and warnings.
    Byte-identical when the file is already current. Verified: the result
    parses to exactly `migrate_config` of the input."""
    original = tomllib.loads(text)
    expected, warnings = migrate_config(original)
    if expected == original:
        return text, warnings

    sections = _parse_sections(text)
    moved: list[tuple[str, _Entry]] = []
    emptied: set[int] = set()
    for section in sections:
        keep = []
        for entry in section.entries:
            if entry.key is not None and (section.name, entry.key) in RENAMES:
                emptied.add(id(section))
                for target_table, target_key in _targets(RENAMES[(section.name, entry.key)]):
                    moved.append((target_table, entry.renamed(target_key)))
            else:
                keep.append(entry)
        section.entries = keep

    for table, entry in moved:
        existing = next((s for s in sections if s.name == table), None)
        if existing is None:
            existing = _Section(table, f"[{table}]")
            sections.insert(_insertion_index(sections, table), existing)
        last_key = max((i for i, e in enumerate(existing.entries) if e.key is not None), default=len(existing.entries) - 1)
        existing.entries.insert(last_key + 1, entry)  # keeps the table's trailing blank lines last

    # tables whose keys all moved away disappear (an empty header is noise)
    sections = [s for s in sections if id(s) not in emptied or any(e.key is not None for e in s.entries)]
    result = _with_version_marker(_render(sections))
    if tomllib.loads(result) != expected:
        raise ValueError("internal error: the migrated TOML does not match the expected configuration")
    return result, warnings
