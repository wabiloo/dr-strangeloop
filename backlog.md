# Backlog

Things we decided to defer, with enough context to pick them up later. Newest
design context: `loop-dee-loop/SCOPE.md` §13.8 (per-request query parameters) and
`its-a-live/AGENTS.md` (`config_version`, `[query]`).

## 1. Opt-in "bake every HLS form", then choose it per request

**Idea.** Today `[packaging.hls] container` (`cmaf` | `ts`) and `mux_audio` decide
the baked HLS segment bytes, so they are TOML-only. Bake several forms at bake
time and let a request choose among them, the way `descriptor_grouping` already
chooses between the shared and narrowed SCTE-35 message.

**What is in a package today.** CMAF fragments (`segments/`) are always baked; DASH
and CMAF-HLS use them. `container = "ts"` adds an ffmpeg remux of those fragments
into `hls-ts/`; `mux_audio` decides whether that remux puts audio in each video TS
file or writes a separate `hls-ts/audio` set.

**Design sketch.**
- Opt-in, never default: a TOML list of what to bake (e.g. `[packaging.hls]
  containers = [...]`, plus the audio-mux choices). Defaults stay as today.
- Cost of baking all three (CMAF + TS muxed + TS video-only with separate audio):
  about 3x storage (1x CMAF only, 2x with one TS flavor), two ffmpeg remux passes
  per segment per rendition, larger S3 sync on every ECS task start, larger
  `bundle_content` images.
- Request side: `container` and `mux_audio` query parameters (new names in
  `timeshift.FIXED_PARAMS`, `_query_cfg.FIXED_PARAMS`, Igor's `QUERY_FIXED_PARAMS`, the
  OpenAPI spec). A value the package did not bake is a `400` naming what it holds
  (same guard as `descriptor_grouping`; marker in `loop_descriptor.json`, like
  `scte35_payloads`).
- **Path contract (the careful part).** Muxed and video-only TS would both be
  `/<r>/seg/N.ts`; a URL must always return the same bytes (CDN caching), so each TS
  flavor needs its own path. Playlist names already depend on muxing (`video.m3u8` vs
  `video_audio.m3u8`) and routes are registered once at startup from the baked flags:
  both sets must be registered.
- Not bakeable "in every possibility": segment duration / GOP structure (continuous
  values, they are the cut points of the bytes).

**Cheap subset, if wanted first.** `container=cmaf` on a package baked as TS is a
manifest-only change (the CMAF fragments already exist for DASH); everything else
needs a second bake or a per-segment remux.

## 2. Marker filtering (by type, or switched off)

Deferred: a per-request filter of which SCTE-35 markers a manifest signals
(e.g. `marker_types=0x22,0x23`, or none for a clean feed), as a way to give each
consumer of one channel its own marker view.

Open questions: interaction with `split_on`; keeping Start/End pairs together;
markers already embedded in baked TS bytes cannot be filtered by a manifest change.
A server-side flag may be enough if the only need is testing. (`audio=false` was
dropped for good: TS with muxed audio cannot drop it.)

## 3. Igor API / UI field names

The Igor API payload and form fields still use the older flat names
(`dvr_window_seconds`, `hls_format`, `daterange_mode`, `dash_descriptor_mode`,
`timeshift_enabled`, ...); `formFieldsFromConfig` and `its_a_live.generate_toml` map
them to the TOML. The groups already follow the TOML tables. To do: rename the
fields to the TOML names, one `descriptor_grouping` per format, no `hls_`/`dash_`
prefixes (mapping table: `igor/AGENTS.md`, "Interim naming gap").

## 4. Smaller follow-ups

- **DASH `descriptor_grouping = grouped`.** DASH only has `shared | narrowed`; decide
  whether `grouped` makes sense there.
- **Real multi-descriptor test.** `descriptor_grouping` is tested with stand-in
  payloads; add an end-to-end test with a real coincident multi-descriptor SCTE-35
  message through bake and serve.
- **Release notes.** 2.0.0 breaks the config keys, `config_version`, the query
  parameter names and the `/health` shape; there is no CHANGELOG yet.
- **CloudFront cache key.** With both `[query]` switches on there are 17 names, over
  CloudFront's default 10-name allow-list, so the manifest cache keys on the whole
  query string. Revisit if the quota is raised or manifests become cacheable longer.
