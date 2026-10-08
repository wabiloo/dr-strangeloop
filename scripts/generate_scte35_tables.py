#!/usr/bin/env python3
"""Regenerate the generated copies of SCTE-35 Table 23 reference data for
consumers that cannot import the `scte35_table23` package directly:

- `loop-dee-loop/scte35_table23_data.py` -- loop-dee-loop is deployed as
  flat files copied into a standalone Docker image (see loop-dee-loop/
  Dockerfile and its-a-live/loop_stack.py, both of which build the image
  from the `loop-dee-loop/` directory alone), so it can't depend on a
  sibling workspace package at runtime.
- `igor/frontend/src/segmentationTable23.generated.ts` -- a separate
  (TypeScript) runtime that can't import a Python package at all.

Note the package is `scte35_table23`, not `scte35_tables`: the latter
name is already taken, on purpose, by inspector-krogh's own
`scte35_tables.py` -- a deliberate, separate duplicate of this same Table
23 data for verification independence (see that file's docstring). The
two must never be unified, and can't even both be imported under the
name `scte35_tables` in one Python process (module cache collision) --
hence the different name here.

`scte35_table23` (this script's only import) is the single source of
truth; run this after changing it:

    uv run python scripts/generate_scte35_tables.py

`--check` regenerates in memory and reports (without writing) whether
either target file is stale -- this is what
`loop-dee-loop/tests/test_scte35_tables_sync.py` runs (it checks both
generated files, not just loop-dee-loop's own copy), so a change to
`scte35_table23` that isn't followed by regenerating these files fails
that test instead of silently shipping a drifted copy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scte35_table23 import (
    INSTANT_SEGMENTATION_TYPE_IDS,
    LANE_PALETTE,
    SEGMENTATION_END_TYPE_ID,
    SEGMENTATION_TYPE_CODE,
    SEGMENTATION_TYPE_NAME,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_DEE_LOOP_TARGET = REPO_ROOT / "loop-dee-loop" / "scte35_table23_data.py"
IGOR_FRONTEND_TARGET = REPO_ROOT / "igor" / "frontend" / "src" / "segmentationTable23.generated.ts"

GENERATED_NOTICE = "GENERATED FILE -- DO NOT EDIT BY HAND."
SOURCE_NOTICE = "Source of truth: scte35-table23/scte35_table23/__init__.py"
REGEN_NOTICE = "Regenerate with: uv run python scripts/generate_scte35_tables.py"


def _str_dict_literal(d: dict[str, str]) -> str:
    lines = [f'    "{k}": "{v}",' for k, v in d.items()]
    return "{\n" + "\n".join(lines) + "\n}"


def render_loop_dee_loop() -> str:
    """A local, dependency-free copy of the canonical string-keyed tables --
    `scte35_signaling.py` derives the int-keyed views it actually uses
    (`SEGMENTATION_TYPE_CODES`, `SEGMENTATION_TYPE_NAMES`,
    `SEGMENTATION_START_TYPE_FOR_END`) from these at import time, the same
    way franken-ts's config.py derives its own frozensets."""
    return f'''"""{GENERATED_NOTICE}
{SOURCE_NOTICE}
{REGEN_NOTICE}

SCTE-35 Table 23 segmentation_type_id reference data -- see
scte35-table23/scte35_table23/__init__.py for the full explanation of why
this is a generated copy rather than an import: loop-dee-loop is deployed
as flat files into a standalone Docker image (see loop-dee-loop/Dockerfile
and its-a-live/loop_stack.py), built from the `loop-dee-loop/` directory
alone, so it cannot depend on a sibling workspace package at runtime.
"""

from __future__ import annotations

SEGMENTATION_TYPE_NAME: dict[str, str] = {_str_dict_literal(SEGMENTATION_TYPE_NAME)}

SEGMENTATION_TYPE_CODE: dict[str, str] = {_str_dict_literal(SEGMENTATION_TYPE_CODE)}

SEGMENTATION_END_TYPE_ID: dict[int, int] = {{
{chr(10).join(f"    0x{k:02X}: 0x{v:02X}," for k, v in SEGMENTATION_END_TYPE_ID.items())}
}}

INSTANT_SEGMENTATION_TYPE_IDS: frozenset[str] = frozenset({{
{chr(10).join(f'    "{v}",' for v in sorted(INSTANT_SEGMENTATION_TYPE_IDS))}
}})
'''


