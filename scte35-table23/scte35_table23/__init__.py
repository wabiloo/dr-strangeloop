"""Canonical SCTE-35 Table 23 `segmentation_type_id` reference data.

This is the single source of truth for the Start/standalone
`segmentation_type_id` values franken-ts authors and igor's editor exposes:
their bare Table 23 name, a stable three-letter compact code (OSD overlays,
HLS/DASH signaling IDs), and the Start -> End type_id pairing.

Named `scte35_table23`, not `scte35_tables`, to avoid colliding with
`inspector-krogh/scte35_tables.py` -- that file is a deliberate, separate
duplicate of this same Table 23 data (see its own docstring: krogh
verifies a built `.ts` independently of franken-ts's bookkeeping, so it
intentionally does NOT share this package). Nothing here should be merged
with that file.

Three places need this same data and, before this package existed, each
hand-maintained its own copy that could silently drift from the others:

- `franken_ts.config` (the authoring/validation source of truth) --
  imports these tables directly (same uv workspace, always in sync by
  construction).
- `loop-dee-loop/scte35_signaling.py` -- deployed as flat files into a
  standalone Docker image (see loop-dee-loop/Dockerfile), so it cannot
  depend on this package at runtime without restructuring that build.
  `loop-dee-loop/scte35_table23_data.py` is a generated, checked-in copy
  of this module's data instead; `scripts/generate_scte35_tables.py`
  regenerates it, and `loop-dee-loop/tests/test_scte35_tables_sync.py`
  fails CI if it ever falls out of date.
- `igor/frontend/src/segmentationPresets.ts` -- a separate (TypeScript)
  runtime, so it consumes a generated
  `igor/frontend/src/segmentationTable23.generated.ts`, produced by the
  same `scripts/generate_scte35_tables.py` and checked by the same
  `loop-dee-loop/tests/test_scte35_tables_sync.py` (it checks both
  generated files, not just its own tool's copy).

See root `SCTE35_MARKER_RULES.md` and
`scte35-segment-numbering-spec-comparison.md` for the broader SCTE-35
semantics this data feeds into (hierarchy/overlap rules and numbering
profiles) -- those remain implemented independently in franken-ts (Python,
authoritative) and igor's frontend (TypeScript, instant editor feedback);
only the plain reference *data* below is centralized here.
"""

from __future__ import annotations

# Table 23 segmentation_type_id (Start value, or the only value for a
# standalone/instant signal) -> bare name (no "Start"/"End" wording).
SEGMENTATION_TYPE_NAME: dict[str, str] = {
    "0x00": "Not Indicated",
    "0x01": "Content Identification",
    "0x02": "Call Ad Server",
    "0x10": "Program",
    "0x12": "Program Early Termination",
    "0x13": "Program Breakaway",
    "0x14": "Program Resumption",
    "0x15": "Program Runover Planned",
    "0x16": "Program Runover Unplanned",
    "0x17": "Program Overlap Start",
    "0x18": "Program Blackout Override",
    "0x19": "Program Join",
    "0x1A": "Program Immediate Resumption",
    "0x20": "Chapter",
    "0x22": "Break",
    "0x24": "Opening Credit",
    "0x26": "Closing Credit",
    "0x30": "Provider Advertisement",
    "0x32": "Distributor Advertisement",
    "0x34": "Provider Placement Opportunity",
    "0x36": "Distributor Placement Opportunity",
    "0x38": "Provider Overlay Placement Opportunity",
    "0x3A": "Distributor Overlay Placement Opportunity",
    "0x3C": "Provider Promo",
    "0x3E": "Distributor Promo",
    "0x40": "Unscheduled Event",
    "0x42": "Alternate Content Opportunity",
    "0x44": "Provider Ad Block",
    "0x46": "Distributor Ad Block",
    "0x50": "Network",
}

