import { test } from 'node:test';
import assert from 'node:assert/strict';
import { checkDocParity } from './doc-parity.mjs';
import { repoFiles } from './shared-concepts.mjs';

function repo(files) {
  return { read: (name) => files[name], list: () => Object.keys(files) };
}

const PACKAGE = JSON.stringify({ scripts: { test: 'node --test', check: 'node --test', 'docs:impact': 'x' } });

test('docs that name real scripts and files have no problems', () => {
  assert.deepEqual(checkDocParity(repo({
    'package.json': PACKAGE,
    'src/cache.ts': '',
    'README.md': 'Run `npm test`, `npm run check` and `npm run docs:impact`. See `src/cache.ts` and [the cache](src/cache.ts).',
  })), []);
});

test('a script the docs name but package.json lacks is reported', () => {
  assert.deepEqual(checkDocParity(repo({ 'package.json': PACKAGE, 'AGENTS.md': 'Run `npm run lint` first.\n' })),
    ['AGENTS.md: `npm run lint` is not a script in package.json']);
});

test('a repo path or relative link that no longer exists is reported', () => {
  const problems = checkDocParity(repo({
    'package.json': PACKAGE,
    'src/cache.ts': '',
    'docs/guide.md': 'The cache lives in `src/old-cache.ts`; see [its test](../src/cache.test.ts).\n',
  }));
  assert.deepEqual(problems, [
    'docs/guide.md: `src/old-cache.ts` does not exist',
    'docs/guide.md: link ../src/cache.test.ts does not exist',
  ]);
});

test('paths outside the repo, placeholders and URLs are not checked', () => {
  assert.deepEqual(checkDocParity(repo({
    'package.json': PACKAGE,
    'src/a.ts': '',
    'README.md': '`~/.config/herdr/profiles/` `src/<feature>/index.ts` `src/*.ts` `/tmp/x` `lib/x.ts` ' +
      '[site](https://example.com/src/x) [section](#setup) [sibling](../other-repo/README.md)\n',
  })), []);
});

test('this repository\'s docs name only scripts and files that exist', () => {
  assert.deepEqual(checkDocParity(repoFiles()), []);
});
