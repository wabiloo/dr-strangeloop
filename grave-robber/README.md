# grave-robber

Derives `loop-dee-loop` timing + SCTE-35 markers from a **captured HTTP
archive** (HAR from Chrome DevTools/Fiddler, or a Proxyman log) of a real
HLS/DASH session — so an already-observed stream can be re-served as a
loop, without needing the source's own encode. See [`SCOPE.md`](./SCOPE.md)
for the full design; this file is usage-focused.

Phase 1 (content) + phase 2 (build) of the repo-root pipeline don't apply
to this tool at all — it's an alternative *source* for phase 3
(`loop-dee-loop`), replacing `franken-ts`'s `.ts` + `.markers.json` output
with a **segment-list manifest** derived from a capture instead. See the
repo-root [`AGENTS.md`](../AGENTS.md).

## Pipeline

```
HAR/Proxyman archive
        |  trace-shrink: open_trace -> Trace -> ManifestStream (per rendition)
        v
  per-format extractor        HLS: m3u8   /   DASH: mpd-inspector
        |
        v
  TimingSegment[] + RawMarker[] + AssetBoundary[]   (per manifest snapshot)
        |
        v
  merge snapshots into one timeline (boundaries.py, SCOPE.md §7)
        |
        v
  SCTE-35 binary decode (threefive)   -- unifies b64 AND XML-native cases
        |
        v
  media body extraction, best-effort, per segment's source_uri
        |
        v
  segment-list manifest (index, duration_ticks, asset_boundary,
  gap_ticks, media_path|null) + markers.json-shaped list
        |    (loop-dee-loop's bake.py sparse input mode -- SCOPE.md §11)
        v
  loop-dee-loop's scte35_signaling.py / loop_math.py -- unchanged
```

## Command

```bash
# The common case: one already-known manifest URL (single rendition, or a
# human-picked reference variant after reviewing `coverage`'s report).
uv run --project grave-robber grave-robber ingest <archive.har> <manifest-url> \
    --output outputs/my-archive-channel/

# Multi-variant HLS: see each variant's captured wall-clock coverage
# (SCOPE.md §8) -- a text/table fallback; the real range-picker wizard
# lives in igor (SCOPE.md §10).
uv run --project grave-robber grave-robber coverage <archive.har>
```

`ingest` writes `<output>/manifest.json` (the segment-list manifest +
markers) and `<output>/media/` (whatever segment bodies the archive
happened to contain — see "Media completeness" below), ready for:

```bash
python3 ../loop-dee-loop/bake.py outputs/my-archive-channel/manifest.json \
    --output /var/loop-packages/my-archive-channel \
    --allow-missing-segments   # near-certain to be needed, see below
```

## Media completeness

This tool extracts real segment media on a **best-effort** basis: wherever
the archive's captured entries include a matching response body for a
segment the manifest references, that body is carried through; where it
doesn't (HAR captures are frequently manifest-only), the segment is simply
absent (`media_file: null`) from the manifest. This is **not** a blocking
requirement — the point of this tool is a structurally complete,
correctly-timed manifest (right segment count, durations, discontinuities,
SCTE-35), which downstream systems that only manipulate manifests (an SSAI
engine, for instance) can consume regardless of whether every segment is
actually playable end-to-end. See `loop-dee-loop/SCOPE.md` §11 for the
corresponding "manifest-complete, media-optional" serving mode.

## VOD manifest URL (rendition ladder)

No capture needed for a **VOD** (`#EXT-X-ENDLIST` HLS, `type="static"`
DASH): `ingest-url` fetches the manifest and every segment itself.

```bash
uv run --project grave-robber grave-robber ingest-url https://cdn.example/vod/master.m3u8 \
    --output outputs/<name>/ [--renditions all|best|720,360|#1,#3] [--no-audio] [--allow-missing-segments]
python3 loop-dee-loop/bake.py outputs/<name>/manifest.json --output <package-dir>
```

- A multivariant playlist / MPD becomes a **rendition ladder**
  (`--renditions`, default all): one shared timeline + markers (read from
  the highest-bandwidth rendition), and a `media_files` list per rendition
  — see `loop-dee-loop/SCOPE.md` §11.2. A media playlist gives a single
  rendition. Renditions must be segment-aligned (same count,
  discontinuities, durations within 10 ms) or ingest fails.
- Separate audio is kept as one track: HLS `#EXT-X-MEDIA` playlist (from
  the highest-bandwidth variant's audio group) or DASH audio AdaptationSet
  (the first one, first Representation). DASH audio is aligned to the video
  by segment start ticks, so audio and video segments needn't have equal
  durations; a video segment with no audio starting within half its length
  gets no audio. Byte-range HLS (single-file VOD) is supported.
- All segments are downloaded (`--workers`, default 8). A failed download
  fails the ingest unless `--allow-missing-segments`. Live playlists are
  refused — capture them and use `ingest`.
- Not supported: lazy/proxy serving of the origin's segments, live
  sources, and an ABR ladder from a HAR (`ingest` keeps one variant).

## Known limitations (v1)

- **Loop boundary**: uses the archive's full captured span, no
  auto-detection/fingerprinting (SCOPE.md §7's explicit decision) — the
  wrap point is always a real (possibly rough) seam.
- **Multi-variant selection** (SCOPE.md §8): `coverage`'s CLI report is
  the fallback; the real human-in-the-loop range-picker + full-coverage
  filter is designed as an `igor` UI wizard, not a CLI flag.
- **Archive imports (`ingest`) keep one variant** — the reference variant
  you pick, with at most one separate audio track. A rendition ladder
  (and DASH audio) comes only from `ingest-url` on a VOD manifest.
- **RFC 6381 codec strings / bandwidth** aren't derived by this tool at
  all — loop-dee-loop's sparse `bake.py` mode probes them via `ffprobe`
  from whatever media it recovers (best-effort, see its own README).
- **In-band-only SCTE-35 signaling with no manifest mirror** (DASH `emsg`
  with no `<EventStream>` echo; TS-embedded SCTE-35 with no `DATERANGE`
  echo in HLS) isn't recoverable without segment bytes — check what a real
  target capture actually carries before relying on this tool for it.
- **Real HAR end-to-end validation** (a full archive with real markers,
  multiple variants, real playable media) hasn't been spiked in this
  environment — the fixtures under `tests/fixtures/trace-shrink/` are
  small (2-4 entries, no markers) smoke-test data, not a substitute.

## Testing

```bash
uv run --project grave-robber python -m pytest grave-robber/tests/ -q
```

Most tests exercise the trickier timing/decode/filtering logic against
synthetic manifest text (HLS discontinuities/PROGRAM-DATE-TIME gotchas,
DASH Period gap/overlap math, multi-variant coverage filtering) rather
than relying on real captures for everything. `tests/fixtures/trace-shrink/`
holds small real HAR/Proxyman fixtures (copied from `wabiloo/trace-shrink`'s
own test suite) for pipeline-shape smoke tests (`test_fixtures_smoke.py`,
`test_cli.py`).
