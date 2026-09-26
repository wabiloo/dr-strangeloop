# Agent reference — grave-robber

An alternative *source* for phase 3 (`loop-dee-loop`), sibling to the
normal `franken-ts` → `loop-dee-loop` path (see repo-root
[`AGENTS.md`](../AGENTS.md)): derives loop timing + SCTE-35 markers from a
**captured HTTP archive** (HAR from Chrome DevTools/Fiddler, or a Proxyman
log) of a real HLS/DASH session, instead of from an authored
`franken-ts` playlist. See [`SCOPE.md`](./SCOPE.md) for full design
rationale (read it before changing anything here — the asset-boundary/
gap-overlap modeling and multi-variant filtering logic there are
load-bearing, not stylistic) and [`README.md`](./README.md) for usage.

## When to use this instead of franken-ts

Use `grave-robber` when the desired loop content is "re-serve an
already-observed real stream" rather than "author new content + ad
breaks" — you have (or can get) a HAR/Proxyman capture of the target
session, not a set of source video assets.

## Command

```bash
uv run --project grave-robber grave-robber ingest <archive.har> <manifest-url> \
    --output outputs/<name>/
uv run --project grave-robber grave-robber coverage <archive.har>   # multi-variant HLS report
```

- `ingest` is the common case: one already-known manifest URL (a single
  rendition, or a human-picked reference variant after reviewing
  `coverage`'s report — the real multi-variant range-picker wizard lives
  in `igor`, SCOPE.md §10, not this CLI).
- Output: `<output>/manifest.json` (segment-list manifest + markers) +
  `<output>/media/` (best-effort recovered segment bodies) — feed
  `manifest.json` straight into `loop-dee-loop/bake.py` (its sparse
  segment-list input mode, `loop-dee-loop/SCOPE.md` §11), almost always
  with `--allow-missing-segments`.

## What this tool does NOT decide

Media completeness is never this tool's problem to solve — see SCOPE.md
§1's "revised stance" and README.md's "Media completeness" section. A
structurally complete, correctly-timed manifest is the whole point, not a
guarantee that every segment is actually playable.

## Module layout

| File | Responsibility |
|---|---|
| `models.py` | `TimingSegment`/`AssetSpan`/`AssetBoundary`/`RawMarker` (SCOPE.md §5). |
| `extract_hls.py` | HLS manifest parsing via `m3u8` (§5.1). |
| `extract_dash.py` | DASH manifest parsing via `mpd-inspector` (§5.1). |
| `scte35_decode.py` | Unified SCTE-35 binary decode via `threefive`, both b64/hex and DASH XML-native (§5.2). |
| `media_extract.py` | Best-effort segment body extraction from the archive (§5.3). |
| `boundaries.py` | Multi-snapshot timeline merge, AssetSpan computation, loop-boundary selection (§6/§7). |
| `coverage.py` | Multi-variant HLS/DASH coverage map + full-coverage filtering (§8). |
| `manifest_writer.py` | Segment-list manifest + markers.json-shaped output (§11 decision). |
| `pipeline.py` | Orchestrates the above for one manifest URL. |
| `cli.py` | `grave-robber ingest`/`coverage` entrypoints. |

## Testing

```bash
uv run --project grave-robber python -m pytest grave-robber/tests/ -q
```

Most tests use synthetic manifest text (the trickier timing/gap/filtering
math); `tests/fixtures/trace-shrink/` holds small real HAR/Proxyman
captures for pipeline-shape smoke tests. See README.md's "Known
limitations" for what real-HAR validation hasn't been spiked yet.
