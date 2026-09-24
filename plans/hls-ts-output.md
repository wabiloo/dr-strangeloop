# Plan: optional HLS/TS (muxed MPEG-TS) output for loop-dee-loop

Adds a 3rd delivery output to loop-dee-loop channels: HLS backed by raw
muxed MPEG-TS segments, alongside the existing default (CMAF-based HLS +
DASH). Optional, off by default, set per channel.

**Decisions locked in:**
- Muxed A/V per TS segment (no separate audio track/playlist) — the
  standard/traditional MPEG-TS HLS shape.
- In-band SCTE-35 (GPAC `scte35dec:mode=passthrough`, same as today)
  *plus* the existing out-of-band `EXT-X-DATERANGE`, with a bake-time
  consistency check between the two.
- Full ABR ladder parity with the CMAF output (not just a single
  rendition).
- Setting lives in the its-a-live channel TOML (`[packaging]`),
  ecs-express/local-docker only — hard error on `aws-media` (that backend
  never runs bake.py/GPAC at all).

**One open question the implementing agent must resolve via prototyping
before locking the design:** whether GPAC's `dasher` filter can emit
muxed MPEG-TS segments with exact cue-forced boundaries in one pass, or
whether `ffmpeg`'s segment muxer (`-hls_segment_type mpegts` +
`-force_key_frames` at the same ticks used for `cues.xml`) is the better
tool for just this leg. SCOPE.md's own precedent ("either is fine,
correctness matters more than invocation count") licenses either choice
— but this hasn't been verified against the pinned GPAC commit, so don't
assume GPAC can do it until confirmed by hand.

---

## Why the existing post-bake validation isn't optional, and applies here too

Context for whoever implements this: loop-dee-loop's `bake.py` doesn't
just build a manifest from a list of segments and markers — it's the
step that *produces* a trustworthy list in the first place. GPAC's
`dasher` `cues=...:cts` mode is documented (SCOPE.md §6) to silently snap
a requested tick to the nearest real frame with **no error**. That's
harmless for nominal grid points (normal keyframe-aligned segmenting),
but not for SCTE-35 marker ticks — those are ad-break boundaries, and a
silent snap there means the wrong content plays at the ad insertion
point. Because this architecture bakes once and serves the same bytes
forever (no re-validation once a channel is live), `bake.py` hard-fails
if any marker tick doesn't land with **zero** offset on a real produced
segment boundary (see `bake.py:891-899`). The same risk applies
identically to the new TS pass (same `cues.xml`, same snapping
mechanism, whichever tool ends up producing it), so the TS bake path
needs the same class of check, not a lighter one.

---

## 0. Prototyping spike (do this first, no code yet)

