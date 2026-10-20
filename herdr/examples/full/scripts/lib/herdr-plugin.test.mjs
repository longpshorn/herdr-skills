import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ensureHerdrPlugin } from './herdr-plugin.mjs';

// A fake `claude` CLI that records the calls bootstrap makes.
function machine({ available = true, plugins = [], marketplaces = [], fail = '' } = {}) {
  const calls = [];
  return {
    calls,
    claude: {
      available: () => available,
      plugins: () => plugins,
      marketplaces: () => marketplaces,
      addMarketplace: (source) => {
        calls.push(`marketplace add ${source}`);
        if (fail === 'add') throw new Error('network down');
      },
      install: (id) => {
        calls.push(`install ${id}`);
        if (fail === 'install') throw new Error('network down');
      },
    },
  };
}

test('adds the marketplace and installs the plugin at user scope when both are missing', () => {
  const m = machine();
  assert.equal(ensureHerdrPlugin(m.claude).status, 'ok');
  assert.deepEqual(m.calls, ['marketplace add longpshorn/herdr-skills', 'install herdr@herdr-skills']);
});

test('does nothing when the plugin is installed at user scope', () => {
  const m = machine({ plugins: [{ id: 'herdr@herdr-skills', scope: 'user' }] });
  assert.match(ensureHerdrPlugin(m.claude).detail, /already installed/);
  assert.deepEqual(m.calls, []);
});

test('keeps an existing marketplace of the same name, such as a local clone someone edits', () => {
  const m = machine({ marketplaces: [{ name: 'herdr-skills', source: 'directory', path: '/src/herdr-skills' }] });
  ensureHerdrPlugin(m.claude);
  assert.deepEqual(m.calls, ['install herdr@herdr-skills']);
});

test('a project- or local-scope install does not count: the plugin belongs in personal settings', () => {
  const m = machine({ plugins: [{ id: 'herdr@herdr-skills', scope: 'project' }],
    marketplaces: [{ name: 'herdr-skills' }] });
  ensureHerdrPlugin(m.claude);
  assert.deepEqual(m.calls, ['install herdr@herdr-skills']);
});

test('skips without the claude CLI, and never stops setup on a failure', () => {
  assert.match(ensureHerdrPlugin(machine({ available: false }).claude).detail, /claude CLI is not installed/);
  for (const fail of ['add', 'install']) {
    const result = ensureHerdrPlugin(machine({ fail }).claude);
    assert.equal(result.status, 'warn');
    assert.match(result.detail, /claude plugin/);
  }
});
