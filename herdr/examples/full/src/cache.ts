import { DURATIONS } from './durations.ts';

export function isFresh(storedAt: number, now: number): boolean {
  return now - storedAt < DURATIONS.cacheTtlMs;
}