Against a real franken-ts fixture `.ts` (see `loop-dee-loop/tests/fixtures/`),
by hand on the CLI:
1. Try to get GPAC's `dasher` to emit muxed-TS HLS segments (`-o
   .../live.m3u8` with whatever flag forces MPEG-TS segments instead of
   CMAF — check `gpac -ha dasher` for the relevant option).
2. If that doesn't work cleanly, try `ffmpeg -i in.ts -c copy -f hls
   -hls_segment_type mpegts -force_key_frames <comma-separated boundary
   tick times>` against the *same* boundary set
   `compute_segment_boundary_ticks` already produces.
3. Confirm with `ffprobe`/`tsdump`/`threefive` that: (a) segments land
   exactly on the requested marker ticks, (b) real SCTE-35 splice_info
   survives into the output TS.

Write up which tool won and why in `loop-dee-loop/SCOPE.md` before
proceeding — this becomes the load-bearing rationale for whichever
`gpac_pipeline.py` function gets written.

## 1. `loop-dee-loop/gpac_pipeline.py`

New function, e.g. `run_muxed_ts_hls(input_ts, cues_xml, output_dir, *,
segment_duration_seconds, dry_run)`, sibling to `run_gpac_dasher`. Key
constraints:

- **Reuse the exact same `cues.xml`** already built for the CMAF pass
  (same marker ticks, same nominal grid) — do not recompute. This is
  what guarantees the TS rendition's segment boundaries land on the
  identical loop-relative ticks as the CMAF renditions, which `serve.py`
  depends on for shared segment indexing (see `serve.py:429-436`'s
  "every rendition/track has exactly the same segment count per loop"
  invariant).
- Keep `scte35dec:mode=passthrough` ahead of whichever muxer is chosen —
  this already puts real SCTE-35 into the output; for TS the difference
  is just that it lands natively in PES/PSI instead of being swallowed
  into fMP4 `emsg`/CMAF boxes.
- One muxed A/V TS segment per boundary (per the muxed-audio decision) —
  no separate video/audio track outputs for this leg, unlike the CMAF
  pass.
- Document the same GPAC-snapping risk callout as the existing module
  docstring (SCOPE.md §6) — it applies identically here; don't assume
  it's a CMAF-only quirk.

## 2. `loop-dee-loop/bake.py`

- New CLI flag: `--enable-hls-ts` (store_true), threaded through
  `bake()`'s per-rendition loop.
- New readback function `read_ts_segment_boundary_ticks(segments_dir)` —
  TS equivalent of `read_segment_boundary_ticks`, but via `ffprobe` (no
  `MP4Box -diso`/`tfdt`, since TS has no such box — read each segment's
  first video PTS via `ffprobe -show_packets`/`-read_intervals`). This is
  "ground truth," same philosophy as `compute_total_loop_duration_ticks`'s
  docstring: never trust the requested cue grid blindly.
- Reuse (don't duplicate) the marker-exactness hard-fail: refactor the
  existing `unmatched_markers` check (`bake.py:891-899`) into a small
  helper callable against either the CMAF boundaries or the TS
  boundaries, and call it for both when `--enable-hls-ts` is set.
  Additionally assert `ts_boundary_ticks == segment_boundary_ticks`
  outright (stronger than the audio/video count-only check today) —
  since both passes consume the identical `cues.xml`, any divergence at
  all is a hard failure, not just a marker-tick divergence.
- New in-band consistency check: decode the SCTE-35 actually embedded in
  the produced TS segments and compare against `markers.json`, reusing
  the same decode-and-compare pattern already used to validate the
  *source* `.ts` against `markers.json` (`bake.py:302-362`). Hard-fail
  on any mismatch, same rigor, same error style ("hard failure, not
  reconciled").
- Skip `repair_malformed_segments_in_dir` for the TS pass — that's a
  CMAF `moof`/`mdat` quirk with no TS analog; leave a one-line comment
  saying why, don't silently omit it without explanation.
- Write the new per-rendition data into `loop_descriptor.json` (additive,
  no version bump — see below).

## 3. `loop_descriptor.json` schema (additive, keep version 2)

Top-level: `"hls_ts_enabled": true|false` (mirrors the existing
`daterange_mode`/`cue_tags` top-level convention).

Per rendition, when enabled:
```json
"hls_ts": {
  "segment_boundary_ticks": [...],   // must equal the CMAF rendition's own segment_boundary_ticks
  "variant": { "bandwidth": ..., "codecs": "avc1...,mp4a...", "width": ..., "height": ..., "frame_rate": ... }
}
```
No version bump needed since old packages simply lack this key and
`hls_ts_enabled` defaults false — avoids repeating the "no v1→v2
migration" pain already flagged as a known limitation.

## 4. `loop-dee-loop/serve.py`

- `VideoRendition.__init__`: optionally discover `.ts` segments under a
  **separate subdirectory**, e.g. `segments/<name>/ts/*.ts`, so the glob
  never collides with the existing `*track{id}_*.m4s` pattern. Store
  `ts_segment_files`/`ts_boundary_ticks`, gate with a `has_ts` property.
- Generalize `_build_hls_media_playlist` to accept `init_uri: str |
  None` — TS has no init segment, so when `None`, skip emitting
  `#EXT-X-MAP` entirely. Everything else in that function (sliding
  window, `#EXT-X-DISCONTINUITY`, `PROGRAM-DATE-TIME`, `DATERANGE`/cue
  tags) is container-agnostic and should be reused as-is, not
  reimplemented.
- New `build_hls_ts_master_playlist()` — a genuinely **separate** master
  playlist (distinct filename requirement), not additional
  `#EXT-X-STREAM-INF` entries merged into `/master.m3u8`. No `AUDIO=`
  attribute, no `#EXT-X-MEDIA` audio group, `CODECS` carries both
  video+audio since audio is muxed in.
- New routes, registered conditionally on `package.hls_ts_enabled`
  (mirrors the existing `if package.has_audio:` conditional block at
  `serve.py:1046`):
  - `GET /master-ts.m3u8`
  - `GET /<rendition_name>/live-ts.m3u8`
  - `GET /<rendition_name>/ts/seg/<int:physical_index>.ts` —
    `mimetype="video/mp2t"`
- `LoopPackage.__init__`: read `hls_ts_enabled`, and if set, hard-fail
  (same "package inconsistent, refusing to serve" style as elsewhere) if
  any rendition is missing its `hls_ts` block.

## 5. its-a-live plumbing

- **Config schema** (`its-a-live/AGENTS.md`, `README.md`, wherever
  `config.toml` is documented): add to `[packaging]`:
  ```toml
  [packaging]
  segment_duration = 4.0
  dvr_window_seconds = 30
  hls_ts_enabled = false   # optional, default false -- also bake a 3rd output: HLS backed by raw
                            # muxed MPEG-TS segments (audio+video together, real in-band SCTE-35),
                            # served at a distinct master-ts.m3u8, alongside the default CMAF-based
                            # HLS+DASH. ecs-express/local-docker only -- error if set with
                            # backend = "aws-media" (that backend never runs bake.py/GPAC at all).
  ```
- **Validation**: wherever config is checked pre-deploy in `channel.py`,
  add a hard error when `hls_ts_enabled = true` and `backend =
  "aws-media"`.
- **`its-a-live/_ecs_express_ops.py::spark`** and
  **`its-a-live/_local_docker_ops.py`**'s bake invocation: read
  `cfg.get("packaging", {}).get("hls_ts_enabled", False)` and append
  `--enable-hls-ts` when true — same pattern already used for
  `increment_event_ids` (`_ecs_express_ops.py:61-62`).
- **`serve.py` invocation sites** (`loop_stack.py`'s ECS task command,
  `_local_docker_ops.py`'s `run_args`): **no changes** — route
  availability comes purely from the baked `loop_descriptor.json`,
  consistent with "bake once, serve reads back what was baked," not a
  serve-time flag.
- **`its-a-live/loop_stack.py` CDK**: the existing `*/seg/*` CloudFront
  cache behavior (`loop_stack.py:224-229`) already matches
  `<rendition>/ts/seg/*` as long as the URL layout keeps a `seg/` path
  segment (see §4) — no new cache behavior needed. Add a new
  `CfnOutput("HlsTsPlaybackUrl", value=f"https://{distribution.distribution_domain_name}/master-ts.m3u8")`,
  gated on `packaging_cfg.get("hls_ts_enabled", False)`.
- Wherever `channel.py start`/`status` prints `HlsPlaybackUrl`/
  `DashPlaybackUrl`, also print the TS URL when enabled.

## 6. Tests

- `loop-dee-loop/tests/`: new test(s) for the TS boundary-readback
  helper; a bake-level test with `--enable-hls-ts` against fixtures
  asserting: segment count/boundaries match the CMAF rendition exactly,
  marker ticks land with zero offset, in-band TS SCTE-35 matches
  `markers.json`.
- Extend the existing HLS master-playlist test coverage for the new TS
  master playlist (no audio group, distinct filename, correct
  `CODECS`).
- Extend `tests/test_bake_validation.py` to cover the new
  in-band-vs-`markers.json` hard-fail path for TS output.
- `tests/test_loop_math.py` stays untouched — confirm it still passes
  (no drift-math changes here).

## 7. Docs

- `loop-dee-loop/SCOPE.md`: new subsection documenting the TS pipeline
  choice (from the prototyping spike), its own GPAC/ffmpeg-snapping
  risk, and the muxed-audio/in-band-SCTE-35 rationale — same
  "load-bearing, not stylistic" treatment the CMAF pipeline already
  gets.
- `loop-dee-loop/README.md` "Known limitations": add whatever real
  corners get cut during implementation (don't pre-assert precision that
  hasn't been verified).
- `loop-dee-loop/AGENTS.md`, `its-a-live/AGENTS.md`: update the config
  schema blocks and module-layout tables to mention the new
  function(s)/flag.

---

## Igor front-end: 3rd player + config toggle

Traced the actual code paths — this slots in along the exact same seams
`increment_event_ids`/`daterange_mode`/`cue_tags` already use.

### 8. its-a-live: surface the URL + startup print

- **`its-a-live/_local_docker_ops.py`**: in the status function (`~line
  384-395`, where `hls_url`/`dash_url` are built), read
  `hls_ts_enabled` from `cfg.get("packaging", {})` and add:
  ```python
  hls_ts_url = f"http://localhost:{port}/master-ts.m3u8" if docker_status == "running" and port and hls_ts_enabled else None
  ...
  "hls_ts_url": hls_ts_url,
  ```
  Leave `check_manifest_reachable`'s `reachable` semantics untouched
  (HLS/DASH only) — don't fold TS into that gate, it's an additive
  extra, not a required signal.
- Same file, the `spark`-adjacent startup print block (`~line
  248-249`): add a third `print(f"HLS/TS: ...")` line, conditional on
  `hls_ts_enabled`.
- **`its-a-live/loop_stack.py`** (ecs-express CDK, `~line 239`): add
  `CfnOutput("HlsTsPlaybackUrl", ...)` as in §5, gated on
  `packaging_cfg.get("hls_ts_enabled", False)` so it's absent (not just
  empty) for channels that didn't opt in — matches how `ChannelOutputs`
  is a bare `Record<string,string>` on the frontend, so a missing key
  naturally hides the card.

### 9. Igor backend

- **`igor/src/igor/app/routes/channels.py`**, `ChannelCreatePayload`
  (`~line 24-43`): add `hls_ts_enabled: bool = False` next to
  `increment_event_ids`. Add a `model_validator` mirroring
  `_validate_port_backend` (`~line 78-82`):
  ```python
  @model_validator(mode="after")
  def _validate_hls_ts_backend(self) -> "ChannelCreatePayload":
      if self.hls_ts_enabled and self.backend == "aws-media":
          raise ValueError("hls_ts_enabled is only supported for ecs-express/local-docker (aws-media never runs bake.py/GPAC)")
      return self
  ```
  No change needed to `define_channel`/`update_channel` themselves —
  both already just spread `payload.model_dump()` into
  `its_a_live.generate_toml(**...)`, so this is free once the field
  exists on both sides.
- **`igor/src/igor/integrations/its_a_live.py`**: add `hls_ts_enabled:
  bool = False` to `generate_toml()`'s signature, and append
  `hls_ts_enabled = {hls_ts_enabled}` (via
  `str(hls_ts_enabled).lower()`, same treatment as
  `increment_event_ids`) into `_ECS_EXPRESS_EXTRA`/`_LOCAL_DOCKER_EXTRA`'s
  `[packaging]` block, alongside `segment_duration`/`dvr_window_seconds`
  (`~line 49-51` and `60-62`) — matches where it lives in the its-a-live
  TOML schema itself, *not* the `[markers]` block (that's HLS/DASH
  signaling shape; this is a different output entirely).

### 10. Igor frontend types (`frontend/src/api/types.ts`)

- `ChannelCreatePayload`: add `hls_ts_enabled?: boolean`.
- `ChannelStatus`: add `hls_ts_url?: string | null` (local-docker only,
  sibling to `hls_url`/`dash_url`).
- `ChannelOutputs` stays `Record<string, string>` as-is —
  `outputs.value?.HlsTsPlaybackUrl` just works once the CDK output
  exists, no type change needed.

### 11. Config toggle in the UI — `ChannelNew.vue` (create) and `ChannelDetail.vue` (edit)

Both forms are structurally identical (confirmed — `ChannelNew.vue:51-58`
mirrors `ChannelDetail.vue:74-81` field-for-field, same
`usesChannelSection`/`editUsesChannelSection` gating), so make the same
three edits in both:
1. Add `hls_ts_enabled: false` to the reactive form default
   (`ChannelNew.vue` `form`, `ChannelDetail.vue` `editForm`).
2. `ChannelDetail.vue`'s `startEdit()` (`~line 152-153`, where
   `packaging` section fields are read): add `hls_ts_enabled:
   Boolean(packaging.hls_ts_enabled ?? false)` — read from the
   already-destructured `packaging` object, not `markers` (see §9's
   schema placement).
3. Add a Checkbox control inside the existing `<template
   v-if="editUsesChannelSection">` / `usesChannelSection` block — this
   gating already matches "ecs-express/local-docker only," no new
   conditional needed. Suggested placement: near `increment_event_ids`
   since both are boolean output-shape toggles, with a one-line label
   like *"Also bake HLS/TS (raw MPEG-TS segments, in-band SCTE-35) as a
   3rd output."*

### 12. `ChannelDetail.vue`: the third playback URL

Mirror the existing `playbackHlsUrl`/`playbackDashUrl` computed pair
(`~line 486-491`):
```ts
const playbackHlsTsUrl = computed(() =>
  status.value?.backend === 'local-docker' ? status.value.hls_ts_url : outputs.value?.HlsTsPlaybackUrl,
)
```
Pass as a new `:hls-ts-url="playbackHlsTsUrl"` prop on `<PlaybackPanel>`
(`~line 689-696`). Leave `showPlayback`'s gating (`~line 500-502`) as-is
— it only needs *any* URL present to mount the panel; each card inside
`PlaybackPanel` already gates on its own URL prop.

### 13. `PlaybackPanel.vue`: the actual 3rd player

- New prop `hlsTsUrl?: string | null`.
- New state: `hlsTsVideo`, `hlsTsError`, `hlsTsLoading`, `hlsTsPlaying`,
  `hlsTsPlayheadTime`, `hlsTsInstance`, `hlsTsMarkerToasts`,
  `hlsTsSeenActivations`.
- `playHlsTs()`: reuse **hls.js**, not a new library — hls.js natively
  demuxes MPEG-TS segments (that was its original segment format before
  CMAF support existed), so this is the same player, pointed at
  `master-ts.m3u8` instead of `master.m3u8`. It gets the same
  `LatencyController` settings, the same `FRAG_CHANGED`-based wall-clock
  playhead mapping, and the same `attachHlsMetadataCueListener` SCTE-35
  toast wiring `playHls()` already has (`~line 254-332`).
  **Judgment call for the implementing agent:** `playHls()`/`playDash()`
  today are two independent ~80-line copies (the codebase's own
  precedent — they differ by library, so duplication was the right
  call). A third copy of `playHls()` differing *only* by which
  refs/url it touches is a different situation — recommend factoring
  `playHls()`'s body into a small helper parameterized by (video ref,
  url, error/loading/playing refs, marker-toast list, seen-activations
  set) and having both `playHls()` and `playHlsTs()` call it, rather
  than a third hand-copied block. Either is acceptable; flag the choice,
  don't dictate it.
- `destroyHlsTs()` mirrors `destroyHls()`; wire into the same
  `watch(...)`/`reloadPlayers()`/`onBeforeUnmount()` registrations
  HLS/DASH already use (`~line 511-538`).
- Template: third `.player-card` in `.players-grid`, `v-if="hlsTsUrl"`,
  labeled **"HLS/TS"** (distinct from "HLS" so the CMAF-vs-TS
  distinction is visible at a glance, not just infer-able from the
  filename in the URL box).

### 14. Sizing — three players, smaller

`.players-grid` is already `grid-template-columns: repeat(auto-fit,
minmax(280px, 1fr))` (`~line 806`), so it already reflows to fit however
many cards are present rather than hardcoding 2. With a 3rd card this
responsively wraps at narrower widths, but the goal is: 3 should
comfortably fit side-by-side at typical desktop widths without wrapping
to a second row. Concretely: lower the `minmax` floor (e.g. `280px` →
`220px`) so 3 cards fit ~1280px+ viewports without hardcoding `repeat(3,
1fr)` (which would break the 1-or-2-player layout when TS is disabled).
Verify by hand in a browser at 1280/1440/1920 widths once wired up —
this is a CSS number that wants eyeballing, not derivation.

---

## Suggested sequencing

1. Prototyping spike (§0) — resolves the GPAC-vs-ffmpeg question with
   evidence, not assumption.
2. `gpac_pipeline.py` new function + a throwaway manual check
   (ffprobe/tsdump) that boundaries and in-band SCTE-35 look right.
3. `bake.py` readback + validation, tested against the fixture.
4. `loop_descriptor.json` schema + `serve.py` routes/manifest,
   sanity-checked by curling the new endpoints and loading
   `master-ts.m3u8` in `ffplay`/VLC/hls.js.
5. its-a-live config plumbing + CDK output (§5).
6. Docs (§7), once the real shape (not the planned shape) is known.
7. its-a-live URL/output plumbing (§8).
8. Igor backend model + TOML template (§9).
9. Igor frontend types + config forms (§10-11), verified by creating a
   channel with the box checked and confirming the TOML comes out
   right.
10. `PlaybackPanel.vue` 3rd player (§12-14), tested against a real
    baked HLS/TS package once loop-dee-loop's side of this plan is
    actually working end-to-end — this piece is meaningless to
    build/test before that.
