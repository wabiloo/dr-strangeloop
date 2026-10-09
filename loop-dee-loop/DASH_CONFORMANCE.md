# DASH MPD conformance review

Review of the MPDs `serve.py` generates (2026-10-08). Checked against the
DASH XSD (MPEGGroup/DASHSchema 5th-Ed, via `xmllint`) and manually against
DASH-IF IOP v4.3 and AWS MediaTailor's DASH ad-marker requirements.

**The official DASH-IF conformance validator was NOT run**, and ISO/IEC
23009-1 itself (paywalled) was not consulted -- the Event-ordering rule below
is from memory of that spec plus MediaTailor's documented behavior.

Variants checked: live per-loop, live continuous, `--period-on-segmentation`,
`offset` (UTCTiming), ended and growing startover ranges (per-loop and
continuous). All pass the XSD except the negative-`presentationTime` case (#2).

## Fixed

- `@frameRate` was written as `25.000`; DASH only allows `N` or `N/D`
  (MediaTailor returned an error). Now `25`, `30000/1001`, etc.
  (`_dash_frame_rate` in `serve.py`; `bake.py` also parses `N/D` now).
- `<Event>`s within an `<EventStream>` were not ordered by `presentationTime`
  in per-loop mode. MediaTailor (multi-period mode) acts on the *first* Event
  per Period, so order matters. Now sorted (`_join_events`).

## Open

1. **Missing attributes IOP v4.3 §3.2.4 marks "shall".**
   - Video Representation: `@sar`. Video AdaptationSet: `@par` and
     `@maxWidth`/`@maxHeight`/`@maxFrameRate` (or `@width`/`@height`/
     `@frameRate` at AdaptationSet level).
   - Audio AdaptationSet: `@lang`. Audio Representation: `@audioSamplingRate`
     and `AudioChannelConfiguration`.
   - `@sar` is present in the source GPAC MPD but not carried through
     `bake.py`'s variant metadata; the audio ones need probing in `bake.py`.
2. **Negative `Event@presentationTime`** in startover ranges that begin inside
   an ad break (e.g. `-13050000`); the XSD type is `xs:unsignedLong`. Option:
   clamp to 0 and shorten `duration` so the Event ends at the same moment.
   Changes mid-break semantics, so undecided.
3. **Newest segment is advertised up to ~3.5 s past `publishTime`.** Deliberate
   (see the `suggestedPresentationDelay` comment in `serve.py`), but strictly a
   segment is only available once complete.
4. **Per-loop mode lists only the HLS-style trailing window** (last N
   segments, kept segments' numbers/`t`/`Period@start` stable between
   refreshes). Verified with dash.js and Shaka (`player-lab/`); an earlier
   belief that players need the whole open Period was wrong.
5. **Namespace for `urn:scte:scte35:2014:xml+bin`.** We emit
   `urn:scte:scte35:2013:xml`; AWS's xml+bin example uses
   `http://www.scte.org/schemas/35/2016`, threefive uses
   `https://scte.org/schemas/35`. Sources disagree; low risk.
6. Minor: audio segments at Period boundaries can start 8-21 ms before the
   Period start (normal); `UTCTiming` is emitted after `Period` (XSD accepts
   it; ISO child ordering not checked).
