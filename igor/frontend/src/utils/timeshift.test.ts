// Run: node --test --experimental-strip-types src/utils/timeshift.test.ts
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  DEFAULT_TIMESHIFT_PARAMS as P,
  buildTimeshiftUrl,
  classify,
  dateToInputValue,
  effectiveRange,
  formatInstant,
  inputValueToDate,
  timeshiftParamsFromConfig,
  validateRequest,
  type TimeshiftRequest,
} from './timeshift.ts'

const at = (iso: string) => new Date(iso)
const req = (over: Partial<TimeshiftRequest> = {}): TimeshiftRequest => ({
  start: at('2026-09-30T08:00:00Z'),
  end: at('2026-09-30T08:10:00Z'),
  fullLoop: false,
  timeline: 'default',
  format: 'iso',
  ...over,
})
const NOW = at('2026-09-30T12:00:00Z').getTime()

test('formatInstant: iso drops zero millis, keeps real ones', () => {
  assert.equal(formatInstant(at('2026-09-30T08:00:00Z'), 'iso'), '2026-09-30T08:00:00Z')
  assert.equal(formatInstant(at('2026-09-30T08:00:00.250Z'), 'iso'), '2026-09-30T08:00:00.250Z')
  assert.equal(formatInstant(at('2026-09-30T08:00:00.999Z'), 'epoch'), '1790755200')
  assert.equal(formatInstant(at('2026-09-30T08:00:00Z'), 'epoch_ms'), '1790755200000')
})

test('buildTimeshiftUrl: catchup with default names', () => {
  const url = buildTimeshiftUrl('https://cdn.example/index.m3u8', P, req())
  assert.equal(url, 'https://cdn.example/index.m3u8?start=2026-09-30T08:00:00Z&end=2026-09-30T08:10:00Z')
})

test('buildTimeshiftUrl: custom names, full loop, timeline override, epoch values', () => {
  const params = { ...P, start_param: 'from', end_param: 'to', full_loop_param: 'whole' }
  const url = buildTimeshiftUrl('http://localhost:8080/stream.mpd', params, req({ fullLoop: true, timeline: 'periodic', format: 'epoch' }))
  assert.equal(url, 'http://localhost:8080/stream.mpd?from=1790755200&to=1790755800&whole=true&timeline=periodic')
})

test('buildTimeshiftUrl: no start means live; timeline alone is still a valid override', () => {
  const live = req({ start: null, end: null })
  assert.equal(buildTimeshiftUrl('http://x/index.m3u8', P, live), 'http://x/index.m3u8')
  assert.equal(
    buildTimeshiftUrl('http://x/index.m3u8', P, { ...live, timeline: 'continuous' }),
    'http://x/index.m3u8?timeline=continuous',
  )
})

test('buildTimeshiftUrl: appends to an existing query; ignores end/full-loop without start', () => {
  assert.equal(buildTimeshiftUrl('http://x/a?token=1', P, req({ end: null })), 'http://x/a?token=1&start=2026-09-30T08:00:00Z')
  assert.equal(buildTimeshiftUrl('http://x/a', P, req({ start: null })), 'http://x/a')
})

test('classify', () => {
  assert.equal(classify(req({ start: null, end: null }), NOW), 'live')
  assert.equal(classify(req(), NOW), 'catchup')
  assert.equal(classify(req({ end: null }), NOW), 'startover-open')
  assert.equal(classify(req({ end: at('2026-09-30T13:00:00Z') }), NOW), 'startover-bounded')
})

test('validateRequest: ok, future start, reversed, over max span, end without start', () => {
  assert.deepEqual(validateRequest(req(), P, NOW), [])
  assert.match(validateRequest(req({ start: at('2026-09-30T13:00:00Z'), end: null }), P, NOW)[0], /future/)
  assert.match(validateRequest(req({ end: at('2026-09-30T07:00:00Z') }), P, NOW)[0], /after start/)
  assert.match(validateRequest(req({ start: at('2026-09-30T00:00:00Z'), end: at('2026-09-30T08:00:00Z') }), P, NOW)[0], /at most 6h/)
  assert.match(validateRequest(req({ start: null }), P, NOW)[0], /requires/)
})

