// Run: node --test --experimental-strip-types src/utils/epoch.test.ts
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  DEFAULT_EPOCH_UTC,
  EPOCH_PRESETS,
  epochPresetFor,
  epochToInputValue,
  formatEpochUtc,
  inputValueToEpoch,
  isValidEpochUtc,
} from './epoch.ts'

const preset = (id: string) => EPOCH_PRESETS.find((p) => p.id === id)!

test('three presets: 1970, 2026 and now', () => {
  assert.deepEqual(EPOCH_PRESETS.map((p) => p.id), ['unix', 'y2026', 'now'])
  assert.equal(preset('unix').value(), '1970-01-01T00:00:00Z')
  assert.equal(preset('y2026').value(), '2026-01-01T00:00:00Z')
  assert.equal(DEFAULT_EPOCH_UTC, '2026-01-01T00:00:00Z')
})

test('"now" is the current wall clock in UTC, truncated to whole seconds', () => {
  assert.equal(preset('now').value(new Date('2026-10-01T09:15:30.987Z')), '2026-10-01T09:15:30Z')
  assert.ok(isValidEpochUtc(preset('now').value()))
})

test('formatEpochUtc drops fractional seconds', () => {
  assert.equal(formatEpochUtc(new Date('2026-01-01T00:00:00.000Z')), '2026-01-01T00:00:00Z')
  assert.equal(formatEpochUtc(new Date('2026-01-01T00:00:00.999Z')), '2026-01-01T00:00:00Z')
})

test('validity is exactly the form serve.py accepts', () => {
  for (const ok of ['1970-01-01T00:00:00Z', '2026-12-31T23:59:59Z']) assert.ok(isValidEpochUtc(ok), ok)
  for (const bad of ['', 'now', '2026-01-01', '2026-01-01T00:00:00', '2026-01-01T00:00:00.000Z',
    '2026-01-01 00:00:00Z', '2026-13-01T00:00:00Z', '2026-02-30T00:00:00Z', '2026-01-01T24:00:00Z']) {
    assert.ok(!isValidEpochUtc(bad), bad)
  }
})

test('a stored value maps back to its preset (now never does)', () => {
  assert.equal(epochPresetFor('1970-01-01T00:00:00Z'), 'unix')
  assert.equal(epochPresetFor('2026-01-01T00:00:00Z'), 'y2026')
  assert.equal(epochPresetFor('2026-10-01T09:15:30Z'), null)
})

test('datetime-local round trip is UTC', () => {
  assert.equal(epochToInputValue('2026-10-01T09:15:30Z'), '2026-10-01T09:15:30')
  assert.equal(inputValueToEpoch('2026-10-01T09:15:30'), '2026-10-01T09:15:30Z')
  // minutes-only values (no seconds) are what some browsers produce
  assert.equal(inputValueToEpoch('2026-10-01T09:15'), '2026-10-01T09:15:00Z')
  assert.equal(epochToInputValue('garbage'), '')
  assert.equal(inputValueToEpoch(''), null)
})
