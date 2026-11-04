// Run: node --test --experimental-strip-types src/utils/manifestCapture.test.ts
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ManifestStore, changedLines, classifyManifest, lineKind, looksLikeManifestUrl, parseVariants, summarizeManifest } from './manifestCapture.ts'

const MASTER = `#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
video/index.m3u8
`
const media = (seq: number) => `#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:${seq}\n#EXTINF:4,\nseg${seq}.m4s\n`
const H = 'http://h/'

test('classify and url matching', () => {
  assert.equal(classifyManifest(MASTER), 'master')
  assert.equal(classifyManifest(media(1)), 'media')
  assert.equal(classifyManifest('<?xml version="1.0"?><MPD xmlns="x"></MPD>'.replace('<?xml version="1.0"?>', '')), 'mpd')
  assert.equal(classifyManifest('{"a":1}'), null)
  assert.ok(looksLikeManifestUrl('http://h/a/index.m3u8?x=1'))
  assert.ok(looksLikeManifestUrl('http://h/manifest', 'application/vnd.apple.mpegurl'))
  assert.ok(!looksLikeManifestUrl('http://h/seg1.m4s'))
})

test('variants are labelled from the master', () => {
  const v = parseVariants(MASTER, H + 'index.m3u8')
  assert.equal(v.get(H + 'video/index.m3u8'), 'Video · 1280x720')
  assert.equal(v.get(H + 'audio/index.m3u8'), 'Audio · English · aud')
})

test('history keeps the last N per slot, unchanged polls only count hits', () => {
  const s = new ManifestStore(3)
  s.add(H + 'index.m3u8', 200, '', MASTER)
  for (let i = 1; i <= 5; i++) s.add(H + 'video/index.m3u8', 200, '', media(i))
  s.add(H + 'video/index.m3u8', 200, '', media(5)) // same body again
  const video = s.entries.filter((e) => e.slot !== 'master')
  assert.equal(video.length, 3)
  assert.deepEqual(video.map((e) => e.body.includes('SEQUENCE:5')), [false, false, true])
  assert.equal(video[2].hits, 2)
  assert.equal(s.slots()[0].label, 'Master playlist')
  assert.equal(s.slots()[1].label, 'Video · 1280x720')
})

test('pinned versions survive eviction; clearUnpinned keeps them', () => {
  const s = new ManifestStore(2)
  s.add(H + 'index.m3u8', 200, '', MASTER)
  const first = s.add(H + 'video/index.m3u8', 200, '', media(1))!
  s.togglePin(first.id)
  for (let i = 2; i <= 6; i++) s.add(H + 'video/index.m3u8', 200, '', media(i))
  const video = s.entries.filter((e) => e.slot !== 'master')
  assert.equal(video.length, 3) // the pin + the newest two
  assert.ok(video[0].pinned)
  s.clearUnpinned()
  assert.deepEqual(s.entries.map((e) => e.pinned), [true])
})

test('errors and non-manifests are ignored; only the first master is the stream', () => {
  const s = new ManifestStore()
  assert.equal(s.add(H + 'index.m3u8', 404, '', MASTER), null)
  assert.equal(s.add(H + 'x.m3u8', 200, '', 'nope'), null)
  assert.ok(s.add(H + 'index.m3u8', 200, '', MASTER))
  assert.equal(s.add(H + 'ad/master.m3u8', 200, '', MASTER), null)
})

test('changed lines and line kinds', () => {
  assert.deepEqual([...changedLines(media(2), media(1))].sort(), [1, 3])
  assert.equal(changedLines(media(2), undefined).size, 0)
  assert.equal(lineKind('#EXT-X-DATERANGE:ID="a"', 'hls'), 'marker')
  assert.equal(lineKind('#EXT-X-DISCONTINUITY', 'hls'), 'discontinuity')
  assert.equal(lineKind('seg1.m4s', 'hls'), 'uri')
  assert.equal(lineKind('<EventStream schemeIdUri="urn:scte:scte35:2013:xml">', 'dash'), 'marker')
  assert.equal(lineKind('<Period id="1">', 'dash'), 'discontinuity')
})

test('only the first video variant is kept; the MPD tab comes first', () => {
  const s = new ManifestStore()
  const master = MASTER + '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow/index.m3u8\n'
  s.add(H + 'index.m3u8', 200, '', master)
  assert.ok(s.add(H + 'video/index.m3u8', 200, '', media(1)))
  assert.equal(s.add(H + 'low/index.m3u8', 200, '', media(1)), null) // another video variant: ignored
  assert.ok(s.add(H + 'audio/index.m3u8', 200, '', media(1))) // audio is still kept
  s.add(H + 'stream.mpd', 200, '', '<MPD xmlns="x"></MPD>')
  assert.deepEqual(s.slots().map((x) => x.label), ['MPD', 'Master playlist', 'Video · 1280x720', 'Audio · English · aud'])
})

test('HLS media playlist summary: segments, duration, first PROGRAM-DATE-TIME', () => {
  const body = '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-PROGRAM-DATE-TIME:2026-10-25T10:00:00.000Z\n#EXTINF:4.0,\na.m4s\n#EXTINF:3.5,\nb.m4s\n#EXT-X-PROGRAM-DATE-TIME:2026-10-25T10:00:07.5Z\n#EXTINF:4,\nc.m4s\n'
  assert.deepEqual(summarizeManifest(body, 'media'), { segments: 3, durationS: 11.5, firstStart: '2026-10-25T10:00:00.000Z', note: undefined })
  assert.equal(summarizeManifest('#EXTM3U\n#EXTINF:4,\na.m4s\n', 'media')?.firstStart, null)
  assert.equal(summarizeManifest(MASTER, 'master'), null)
})

test('DASH summary: video SegmentTimeline over periods, first start from AST + period + t - pto', () => {
  const mpd = `<MPD availabilityStartTime="2026-01-01T00:00:00Z" type="dynamic">
  <Period id="1" start="PT100S">
    <AdaptationSet contentType="audio"><SegmentTemplate timescale="48000"><SegmentTimeline><S t="0" d="192000"/></SegmentTimeline></SegmentTemplate></AdaptationSet>
    <AdaptationSet contentType="video"><SegmentTemplate timescale="90000" presentationTimeOffset="180000"><SegmentTimeline>
      <S t="270000" d="360000" r="2"/><S d="180000"/></SegmentTimeline></SegmentTemplate></AdaptationSet>
  </Period>
  <Period id="2" start="PT116S"><AdaptationSet contentType="video"><SegmentTemplate timescale="90000"><SegmentTimeline><S t="0" d="360000"/></SegmentTimeline></SegmentTemplate></AdaptationSet></Period>
</MPD>`
  const s = summarizeManifest(mpd, 'mpd')!
  assert.equal(s.segments, 5)
  assert.equal(s.durationS, 12 + 2 + 4)
  assert.equal(s.firstStart, '2026-01-01T00:01:41.000Z') // 100 s + (270000-180000)/90000 = 101 s
})