def render_igor_frontend_ts() -> str:
    """One row per selectable (Start or standalone) Table 23 value -- the
    data `segmentationPresets.ts` needs to build `SEGMENTATION_PAIR_OPTIONS`
    (display `label`/lane grouping stay hand-written there; they're
    presentation logic, not spec data).

    Note `SEGMENTATION_TYPE_NAME` has one entry (0x14 "Program Resumption")
    that is neither a Start type nor standalone -- it's the End half of an
    asymmetrically-named pair (0x13 Program Breakaway / 0x14 Program
    Resumption), kept in that table only so OSD label lookups can name it if
    it's ever encountered directly. It's excluded here: the editor selects a
    Start or standalone value, never a bare End type (see
    SEGMENTATION_END_TYPE_IDS handling in franken_ts.config.validate_markers)."""
    selectable = sorted(set(SEGMENTATION_END_TYPE_ID) | {int(t, 16) for t in INSTANT_SEGMENTATION_TYPE_IDS})
    rows = []
    for value in selectable:
        type_id = f"0x{value:02X}"
        name = SEGMENTATION_TYPE_NAME[type_id]
        code = SEGMENTATION_TYPE_CODE[type_id]
        instant = type_id in INSTANT_SEGMENTATION_TYPE_IDS
        end_value = "null" if instant else f"'0x{SEGMENTATION_END_TYPE_ID[value]:02X}'"
        rows.append(
            f"  {{ value: '{type_id}', name: '{name}', code: '{code}', "
            f"instant: {'true' if instant else 'false'}, endValue: {end_value} }},"
        )
    rows_block = "\n".join(rows)
    palette_block = "\n".join(f"  '#{c}'," for c in LANE_PALETTE)
    return f"""/**
 * {GENERATED_NOTICE}
 * {SOURCE_NOTICE}
 * {REGEN_NOTICE}
 *
 * One row per Table 23 Start/standalone segmentation_type_id --
 * segmentationPresets.ts builds SEGMENTATION_PAIR_OPTIONS (and its display
 * `label` strings) from this. */

export interface Table23Entry {{
  value: string
  name: string
  code: string
  instant: boolean
  endValue: string | null
}}

export const TABLE23: Table23Entry[] = [
{rows_block}
]

/** Timeline lane colors -- port of scte35_table23.lane_key/lane_color. */
export const LANE_PALETTE = [
{palette_block}
]

/** Lane key for a marker: `time_signal:0xNN` per segmentation type_id (any
 * case/`0X` spelling), or `splice_insert` for everything without one. */
export function laneKey(spliceType: string | undefined, typeId: string | undefined): string {{
  if (spliceType === 'time_signal' && typeId) {{
    return `time_signal:${{typeId.toUpperCase().replace('X', 'x')}}`
  }}
  return 'splice_insert'
}}

/** Deterministic color per lane key: `h = h*31 + ord(c)` as an unsigned 32-bit
 * hash into LANE_PALETTE. */
export function colorForLaneKey(key: string): string {{
  let hash = 0
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0
  return LANE_PALETTE[hash % LANE_PALETTE.length]
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="report staleness without writing; exit 1 if either target file would change",
    )
    args = parser.parse_args()

    targets = {
        LOOP_DEE_LOOP_TARGET: render_loop_dee_loop(),
        IGOR_FRONTEND_TARGET: render_igor_frontend_ts(),
    }

    if args.check:
        stale = [path for path, content in targets.items() if not path.is_file() or path.read_text() != content]
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        if stale:
            print("Run `uv run python scripts/generate_scte35_tables.py` to regenerate.", file=sys.stderr)
        return 1 if stale else 0

    for path, content in targets.items():
        path.write_text(content)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