test('validateRequest: before the channel epoch', () => {
  const timing = { epochMs: at('2026-09-30T09:00:00Z').getTime(), loopMs: 60_000 }
  assert.match(validateRequest(req(), P, NOW, timing).join(' '), /before the channel epoch/)
})

// Loop = 10 min, epoch 08:00:00 -> loops at 08:00, 08:10, 08:20 ...
const timing = { epochMs: at('2026-09-30T08:00:00Z').getTime(), loopMs: 600_000 }

test('effectiveRange: full loop widens start down and end up', () => {
  const r = effectiveRange(req({ start: at('2026-09-30T08:13:00Z'), end: at('2026-09-30T08:21:00Z'), fullLoop: true }), P, timing)!
  assert.equal(new Date(r.startMs).toISOString(), '2026-09-30T08:10:00.000Z')
  assert.equal(new Date(r.endMs).toISOString(), '2026-09-30T08:30:00.000Z')
  assert.equal(r.capped, false)
})

test('effectiveRange: end exactly on a loop boundary is unchanged; open end is capped to whole loops', () => {
  const exact = effectiveRange(req({ start: at('2026-09-30T08:05:00Z'), end: at('2026-09-30T08:20:00Z'), fullLoop: true }), P, timing)!
  assert.equal(new Date(exact.endMs).toISOString(), '2026-09-30T08:20:00.000Z')
  const open = effectiveRange(req({ start: at('2026-09-30T08:05:00Z'), end: null, fullLoop: true }), { ...P, max_span_seconds: 1500 }, timing)!
  assert.equal((open.endMs - open.startMs) / 600_000, 2) // floor(1500s / 600s) loops
  assert.equal(open.capped, true)
})

test('validateRequest: max span is checked after full-loop widening', () => {
  const params = { ...P, max_span_seconds: 1200 } // 2 loops
  const r = req({ start: at('2026-09-30T08:09:00Z'), end: at('2026-09-30T08:11:00Z') }) // 2 min raw
  assert.deepEqual(validateRequest(r, params, NOW, timing), [])
  const widened = validateRequest({ ...r, fullLoop: true }, params, NOW, timing) // -> 08:00..08:20 = 2 loops, still ok
  assert.deepEqual(widened, [])
  const tooWide = validateRequest({ ...r, start: at('2026-09-30T08:09:00Z'), end: at('2026-09-30T08:21:00Z'), fullLoop: true }, params, NOW, timing) // 3 loops
  assert.match(tooWide[0], /after widening/)
})

test('timeshiftParamsFromConfig: missing table = defaults (enabled); overrides and junk', () => {
  assert.deepEqual(timeshiftParamsFromConfig(undefined), P)
  assert.equal(timeshiftParamsFromConfig({ enabled: false }).enabled, false)
  const p = timeshiftParamsFromConfig({ start_param: 'from', max_span_seconds: 60, end_param: '', full_loop_param: 7 })
  assert.equal(p.start_param, 'from')
  assert.equal(p.end_param, 'end')
  assert.equal(p.full_loop_param, 'full-loops')
  assert.equal(p.max_span_seconds, 60)
})

test('datetime-local round trip in UTC and local', () => {
  const d = at('2026-09-30T08:05:09Z')
  assert.equal(dateToInputValue(d, true), '2026-09-30T08:05:09')
  assert.equal(inputValueToDate('2026-09-30T08:05:09', true)!.getTime(), d.getTime())
  const local = inputValueToDate(dateToInputValue(d, false), false)!
  assert.equal(local.getTime(), d.getTime())
  assert.equal(inputValueToDate('', true), null)
  assert.equal(inputValueToDate('garbage', true), null)
  assert.equal(inputValueToDate('2026-09-30T08:05', true)!.getUTCSeconds(), 0) // seconds optional
})