# Stable three-letter codes for compact SCTE-35 span labels (OSD overlays,
# reports, HLS/DASH signaling IDs, and other consumers). Kept explicit
# rather than derived from names: several names collide or produce codes
# that are too long to use as compact identifiers.
SEGMENTATION_TYPE_CODE: dict[str, str] = {
    "0x00": "NIN",  # Not Indicated
    "0x01": "CID",  # Content Identification
    "0x02": "CAS",  # Call Ad Server
    "0x10": "PRG",  # Program
    "0x12": "PET",  # Program Early Termination
    "0x13": "PBA",  # Program Breakaway
    "0x14": "PRS",  # Program Resumption
    "0x15": "PRP",  # Program Runover Planned
    "0x16": "PRU",  # Program Runover Unplanned
    "0x17": "POS",  # Program Overlap Start
    "0x18": "PBO",  # Program Blackout Override
    "0x19": "PJO",  # Program Join
    "0x1A": "PIR",  # Program Immediate Resumption
    "0x20": "CHP",  # Chapter
    "0x22": "BRK",  # Break
    "0x24": "OPN",  # Opening Credit
    "0x26": "CLC",  # Closing Credit
    "0x30": "PAD",  # Provider Advertisement
    "0x32": "DAD",  # Distributor Advertisement
    "0x34": "PPO",  # Provider Placement Opportunity
    "0x36": "DPO",  # Distributor Placement Opportunity
    "0x38": "PVO",  # Provider Overlay Placement Opportunity
    "0x3A": "DVO",  # Distributor Overlay Placement Opportunity
    "0x3C": "PPR",  # Provider Promo
    "0x3E": "DPR",  # Distributor Promo
    "0x40": "USC",  # Unscheduled Event
    "0x42": "ACO",  # Alternate Content Opportunity
    "0x44": "PAB",  # Provider Ad Block
    "0x46": "DAB",  # Distributor Ad Block
    "0x50": "NET",  # Network
}

# Table 23 pairings. Most pairs have consecutive IDs, but program
# segmentation includes non-consecutive pairs and multiple start types that
# share Program End. Values are keys (start type_id) -> end type_id.
SEGMENTATION_END_TYPE_ID: dict[int, int] = {
    0x10: 0x11,  # Program Start / Program End
    0x13: 0x14,  # Program Breakaway / Program Resumption
    0x17: 0x11,  # Program Overlap Start / Program End
    0x19: 0x11,  # Program Join / Program End
    0x20: 0x21,  # Chapter Start / Chapter End
    0x22: 0x23,  # Break Start / Break End
    0x24: 0x25,  # Opening Credit Start / Opening Credit End
    0x26: 0x27,  # Closing Credit Start / Closing Credit End
    0x30: 0x31,  # Provider Advertisement Start / End
    0x32: 0x33,  # Distributor Advertisement Start / End
    0x34: 0x35,  # Provider Placement Opportunity Start / End
    0x36: 0x37,  # Distributor Placement Opportunity Start / End
    0x38: 0x39,  # Provider Overlay Placement Opportunity Start / End
    0x3A: 0x3B,  # Distributor Overlay Placement Opportunity Start / End
    0x3C: 0x3D,  # Provider Promo Start / End
    0x3E: 0x3F,  # Distributor Promo Start / End
    0x40: 0x41,  # Unscheduled Event Start / End
    0x42: 0x43,  # Alternate Content Opportunity Start / End
    0x44: 0x45,  # Provider Ad Block Start / End
    0x46: 0x47,  # Distributor Ad Block Start / End
    0x50: 0x51,  # Network Start / End
}

# Table 23 segmentation_type_id values signaled as standalone/instant (no
# Start/End pairing).
INSTANT_SEGMENTATION_TYPE_IDS: frozenset[str] = frozenset({
    "0x00",  # Not Indicated
    "0x01",  # Content Identification
    "0x02",  # Call Ad Server
    "0x12",  # Program Early Termination
    "0x15",  # Program Runover Planned
    "0x16",  # Program Runover Unplanned
    "0x18",  # Program Blackout Override
    "0x1A",  # Program Immediate Resumption
})

__all__ = [
    "SEGMENTATION_TYPE_NAME",
    "SEGMENTATION_TYPE_CODE",
    "SEGMENTATION_END_TYPE_ID",
    "INSTANT_SEGMENTATION_TYPE_IDS",
]
