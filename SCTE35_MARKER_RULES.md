# SCTE-35 Marker Rules in Igor and franken-ts

This document summarizes the SCTE-35 marker checks currently implemented by the Igor playlist editor and the `franken-ts` playlist model. It describes the implemented subset of SCTE 35 2023r1, especially §10.3.3; it is not a declaration of complete SCTE-35 conformance.

## Enforcement setting

Each playlist has a top-level `enforce_scte35_marker_semantics` boolean. It defaults to `true` when absent, appears in Igor as **Enforce SCTE-35 marker semantics**, and is stored in the playlist YAML.

- **On:** Igor previews semantic issues, prevents committing invalid marker edits and saving, and `franken-ts` rejects invalid playlists at validation/build time. `franken-ts` also computes the supported `segment_num` and `segments_expected` values.
- **Off:** Igor permits semantic violations and shows duplicate-ID warnings. `franken-ts` skips semantic checks and numbering autofill, allowing the authored numbering values to pass through.

Disabling semantic enforcement does not disable the basic structural requirements needed to resolve spans: referenced asset IDs must exist and each marker must refer to a contiguous run of assets in playlist order.

## Event IDs

- With enforcement on, each marker in the playlist must have a unique `event_id`. Igor blocks duplicate IDs and `franken-ts` rejects them.
- With enforcement off, duplicate IDs are accepted by the playlist model. Igor keeps a separate UI identity for each marker so duplicate IDs do not make rows/editing ambiguous. The `.markers.json` sidecar includes `marker_identity` for duplicate-ID markers, and HLS/DASH signaling uses it to keep emitted manifest IDs distinct. The SCTE-35 `event_id` carried on the wire remains the authored value. **Known limitation:** loop-dee-loop's decoded-SCTE-35-to-sidecar validation currently keys occurrences by event ID and PTS, and does not yet reconcile `marker_identity`; duplicate-ID playlists may therefore fail that bake-time consistency check.
- A paired marker still represents one event: the generated start and end boundaries share its `event_id`. Instant segmentation types produce one boundary.

## Structural marker/span checks (always on)

- A marker must reference existing asset IDs.
- The referenced assets must form one contiguous range in playlist order; a span with gaps is rejected.
- Marker times are derived from the resolved asset boundaries rather than authored independently.

## Semantic hierarchy and overlap checks (enforcement on)

Rules apply to recognized `time_signal` segmentation types in the selected SCTE-35 hierarchies. `splice_insert`, standalone/instant descriptors, and types outside these hierarchies do not take part in the type-aware hierarchy checks.

### Content hierarchy

Recognized content levels are **Network → Program → Chapter**. A lower-level overlapping marker must be contained by the relevant higher-level marker. Levels may be omitted; for example, a Chapter or a Program can exist without a Network marker.

- Chapter spans may overlap, as permitted by SCTE 35.
- Program Overlap Start supports the explicitly defined overlap with another Program.
- Other overlapping markers at the same content level are rejected.

### Advertising hierarchy

Recognized advertising levels are **Break → Placement Opportunity → Ad Block → Advertisement/Promo**.

- An overlapping lower-level marker must be contained by its higher-level marker. Thus a PPO that starts inside a Break but crosses beyond the Break’s end is rejected.
- Levels may be omitted. A PPO may be standalone, and when it is inside a Break it may cover only part of the Break.
- Placement Opportunities may nest; partially crossing PPO spans are rejected.
- Same-level Breaks and Ad Blocks may not overlap.
- Advertisements and Promos share the lowest logical level: overlapping Ad/Promo spans are rejected, and nested Ad in Ad, Promo in Promo, or Ad Block in Ad Block is rejected.

### Alternate Content Opportunities

- An Alternate Content Opportunity may be nested within a Content or Advertisement span, and Alternate Content Opportunities may nest within one another.
- A contained Alternate Content Opportunity must remain inside its containing opportunity. Partial crossings are rejected.

### Duplicate same-span signals

With enforcement on, two markers with the same signal identity—same splice type and segmentation type—over exactly the same asset span are treated as redundant and rejected. Different signals may share the same span where the hierarchy rules permit it.

