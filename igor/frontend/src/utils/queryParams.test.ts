// Run: node --test --experimental-strip-types src/utils/queryParams.test.ts
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  FIELD_OVERRIDES,
  QP_NAMES,
  QP_SPECS,
  appliesTo,
  overrideForField,
  overridePairs,
  parseDurationSeconds,
  parseInstantMs,
  parseSplitOn,
  qpSpec,
  validateOverrides,
} from './queryParams.ts'

const NOW = Date.parse('2026-09-30T12:00:00Z')
const ctx = { maxSpanSeconds: 3600, nowMs: NOW }

test('registry: the override names, in the order loop-dee-loop documents them', () => {
  assert.deepEqual(QP_NAMES, [
    'timeline_mode', 'epoch', 'dvr_window', 'timeline_style', 'addressing', 'cue_tags', 'descriptor_grouping',
    'increment_event_ids', 'signal_format', 'id_template', 'split_on', 'max_height', 'max_bitrate',
  ])
  assert.equal(new Set(QP_NAMES).size, QP_NAMES.length)
})

test('registry: the startover parameters are not overrides (they have their own switch)', () => {
  for (const timeshift of ['start', 'end', 'full_loops', 'offset']) assert.ok(!QP_NAMES.includes(timeshift))
  for (const sw of ['timeshift', 'overrides', 'enabled']) assert.ok(!QP_NAMES.includes(sw)) // the switches are TOML only
})

test('registry: per-format applicability matches the server', () => {
  const only = (fmt: 'hls' | 'dash') => QP_SPECS.filter((s) => s.formats.length === 1 && s.formats[0] === fmt).map((s) => s.name)
  assert.deepEqual(only('hls'), ['cue_tags', 'id_template'])
  assert.deepEqual(only('dash'), ['timeline_style', 'addressing', 'signal_format'])
})

test('qpSpec: lookup and unknown', () => {
  assert.equal(qpSpec('epoch').kind, 'instant')
  assert.throws(() => qpSpec('nope'), /unknown query parameter/)
})

test('every choice spec has options and a help text', () => {
  for (const spec of QP_SPECS) {
    assert.ok(spec.help.length > 10, spec.name)
    if (spec.kind === 'choice' || spec.kind === 'bool') assert.ok(spec.options && spec.options.length >= 2, spec.name)
  }
})

test('form fields map to the parameter that overrides them', () => {
  assert.deepEqual(overrideForField('dvr_window_seconds'), { name: 'dvr_window', formats: ['hls', 'dash'] })
  assert.deepEqual(overrideForField('daterange_mode'), { name: 'descriptor_grouping', formats: ['hls'] })
  assert.deepEqual(overrideForField('dash_descriptor_mode'), { name: 'descriptor_grouping', formats: ['dash'] })
  assert.equal(overrideForField('continuous')!.name, 'timeline_mode')
  // fixed at bake time: nothing to override
  for (const field of ['segment_duration', 'hls_format', 'hls_ts_mux_audio', 'port', 'name']) {
    assert.equal(overrideForField(field), undefined, field)
  }
  for (const { name } of Object.values(FIELD_OVERRIDES)) assert.ok(QP_NAMES.includes(name), name)
})

test('every override except max_height/max_bitrate has a channel setting to badge', () => {
  const badged = new Set(Object.values(FIELD_OVERRIDES).map((o) => o.name))
  assert.deepEqual(QP_NAMES.filter((n) => !badged.has(n)), ['max_height', 'max_bitrate'])
})

test('appliesTo: format-only parameters, and grouped has no DASH form', () => {
  assert.equal(appliesTo(qpSpec('cue_tags'), 'hls'), true)
  assert.equal(appliesTo(qpSpec('cue_tags'), 'dash'), false)
  assert.equal(appliesTo(qpSpec('addressing'), 'hls'), false)
  assert.equal(appliesTo(qpSpec('epoch'), 'dash'), true)
  assert.equal(appliesTo(qpSpec('descriptor_grouping'), 'hls', 'grouped'), true)
  assert.equal(appliesTo(qpSpec('descriptor_grouping'), 'dash', 'grouped'), false)
  assert.equal(appliesTo(qpSpec('descriptor_grouping'), 'dash', 'narrowed'), true)
})

test('overridePairs: set values only, trimmed, in registry order, per format', () => {
  const values = {
    max_height: ' 720 ', cue_tags: 'alongside', addressing: 'time', epoch: '', split_on: '0x22', dvr_window: undefined,
    descriptor_grouping: 'grouped',
  }
  assert.deepEqual(overridePairs(values, 'hls'), [
    ['cue_tags', 'alongside'], ['descriptor_grouping', 'grouped'], ['split_on', '0x22'], ['max_height', '720'],
  ])
  assert.deepEqual(overridePairs(values, 'dash'), [['addressing', 'time'], ['split_on', '0x22'], ['max_height', '720']])
  assert.deepEqual(overridePairs(undefined, 'hls'), [])
  assert.deepEqual(overridePairs({}, 'dash'), [])
})

