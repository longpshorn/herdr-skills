import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { ensureEnvLocal, newWorktree, portFor, removeWorktree, worktreePath } from './worktree.mjs';

test('each checkout gets its own stable port, and PORT overrides it', () => {
  const a = portFor('/code/acme-app-feat-ACME-1-login', {});
  assert.equal(portFor('/code/acme-app-feat-ACME-1-login', {}), a);
  assert.notEqual(portFor('/code/acme-app-fix-ACME-2-crash', {}), a);
  assert.ok(a >= 3100 && a < 3900, a);
  assert.equal(portFor('/anywhere', { PORT: '4321' }), 4321);
});

test('a worktree sits next to the main checkout, named after its branch', () => {
  assert.equal(worktreePath('/code/acme-app', 'feat/ACME-1-login'), path.join('/code', 'acme-app-feat-ACME-1-login'));
});

test('.env.local gets the port once and is never overwritten', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'wt-'));
  try {
    assert.equal(ensureEnvLocal(dir, {}), 'created');
    const port = portFor(dir, {});
    assert.equal(readFileSync(path.join(dir, '.env.local'), 'utf8'), `PORT=${port}\n`);
    writeFileSync(path.join(dir, '.env.local'), 'PORT=1234\n');
    assert.equal(ensureEnvLocal(dir, {}), 'kept');
    assert.equal(readFileSync(path.join(dir, '.env.local'), 'utf8'), 'PORT=1234\n');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// The rest runs real git in a temporary main checkout cloned from a bare "origin".
function sandbox(t) {
  const dir = realpathSync(mkdtempSync(path.join(tmpdir(), 'wt-')));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const git = (cwd, ...args) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@example.com',
    '-c', 'commit.gpgsign=false', ...args], { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git(dir, 'init', '-q', '--bare', '-b', 'main', 'origin.git');
  git(dir, 'clone', '-q', path.join(dir, 'origin.git'), 'acme-app');
  const main = path.join(dir, 'acme-app');
  writeFileSync(path.join(main, 'README.md'), '# acme\n');
  git(main, 'add', '-A');
  git(main, 'commit', '-qm', 'init');
  git(main, 'push', '-q', 'origin', 'main');
  const bootstraps = [];
  const bootstrap = (cwd) => bootstraps.push(cwd);
  return { dir, main, git, bootstraps, bootstrap };
}

test('new creates a bootstrapped worktree on a new branch from origin/main, untracked', (t) => {
  const { main, git, bootstraps, bootstrap } = sandbox(t);
  const result = newWorktree('feat/ACME-1-login', { cwd: main, bootstrap });
  assert.equal(result.path, worktreePath(main, 'feat/ACME-1-login'));
  assert.equal(result.created, true);
  assert.equal(git(result.path, 'branch', '--show-current'), 'feat/ACME-1-login');
  assert.equal(git(result.path, 'for-each-ref', '--format=%(upstream)', 'refs/heads/feat/ACME-1-login'), '');
  assert.deepEqual(bootstraps, [result.path]);
});

test('new is idempotent: a second run reuses the worktree and bootstraps it again', (t) => {
  const { main, bootstraps, bootstrap } = sandbox(t);
  const first = newWorktree('feat/ACME-1-login', { cwd: main, bootstrap });
  const second = newWorktree('feat/ACME-1-login', { cwd: main, bootstrap });
  assert.equal(second.path, first.path);
  assert.equal(second.created, false);
  assert.deepEqual(bootstraps, [first.path, first.path]);
});

test('new works from inside another worktree too', (t) => {
  const { main, bootstrap } = sandbox(t);
  const first = newWorktree('feat/ACME-1-login', { cwd: main, bootstrap });
  const second = newWorktree('fix/ACME-2-crash', { cwd: first.path, bootstrap });
  assert.equal(second.path, worktreePath(main, 'fix/ACME-2-crash'));
});

test('remove refuses uncommitted work, then removes the worktree and keeps the branch', (t) => {
  const { main, git, bootstrap } = sandbox(t);
  const { path: wt } = newWorktree('feat/ACME-1-login', { cwd: main, bootstrap });
  writeFileSync(path.join(wt, 'draft.txt'), 'unsaved\n');
  assert.throws(() => removeWorktree('feat/ACME-1-login', { cwd: main }), /uncommitted/);
  rmSync(path.join(wt, 'draft.txt'));
  assert.equal(removeWorktree('feat/ACME-1-login', { cwd: main }).removed, true);
  assert.equal(existsSync(wt), false);
  assert.match(git(main, 'branch', '--list', 'feat/ACME-1-login'), /feat\/ACME-1-login/);
  assert.equal(removeWorktree('feat/ACME-1-login', { cwd: main }).removed, false);
});