## Numbering profiles

Igor exposes **Enforce SCTE-35 marker semantics** and **Numbering** beside the marker list in the Timeline tab. The playlist stores `scte35_numbering_scheme` (`SCTE35_2023R1` by default for existing playlists, `SCTE35_2019A`, or `AF2M_SNPTV`) and `break_numbering_supported` (default `false`). See [the cross-standard comparison](scte35-segment-numbering-spec-comparison.md) for the field definitions and source references.

With enforcement on, franken-ts computes all four 8-bit numbering fields from asset containment; authored numbers are replaced. Start/End pairs use the same outer numbers; sub-numbering is emitted on Starts only. With enforcement off, authored `segment_num`, `segments_expected`, `sub_segment_num`, and `sub_segments_expected` pass through unchanged. Absent sub-fields are omitted in the generated XML/sidecar.

- **SCTE 35 2023r1:** `break_numbering_supported` opts into one-based Breaks per Program. Without a Program, Breaks are grouped by their `break_interval` (default 1), allowing a new provider-defined interval to reset the counter. When disabled, Break and all contained advertising outer numbers are `0/0`. Placement Opportunities, Ads/Promos, and Ad Blocks inherit their Break's outer pair. POs and Ads/Promos have separate one-based, Break-local sub-counters. Ad Block `sub_segment_num` identifies its first contained Ad/Promo's position; `sub_segments_expected` counts Ad Blocks in that Break. An Ad Block without a contained Ad/Promo is rejected.
- **SCTE 35 2019a:** PO outer numbers follow Break numbering as above. PO Starts receive a Break-local sub-counter; Ads/Promos instead receive optional Chapter-style outer numbers, implemented here as a one-based ad/promo collection per Break. Ads/Promos do not carry sub-fields. Ad Block types are rejected because they did not exist in this revision.
- **af2m/SNPTV:** Breaks and Provider POs use fixed `1/1`. Provider Ads require a containing Break. Assets have an optional, scheme-independent `role: advert | jingle` (set in Igor's asset **Role** field). With no role, an asset is counted like an advert; `advert` is currently informational. Spots count one-based within the Break, excluding Jingle assets. A Jingle PAD at the **start** of its Break uses `0/<spot count>`; one at the **end** uses `0/0`. A Jingle PAD anywhere else is rejected. If a PAD spans multiple assets, they must all be Jingle or all non-Jingle. Neither asset names nor UPIDs implicitly classify jingles. The role survives scheme changes, but only affects af2m numbering. Legacy `jingle_role` on assets or markers is migrated to `role: jingle` on the assets when loaded. No sub-fields are emitted. This profile only supports Break `0x22`, Provider PO `0x34`, Provider Ad `0x30`, and Call Ad Server `0x02`. See §3.2.11 (printed p. 20) of *Service de TV Segmentée* v2.0.7 for the numbering table.

Program/Chapter and other fixed Table 23 values continue to be computed in the SCTE profiles. Numbering values must fit the 8-bit fields (0–255). The model derives paired endpoints from one marker, so their event IDs cannot diverge.

## SCTE-35 start/end type mapping

The editor asks for a paired Start type or a standalone signal; users do not enter a separate End type. `franken-ts` emits the corresponding End boundary with the same event ID. The mapping is explicit for non-consecutive program pairs, including:

- Program Start `0x10` → Program End `0x11`
- Program Breakaway `0x13` → Program Resumption `0x14`
- Program Overlap Start `0x17` → Program End `0x11`
- Program Join `0x19` → Program End `0x11`

Known End types cannot be selected as a marker Start while enforcement is on. Standalone types such as Program Early Termination `0x12` remain one-boundary signals.

## Not implemented by this rule set

The current toggle does not validate the full Program lifecycle (for example, requiring a Program End for every Program Start or checking Breakaway/Resumption chronology), UPID identifier matching between endpoints, delivery-restriction rules, or `sub_segment_*` numbering. Other SCTE-35 field, timing, and transport constraints are outside this marker-semantics toggle.
