# SCOPE — grave-robber

> **Naming**: this tool was originally scoped under the placeholder
> directory name `archive-loop-import` — this repo's tools all get a
> deliberate (usually punny) name (`franken-ts`, `loop-dee-loop`,
> `its-a-live`, `inspector-krogh`, `galvanise.py`), and it has since been
> renamed to `grave-robber` (it exhumes a loop's timing/markers from a
> captured archive). Igor-facing UI terminology ("Archives", "archive
> import") is unaffected — that's user-facing wording, not the package
> name.

## 1. Motivation

`loop-dee-loop` bakes and serves a loop from `franken-ts` output: a `.ts`
(+ `.markers.json`) that was *authored* — explicit asset list, explicit ad
break markers, explicit intended loop point. This tool exists to produce
the same two artifacts `bake.py` needs (validated markers + segment
timing), but sourced from a **captured HTTP archive** (HAR from Chrome
DevTools/Fiddler, or a Proxyman log) of a real HLS/DASH session instead —
so an already-observed stream (with its own asset joins and SCTE-35
signaling) can be re-served as a loop, optionally transmuxed to the other
manifest format (HLS↔DASH), without needing the source's own encode.

**Foundational constraint**: timing and marker structure are always
derived from **manifest text alone**, decoupled from whether real segment
bytes are present — the archive may not contain every segment body (HAR
captures are frequently manifest-only, or segment bodies may simply be
missing/truncated), and manifest-derived timing/markers must not depend on
having them.

**Segment media, revised stance**: this tool *does* extract real segment
media from the archive on a **best-effort** basis (§5.3) — wherever the
archive's captured entries include a matching response body for a segment
the manifest references, that body is carried through to the output;
where it doesn't, the segment is simply absent from the baked media. This
is not a blocking requirement: the point of the tool is a **structurally
complete, correctly-timed manifest** (right segment count, durations,
discontinuities, SCTE-35), which downstream systems that only manipulate
manifests — an SSAI engine, for instance — can consume regardless of
whether every segment is actually playable end-to-end. See
`loop-dee-loop/SCOPE.md` §11 for the corresponding "manifest-complete,
media-optional" serving mode this relies on: manifests always reference
every segment as if it exists; a request for one with no backing media
404s, but that never affects manifest content.

## 2. Relationship to existing tools

- **`loop-dee-loop`**: this tool's output is a **segment-list manifest**
  (index, `duration_ticks`, `asset_boundary`, an on-disk media file path
  *or* `null` if no body was recovered for that segment — §5.3) plus the
  validated `markers.json`-shaped marker entries — consumed by
  `bake.py`'s new sparse segment-list input mode (`loop-dee-loop/SCOPE.md`
  §11), not by pretending to be one continuous `.ts`. `scte35_signaling.py`
  and `loop_math.py` stay **unmodified**; `serve.py` needs the
  declared-vs-serving position split (§6.2) and, per `loop-dee-loop/SCOPE.md`
  §11, its segment-byte handler needs a "no file for this index → 404"
  guard — manifest generation itself is unaffected in both cases.
- **`trace-shrink`** (`wabiloo/trace-shrink`): supplies archive parsing
  (`open_trace`, `Trace`, `ManifestStream`) — ordered manifest snapshots
  over time, per rendition. It does **not** parse manifest content
  (`EXTINF`/`DATERANGE`/`SegmentTimeline`/`EventStream`) — that's this
  tool's own job, using the libraries in §3.
- **`abr-encore`** (`wabiloo/abr-encore`): a related but distinct tool —
  replays captured HTTP bytes verbatim (no media processing, finite
  cursor-driven sequence, stateful). This tool instead extracts a
  canonical *timing/marker schedule* from the manifests and feeds it into
  `loop-dee-loop`'s infinite, stateless, drift-free serving model. No
  shared code; both consume `trace-shrink` archives.

## 3. Toolkit

