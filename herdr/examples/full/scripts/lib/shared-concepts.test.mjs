import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkSharedConcepts, registryEntries, repoFiles } from './shared-concepts.mjs';

const REGISTRY = `# Shared concepts

## Registry

| Concept | Owner | Rule | Tell-tales |
| - | - | - | - |
| Durations | [src/durations.ts](../src/durations.ts) \`DURATIONS\` | Timeouts come from here. | \`\\b(ttlMs\\|timeoutMs):\\s*[1-9]\` |
`;

function files(extra = {}) {
  return {
    'docs/shared-concepts.md': REGISTRY,
    'src/durations.ts': 'export const DURATIONS = { ttlMs: 60_000 } as const;\n',
    'src/cache.ts': "import { DURATIONS } from './durations.ts';\nexport const ttl = DURATIONS.ttlMs;\n",
    ...extra,
  };
}

const check = (all) => checkSharedConcepts({ read: (name) => all[name], list: () => Object.keys(all) });

test('reads each concept: its owner file, exports and tell-tale patterns (with escaped pipes)', () => {
  assert.deepEqual(registryEntries(REGISTRY), [{
    concept: 'Durations', owner: 'src/durations.ts', exports: ['DURATIONS'],
    patterns: ['\\b(ttlMs|timeoutMs):\\s*[1-9]'],
  }]);
});

test('a repo that uses the owner has no problems', () => {
  assert.deepEqual(check(files()), []);
});

test('a file that re-lists the values is reported with the owner to use instead', () => {
  const problems = check(files({ 'src/session.ts': 'const config = {\n  timeoutMs: 30000,\n};\n' }));
  assert.deepEqual(problems, ['src/session.ts:2 re-lists Durations (\\b(ttlMs|timeoutMs):\\s*[1-9]): ' +
    'use DURATIONS from src/durations.ts']);
});

test('the owner itself and tests may state the values', () => {
  assert.deepEqual(check(files({ 'src/cache.test.ts': 'const ttlMs: 5 = 5;\nconst x = { ttlMs: 5 };\n' })), []);
});

test('a missing owner file or export is reported', () => {
  assert.match(check(files({ 'src/durations.ts': undefined })).join('\n'), /owner src\/durations.ts is missing/);
  assert.match(check(files({ 'src/durations.ts': 'export const OTHER = 1;\n' })).join('\n'),
    /src\/durations.ts does not export DURATIONS/);
});

const withTelltales = (cell) => files({ 'docs/shared-concepts.md': REGISTRY.replace(/\| `\\b.*` \|$/m, `| ${cell} |`) });

test('a tell-tale that matches the empty string (an escaped || in a table cell) is reported, not run', () => {
  assert.deepEqual(registryEntries(withTelltales('`ttlMs\\|\\|timeoutMs`')['docs/shared-concepts.md'])[0].patterns,
    ['ttlMs||timeoutMs']);
  assert.deepEqual(check(withTelltales('`ttlMs\\|\\|timeoutMs`')),
    ['Durations: tell-tale ttlMs||timeoutMs matches the empty string, so it would flag every line']);
});

test('a tell-tale that is not a valid regex is reported, not thrown', () => {
  const problems = check(withTelltales('`(ttlMs`'));
  assert.equal(problems.length, 1);
  assert.match(problems[0], /^Durations: tell-tale \(ttlMs is not a valid regex \(.+\)$/);
});

test('the concept\'s other tell-tales still run when one is rejected', () => {
  const problems = check({ ...withTelltales('`(ttlMs` `timeoutMs:\\s*[1-9]`'), 'src/session.ts': 'const c = { timeoutMs: 5 };\n' });
  assert.equal(problems.length, 2);
  assert.equal(problems[1], 'src/session.ts:1 re-lists Durations (timeoutMs:\\s*[1-9]): use DURATIONS from src/durations.ts');
});

test('a repo without the registry is told where it goes', () => {
  assert.deepEqual(check({}), ['docs/shared-concepts.md is missing']);
});

test('this repository has no shared-concept drift', () => {
  assert.deepEqual(checkSharedConcepts(repoFiles()), []);
});
