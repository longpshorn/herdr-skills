import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { docsImpact, formatImpact, impactInputs } from './docs-impact.mjs';

const DOCS = {
  'README.md': '# acme\n',
  'CLAUDE.md': '# rules\n',
  'src/billing/README.md': 'Billing.\n',
  'docs/cache.md': 'Entries expire after `isFresh` says so. See `src/cache.ts`.\n',
  'docs/other.md': 'Nothing relevant; a cache is mentioned in prose only.\n',
};

const reasonsFor = (impact, doc) => impact.find((item) => item.doc === doc)?.reasons ?? [];

test('the summary docs are always listed, first', () => {
  const impact = docsImpact({ changed: ['src/x.ts'], diff: '', docs: DOCS });
  assert.deepEqual(impact.slice(0, 2).map((item) => item.doc), ['README.md', 'CLAUDE.md']);
});

test('the nearest guide above a changed file is listed', () => {
  const impact = docsImpact({ changed: ['src/billing/invoice.ts'], diff: '', docs: DOCS });
  assert.match(reasonsFor(impact, 'src/billing/README.md').join(), /nearest guide to src\/billing\/invoice.ts/);
});

test('a doc naming a changed file or a changed export in a code span is listed; prose mentions are not', () => {
  const diff = '+++ b/src/cache.ts\n-export function isFresh(a, b) {\n+export function isFresh(a, b, c) {\n';
  const impact = docsImpact({ changed: ['src/cache.ts'], diff, docs: DOCS });
  const reasons = reasonsFor(impact, 'docs/cache.md').join('; ');
  assert.match(reasons, /names src\/cache.ts/);
  assert.match(reasons, /names the export isFresh/);
  assert.equal(reasonsFor(impact, 'docs/other.md').length, 0);
});

test('a doc the branch already changed is marked, and the output ends with what to do', () => {
  const impact = docsImpact({ changed: ['README.md'], diff: '', docs: DOCS });
  const text = formatImpact(impact);
  assert.match(text, /README.md \(changed\)/);
  assert.match(text, /Docs reviewed/);
});

test('inputs come from the branch since it left the base, plus uncommitted and untracked files', (t) => {
  const dir = mkdtempSync(path.join(tmpdir(), 'impact-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const git = (...args) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@example.com',
    '-c', 'commit.gpgsign=false', ...args], { cwd: dir, encoding: 'utf8' });
  git('init', '-q', '-b', 'main');
  mkdirSync(path.join(dir, 'src'));
  writeFileSync(path.join(dir, 'src', 'a.ts'), 'export const A = 1;\n');
  git('add', '-A');
  git('commit', '-qm', 'base');
  git('checkout', '-qb', 'feature');
  writeFileSync(path.join(dir, 'src', 'a.ts'), 'export const ALPHA = 1;\n');
  git('commit', '-qam', 'rename');
  writeFileSync(path.join(dir, 'src', 'b.ts'), 'export const BETA = 2;\n');
  const { changed, diff } = impactInputs(dir, 'main');
  assert.deepEqual(changed.sort(), ['src/a.ts', 'src/b.ts']);
  assert.match(diff, /\+export const ALPHA/);
  assert.match(diff, /\+export const BETA/);
});