| Concern | Library |
|---|---|
| Archive parsing | `trace-shrink` (`ManifestStream` per rendition) |
| HLS manifest parsing | `m3u8` (globo/globocom) — `Segment.cue_out`/`.scte35`/`.discontinuity`, `Playlist.dateranges` |
| DASH manifest parsing | `mpd-inspector` (`wabiloo/mpd-inspector`) — `SegmentTimeline`/`SegmentTemplate`, `EventStream`, plus its own `scte35` submodule for the XML-native SCTE-35 binding |
| SCTE-35 binary decode | `threefive>=2.4` — already a hard dependency of `loop-dee-loop` (`bake.py`, `scte35_signaling.py`) |

No new runtime dependency for `loop-dee-loop` itself: this tool is a
separate, offline, repo-root-workspace package (like `franken-ts`/
`inspector-krogh`), never deployed into `loop-dee-loop`'s minimal Docker
image.

## 4. Pipeline

```
HAR/Proxyman archive
        |  trace-shrink: open_trace -> Trace -> ManifestStream (per rendition)
        v
  per-format extractor        HLS: m3u8   /   DASH: mpd-inspector
        |
        v
  normalized TimingSegment[] + RawMarker[] + AssetBoundary[]   (per rendition, this tool's own model, S5)
        |
        v
  SCTE-35 binary decode (threefive)   -- unifies b64 AND XML-native cases
        |
        v
  multi-variant coverage + human-selected range + full-coverage filter   (S8, HLS only)
        |
        v
  media body extraction, best-effort, per TimingSegment.source_uri   (S5.3)
        |    trace.get_entries_for_url(uri) -> TraceEntry.content_bytes, or None if absent
        v
  segment-list manifest (index, duration_ticks, asset_boundary, media_path|None)
  + markers.json-shaped list[dict] + total_loop_duration_ticks
        |    (NEW bake.py sparse input mode -- loop-dee-loop/SCOPE.md S11 --
        |     not "one real .ts", since archive segments are already
        |     discrete files with holes, never a continuous encode)
        v
  scte35_signaling.markers_to_signaling() / loop_math.py   <- unchanged, existing loop-dee-loop code
```

## 5. Data model

```python
@dataclass(frozen=True)
class TimingSegment:
    index: int
    duration_ticks: int          # at TIMESCALE=90_000, matching loop-dee-loop
    asset_boundary: bool = False # starts a new asset: HLS discontinuity / DASH new Period
    source_uri: str | None = None  # the manifest-referenced segment URL, for S5.3's
                                    # archive body lookup; None only if a format's
                                    # extractor can't recover an absolute URI

@dataclass(frozen=True)
class AssetSpan:
    """Contiguous run of segments between asset boundaries -- the
    manifest-observable analogue of franken-ts's `assets` list entries
    (see franken-ts/AGENTS.md S57-ish: markers reference "the contiguous
    run of asset ids" they cover). No filename/id available from an
    archive, just the span itself."""
    first_segment_index: int
    segment_count: int
    start_ticks: int
    duration_ticks: int

@dataclass(frozen=True)
class AssetBoundary:
    segment_index: int    # first segment of the NEW asset
    gap_ticks: int         # signed: +gap (dead time) / -overlap. See S6.

@dataclass(frozen=True)
class RawMarker:
    source: str            # "daterange" | "cue-out" | "eventstream-bin" | "eventstream-xml"
    pts_time_ticks: int     # position on THIS rendition's own timeline
    splice_command_b64: str | None
    splice_command_xml: ET.Element | None   # only for eventstream-xml (mpd-inspector native)
    declared_duration_ticks: int | None      # manifest-declared, cross-check only
```

### 5.1 Per-format extraction

