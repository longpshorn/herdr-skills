import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isFresh } from './cache.ts';
import { DURATIONS } from './durations.ts';

test('an entry is fresh until the cache TTL has passed', () => {
  assert.equal(isFresh(1_000, 1_000 + DURATIONS.cacheTtlMs - 1), true);
  assert.equal(isFresh(1_000, 1_000 + DURATIONS.cacheTtlMs), false);
});