test('parseDurationSeconds: seconds and ISO durations, positive only', () => {
  assert.equal(parseDurationSeconds('30'), 30)
  assert.equal(parseDurationSeconds('7.5'), 7.5)
  assert.equal(parseDurationSeconds('PT30S'), 30)
  assert.equal(parseDurationSeconds('PT1H30M'), 5400)
  assert.equal(parseDurationSeconds('p1d'), 86400)
  for (const bad of ['', '0', 'PT0S', '-30', 'abc', 'P', 'PT', '30s', 'P1M']) assert.equal(parseDurationSeconds(bad), null, bad)
})

test('parseInstantMs: epoch seconds, epoch ms, ISO with and without a zone', () => {
  assert.equal(parseInstantMs('1790755200'), 1790755200_000)
  assert.equal(parseInstantMs('1790755200000'), 1790755200_000) // >= 1e11 is milliseconds
  assert.equal(parseInstantMs('2026-09-30T08:00:00Z'), Date.parse('2026-09-30T08:00:00Z'))
  assert.equal(parseInstantMs('2026-09-30T08:00:00'), Date.parse('2026-09-30T08:00:00Z')) // naive = UTC
  assert.equal(parseInstantMs('2026-09-30T10:00:00+02:00'), Date.parse('2026-09-30T08:00:00Z'))
  for (const bad of ['', 'tomorrow', '12abc', '2026-13-45T00:00:00Z']) assert.equal(parseInstantMs(bad), null, bad)
})

test('parseSplitOn: ids, none/empty clear, junk is null', () => {
  assert.deepEqual(parseSplitOn('0x22,0x30'), [0x22, 0x30])
  assert.deepEqual(parseSplitOn(' 34 , 0x30 '), [34, 0x30])
  assert.deepEqual(parseSplitOn(''), [])
  assert.deepEqual(parseSplitOn('none'), [])
  assert.deepEqual(parseSplitOn('NONE,0x22'), [0x22])
  for (const bad of ['abc', '0x22,zz', '256', '-1', '0x100']) assert.equal(parseSplitOn(bad), null, bad)
})

test('validateOverrides: nothing set, or valid values, is fine', () => {
  assert.deepEqual(validateOverrides(undefined, ctx), [])
  assert.deepEqual(validateOverrides({}, ctx), [])
  assert.deepEqual(
    validateOverrides({
      timeline_mode: 'continuous', epoch: '2026-09-30T08:00:00Z', dvr_window: 'PT1M', timeline_style: 'compact',
      addressing: 'time', cue_tags: 'only', descriptor_grouping: 'narrowed', increment_event_ids: 'true',
      signal_format: 'xml', id_template: 'ad-{eventid}-{loop}', split_on: '0x22', max_height: '720', max_bitrate: '2000000',
    }, ctx),
    [],
  )
})

test('validateOverrides: each bad value names its parameter label', () => {
  const bad = (name: string, value: string) => validateOverrides({ [name]: value }, ctx)
  assert.match(bad('timeline_mode', 'sometimes')[0], /Timeline mode.*continuous, periodic/)
  assert.match(bad('epoch', 'tomorrow')[0], /Channel epoch must be/)
  assert.match(bad('epoch', '2026-10-01T00:00:00Z')[0], /future/)
  assert.match(bad('dvr_window', '-1')[0], /DVR window must be/)
  assert.match(bad('dvr_window', '7200')[0], /limited to 3600 seconds/)
  assert.match(bad('cue_tags', 'maybe')[0], /CUE-OUT\/CUE-IN tags/)
  assert.match(bad('increment_event_ids', 'perhaps')[0], /Increment event ids/)
  assert.match(bad('signal_format', 'json')[0], /signal format/)
  assert.match(bad('id_template', '{nope}')[0], /unknown placeholder \{nope\}/)
  assert.match(bad('id_template', '{eventid')[0], /unbalanced/)
  assert.match(bad('split_on', 'zz')[0], /segmentation_type_ids/)
  assert.match(bad('max_height', '0')[0], /positive whole number/)
  assert.match(bad('max_bitrate', 'fast')[0], /positive whole number/)
})

test('validateOverrides: an epoch is judged against the pretend "now"', () => {
  const later = { maxSpanSeconds: 3600, nowMs: NOW + 3600_000 }
  assert.equal(validateOverrides({ epoch: '2026-09-30T12:30:00Z' }, ctx).length, 1)
  assert.deepEqual(validateOverrides({ epoch: '2026-09-30T12:30:00Z' }, later), [])
})

test('validateOverrides: whitespace-only values count as not set', () => {
  assert.deepEqual(validateOverrides({ epoch: '   ', max_height: ' ' }, ctx), [])
})