- **HLS** (`m3u8`): accumulate `#EXTINF` durations into `TimingSegment`s;
  `#EXT-X-DISCONTINUITY` -> `asset_boundary=True`. Markers from
  `playlist.dateranges` (base64 `SCTE35-OUT`/`-IN`/`-CMD`) or per-segment
  `.cue_out`/`.scte35` (Comcast-style). `DATERANGE`'s `START-DATE` is
  absolute wall-clock — position it against the timeline by matching it
  to the segment whose `#EXT-X-PROGRAM-DATE-TIME` window contains it, then
  use accumulated segment durations up to that point as `pts_time_ticks`.
  **Gotcha**: `#EXT-X-DISCONTINUITY` doesn't reliably reset
  `PROGRAM-DATE-TIME` consistently across packagers (some splice multiple
  sources' original wall-clock stamps together) — treat each
  discontinuity run as its own independent time-base for this matching,
  never assume monotonic wall-clock across the whole playlist.
- **DASH** (`mpd-inspector`): `SegmentTimeline`/`SegmentTemplate` ->
  `TimingSegment`s directly in ticks, no wall-clock needed. New `<Period>`
  -> `asset_boundary=True`. Markers from `EventStream`:
  `presentationTime`+`duration` are already Period-relative ticks.

### 5.2 SCTE-35 decode, unified

Funnel everything through `threefive`, even the DASH XML-native case
(`mpd-inspector`'s own `scte35` submodule parses the XML binding but not
the binary splice_info_section):

```python
def decode_marker(raw: RawMarker) -> dict:
    b64 = raw.splice_command_b64 or reencode_xml_scte35_to_b64(raw.splice_command_xml)
    cue = threefive.Cue(b64); cue.decode()
    return {
        "event_id": ...,                      # from cue's segmentation/splice event id
        "pts_time_ticks": raw.pts_time_ticks,  # manifest-derived position, NOT cue.command.pts_time
        "segmentation_type_id": ...,
        "segmentation_duration_ticks": ...,
        "splice_command_b64": b64,             # bake.py's _splice_command_base64_from_marker
                                                # already trusts a pre-attached value as-is --
                                                # no .ts to validate against, so no
                                                # decode_embedded_scte35/validate_markers_against_ts
                                                # step is needed for archive-derived markers.
        "is_out": ..., "is_instant": ..., "marker_identity": ...,
    }
```

The splice command's own internal `pts_time` field is never trusted as
the marker's real position, mirroring `bake.py`'s existing
`_callback` (line ~144) treatment of `threefive`'s decode of a `.ts`: the
real position always comes from where the message was actually observed
(there: demuxed PES PTS; here: the manifest-derived position from §5.1).

### 5.3 Media body extraction (best-effort, per segment)

For each `TimingSegment.source_uri`, look it up in the archive directly —
`trace.get_entries_for_url(source_uri)` (or the format's own resolved
absolute URL if the manifest used a relative one) — and take the first
matching entry's `TraceEntry.content_bytes` (works uniformly whether the
underlying archive is HAR or a Proxyman log; `trace-shrink`'s `TraceEntry`
abstracts that). Write the bytes to disk at a path this tool controls; set
`TimingSegment.media_path` to that path.

