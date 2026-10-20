import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { verify } from '../../.claude/hooks/verify-on-stop.mjs';

const HOOK = fileURLToPath(new URL('../../.claude/hooks/verify-on-stop.mjs', import.meta.url));

test('a repeat stop (stop_hook_active) or a clean tree lets the agent finish without running checks', () => {
  const runs = [];
  const run = () => { runs.push('check'); return { ok: true, output: '' }; };
  assert.equal(verify({ payload: { stop_hook_active: true }, dirty: true, run }).code, 0);
  assert.equal(verify({ payload: {}, dirty: false, run }).code, 0);
  assert.deepEqual(runs, []);
});

test('changed files run the checks; a failure blocks the stop and hands the output back', () => {
  const output = Array.from({ length: 200 }, (_, i) => `line ${i}`).join('\n');
  const failed = verify({ payload: {}, dirty: true, run: () => ({ ok: false, output }) });
  assert.equal(failed.code, 2);
  assert.match(failed.message, /npm run check/);
  assert.match(failed.message, /line 199/);
  assert.doesNotMatch(failed.message, /line 0\n/);
  assert.equal(verify({ payload: {}, dirty: true, run: () => ({ ok: true, output: '' }) }).code, 0);
});

// The real hook, as Claude Code runs it: JSON on stdin, in a project whose `npm run check` passes or fails.
function project(t, checkExitCode) {
  const dir = mkdtempSync(path.join(tmpdir(), 'stop-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  mkdirSync(path.join(dir, '.claude', 'hooks'), { recursive: true });
  writeFileSync(path.join(dir, 'package.json'), JSON.stringify({
    scripts: { check: `node -e "console.log('checked'); process.exit(${checkExitCode})"` } }));
  execFileSync('git', ['init', '-q'], { cwd: dir });
  return dir;
}

function stop(dir, payload = {}) {
  return spawnSync(process.execPath, [HOOK], { input: JSON.stringify(payload), encoding: 'utf8',
    env: { ...process.env, CLAUDE_PROJECT_DIR: dir } });
}

test('the hook blocks a stop when the project check fails', (t) => {
  const result = stop(project(t, 1));
  assert.equal(result.status, 2, result.stderr);
  assert.match(result.stderr, /checked/);
});

test('the hook lets a stop through when the check passes, or on a repeat stop', (t) => {
  const dir = project(t, 0);
  assert.equal(stop(dir).status, 0);
  assert.equal(stop(project(t, 1), { stop_hook_active: true }).status, 0);
});