No match (never requested in this archive, request failed, or the
capture didn't record bodies) → leave it `None`. This is expected and
**not an error** — see §1's revised stance. No byte-level validation
beyond "the entry exists and has a body" (no demux/probe of the segment
content) — matches this tool's whole posture of trusting manifest text
over media inspection.

Duplicate entries for the same URL (a segment re-requested across
manifest refreshes, or on the live edge) are resolved by picking the
response with the largest `raw_size` / most complete body — a live-edge
capture sometimes catches a segment mid-download on its first request and
completes it on a later one.

## 6. Asset boundaries must round-trip to the output

Discontinuities/Period boundaries aren't just an ingest-time detection
concern — they mark real decoder-reset points and must be reproduced when
generating the looped output, wherever the resulting content still has
genuine encoder-boundary joins.

### 6.1 What already exists in `serve.py`

`serve.py` already has this primitive, but it fires **exactly once per
loop iteration**, at the wraparound point only — a fresh DASH
`<Period id="loop{loop_number}">` / an HLS `#EXT-X-DISCONTINUITY`, because
within one loop of `franken-ts` output there are no internal asset joins
(it's a single continuous re-encode). An archive-derived timeline needs
this to fire at every internal asset boundary too, not just the wrap —
same primitive, new call sites; real work in `serve.py`, not free reuse.

### 6.2 Gaps and overlaps at a boundary

At a real asset join, the reconstructed timeline may show a **gap**
(dead time between the last segment of one asset and the first of the
next) or an **overlap** (DASH-IF explicitly permits Period overlap at
boundaries; HLS can represent it too, see below). These must be
*recorded* always, and *reproduced* only optionally (flag, off by
default; warn when on) given the fidelity risk.

**PDT's actual role**: original absolute wall-clock timestamps are
*never* carried into the output — re-looping the content creates a fresh
live timeline from a new `--epoch-utc` regardless. PDT (HLS) / Period
`start=` (DASH) are used only as a **local, transient differential
measurement across one seam** (last segment's end wall-clock in asset N
vs. first segment's start wall-clock in asset N+1 for HLS; `Period start=`
vs. the previous Period's own cumulative `SegmentTimeline` duration for
DASH — no wall-clock needed there at all), producing a single
`gap_ticks` value, then discarded.

**Reproduction mechanism — declared position vs. serving position.**
`serve.py`'s `program_date_time_ticks(loop_number, segment_start_ticks,
total_loop_duration_ticks, epoch_ticks)` (serve.py:668) is a *pure
function of position* — always `epoch + loop_number * total_loop_duration
+ segment_start_ticks`, never accumulated from the previous segment's own
PDT. DASH's `Period start=` is the same shape. This means "position" can
be split into two axes:

1. **Serving position** — real playback position, the actual cumulative
   sum of real segment durations. Drives `total_loop_duration_ticks` and
   the wraparound modulus (`elapsed_ticks % total_loop_duration_ticks`),
   and which segment is served for a given request. **Never perturbed** —
   this is the zero-drift ground truth; segments still play back-to-back
   with no real dead air.
2. **Declared position** — what's rendered into `PROGRAM-DATE-TIME` /
   `DATERANGE START-DATE` / `<Period start=>`. Accumulates every
   `gap_ticks` crossed since loop-position 0, and gets fed into the
   existing PDT/Period-start formulas *instead of* the raw serving
   position.

This reproduces gap **and** overlap symmetrically, on **both** formats,
via the *same* mechanism, and needs **no** change to `loop_math.py` or
`total_loop_duration_ticks` — no phantom duration, no `#EXT-X-GAP`
placeholder segments. HLS overlap specifically: give the segment after
the boundary a PDT computed from `serving_position + accumulated_offset`
where the offset is negative — i.e. a PDT *earlier* than
`previous_PDT + previous_duration` — which is legal per RFC 8216 (PDT is
not required to be implied by cumulative `EXTINF` duration across a
discontinuity).

### 6.3 The loop-wrap boundary is always real

The wrap point is itself just another `AssetBoundary` — but unlike an
internal one, it doesn't exist in the original capture; it's synthetic.
It must **always** get a genuine discontinuity/Period restart in the
output (a real decoder reset), even when content on either side happens
to look continuous. The declared-position mechanism in §6.2 can smooth
the *labeled* timeline there, but never removes the real discontinuity
tag.

## 7. Loop boundary selection

**Decision**: use the archive's full captured span, without
auto-detection or fingerprinting. The first manifest snapshot's earliest
segment defines the start of the first reconstructed `AssetSpan`; the
last manifest snapshot's latest segment defines the end of the last.
Accept a real (and possibly visually rough) discontinuity at the wrap —
fidelity there is explicitly not a goal.

## 8. Multi-variant HLS

An HLS ABR ladder captured from a real OTT player reflects that player's
*adaptation* decisions, not a uniform recording window — different
variants may have been requested for different, non-contiguous stretches
of the session. There is no well-defined automatic "reference rendition"
pick (e.g. "shortest variant") the way there is for `franken-ts`'s
frame-identical-across-bitrates ladder; `bake()`'s existing hard-fail on
`total_loop_duration_ticks`/marker mismatch across renditions
(bake.py:1093-1110) assumes exactly that guarantee, which archive-derived
variants don't have.

**Flow**:

1. **Per-variant coverage map**: using the local PDT cross-referencing
   from §6.2, express each variant's captured segments as covered
   wall-clock intervals: `VariantCoverage(name, covered_ranges: list[(start, end)])`.
   Gaps between manifest polls where a variant wasn't the active one show
   up directly as holes.
2. **Merge and present**: show per-variant coverage over the shared
   timeline so a human can see which stretches are backed by real
   segments vs. gaps, per variant. A natural fit for `igor` (the web UI
   over the whole toolchain, per root `AGENTS.md`) as a coverage-timeline
   picker; a text/table CLI report is an adequate first pass.
3. **Human picks a target range** `[start, end]`.
4. **Filter the ladder**: keep only variants whose coverage is a
   *superset* of `[start, end]` (zero gaps across the entire range).
   Drop everything else from the output ladder entirely — not
   trimmed/reconciled, excluded.
5. **Residual reference pick**: among survivors, segment boundaries
   within the shared window still won't necessarily phase-align across
   bitrates (independent per-variant encoder segmentation) — one
   survivor still needs to be the canonical tick-reference for
   asset-boundary/marker positions, playing `bake()`'s existing
   `reference`-rendition role (bake.py:1077). Once all survivors share
   the exact same wall-clock window, this pick is low-stakes — arbitrary
   (e.g. first survivor) is fine.

DASH multi-period sources get the equivalent treatment for completeness,
though the "player adaptation switching mid-capture" motivation is
HLS-specific (a DASH client typically fetches one MPD covering the whole
session).

## 9. Explicit non-goals / limitations

- **Not the same fidelity tier as `franken-ts`-authored content.** That
  content is authored to loop cleanly by construction; an archive is an
  arbitrary captured window with no such guarantee. The wrap point is
  always a real seam (§7).
- **Real segment media is best-effort, not guaranteed.** §5.3 extracts
  whatever bodies the archive happens to contain; a real player pointed
  at the resulting channel may see 404s on segments the capture never
  recorded and simply fail to play through those spans. The manifest
  itself is never degraded on that account — no `#EXT-X-GAP`, no DASH
  timeline hole — because the primary consumer this tool targets is
  manifest-structural (an SSAI engine manipulating cues/discontinuities),
  not necessarily a real playback client. See `loop-dee-loop/SCOPE.md`
  §11.
- **Overlap reproduction is opt-in per §6.2's fidelity risk note**, even
  though it's now mechanically clean on both formats — still a policy
  choice for whoever bakes a given archive, not a default.
- **Requires the marker signaling to actually be present in manifest
  text.** In-band-only signaling with no manifest mirror (DASH `emsg`
  with no `EventStream` echo; TS-embedded SCTE-35 with no `DATERANGE`
  echo in HLS) isn't recoverable without segment bytes — check what a
  real target capture actually carries before relying on this tool for
  it.

## 10. Igor UI integration

**Decision**: a new top-level section in `igor`, parallel to Playlists and
Channels — not folded into `PlaylistEditor`, since the interaction (parse
an archive → per-variant coverage map → human-selected range → confirm)
is one-shot wizard-style triage, not ongoing document editing, and would
fight `PlaylistEditor`'s schema-driven form generation
(`franken_ts.config.Config.model_json_schema()`) which has nothing to do
with this tool's inputs.

**List view** (`ArchiveImportList.vue`, mirroring `PlaylistList.vue`):
available archives (HAR/Proxyman logs dropped into a store this tool
owns, e.g. `data/archives/`) plus already-completed imports, with basic
metadata (session duration, detected variant/rendition count, marker
count) — same shape as `igor/src/igor/integrations/franken_ts.py`'s
`list_playlists()`.

**Import wizard view** (`ArchiveImportEditor.vue`): parse the archive,
render the per-variant coverage map from §8 step 2 as a timeline picker
(reusing `AssetTimeline.vue`'s existing lane-rendering infrastructure
rather than building a new visualization from scratch), let the human
drag-select the target range (§8 step 3), show which variants survive
the full-coverage filter (§8 step 4) and which are dropped and why,
preview detected `AssetSpan`/`AssetBoundary`/marker overlays for the
selected range, then confirm — which spawns a job (same
`igor.jobs.runner` pattern `franken_ts.spawn_build_job` uses) that runs
this tool's full pipeline (§4) and writes the segment-list manifest +
extracted media (§5.3) + `markers.json`-shaped output to disk.

**Backend API surface** (new `igor/src/igor/app/routes/archives.py`,
mirroring `playlists.py`'s shape):

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/archives/` | list available archives + prior imports |
| `GET /api/v1/archives/{name}/coverage` | parse + return the per-variant coverage map for the wizard's range picker (analogous to `resolve_markers_preview`) |
| `POST /api/v1/archives/{name}/import` | spawn the import job for a human-confirmed range + variant selection; writes the segment-list manifest/markers/media, returns a `Job` (same shape `build_playlist` returns) |
| `GET /api/v1/archives/{name}/import/status` | output freshness (exists/stale vs. the archive or a re-run of the wizard), mirroring `output_status` |

**`ChannelNew.vue` change**: the existing "Content" section's `Playlist`
`Select` (today purely a `form.source_path` autofill + "Edit"/"Build now"
convenience links, per `ChannelNew.vue` lines 210-233 — `bake.py`/
`its-a-live` only ever see the resulting path, never "playlist" as a
concept) gets a source-kind toggle above it: **franken-ts playlist** /
**archive import**. Switching it swaps which `Select` + convenience links
are shown (Playlist's existing pair, or a new one backed by the
`/api/v1/archives/` list above with "Edit this import"/"Re-run import"
links); both still resolve to the same `form.source_path` field
underneath — no backend distinction between the two once a path is
picked.

A new checkbox, **"Allow missing segments (manifest-complete,
media-optional)"**, maps to the `allow_missing_segments` channel-config
flag (`loop-dee-loop/SCOPE.md` §11.2's `bake.py --allow-missing-segments`
equivalent). Defaults **on** when source-kind is archive import (the
near-certain case per §5.3/§9), but stays visible and editable regardless
of source kind — it's a `bake.py`-level flag, not intrinsically tied to
where the content came from.

## 11. Open items

- Final tool name + repo-root workspace placement (this doc's directory
  name is a placeholder, §0).
- Exact CLI shape (`bake.py`-style entrypoint vs. library-only).
- **Decided**: output feeds `bake.py`'s new sparse segment-list input mode
  (`loop-dee-loop/SCOPE.md` §11), not `--markers-override` — this tool
  never produces one continuous `.ts`, so the existing "one real .ts, cut
  by GPAC" contract doesn't fit regardless of media completeness.
- `serve.py` changes for §6.1 (multiple discontinuities/Periods per loop),
  §6.2 (declared-position accumulator), and `loop-dee-loop/SCOPE.md` §11's
  segment-byte 404 guard are real implementation work, not yet scoped in
  detail.
- Media-body matching heuristics (§5.3) beyond exact/relative URL lookup
  and largest-body dedup — e.g. matching across a CDN URL rewrite between
  manifest and segment requests — not yet spiked against a real archive.
- **Decided**: coverage-map visualization is an `igor` UI (§10), not a
  standalone CLI report — the timeline-picker interaction for §8 step 3
  needs a human dragging a range, not just reading a table.
- Testing strategy against real HAR captures (none yet spiked).
