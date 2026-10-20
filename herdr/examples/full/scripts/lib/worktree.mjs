// One worktree per branch, next to the main checkout, each with its own port and its own bootstrap.
import { execFileSync } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const FIRST_PORT = 3100;
const PORT_RANGE = 800;

// FNV-1a: a small, stable hash, so a checkout keeps its port (and its URL) across restarts.
function hash(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h;
}

export function portFor(dir, env = process.env) {
  if (env.PORT) return Number(env.PORT);
  return FIRST_PORT + (hash(path.resolve(dir)) % PORT_RANGE);
}

export function worktreePath(mainCheckout, branch) {
  return path.join(path.dirname(mainCheckout), `${path.basename(mainCheckout)}-${branch.replace(/[^\w.-]+/g, '-')}`);
}

// Written once; a developer's own edits to .env.local are never overwritten.
export function ensureEnvLocal(dir, env = process.env) {
  const file = path.join(dir, '.env.local');
  if (existsSync(file)) return 'kept';
  writeFileSync(file, `PORT=${portFor(dir, env)}\n`);
  return 'created';
}

const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

function tryGit(cwd, ...args) {
  try {
    return git(cwd, ...args);
  } catch {
    return null;
  }
}

// The main checkout is the first entry of `git worktree list`, so these work from any worktree.
function mainCheckout(cwd) {
  return git(cwd, 'worktree', 'list', '--porcelain').split('\n')[0].replace(/^worktree /, '');
}

function defaultBranch(main) {
  return tryGit(main, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD') || 'origin/main';
}

export function newWorktree(branch, { cwd = process.cwd(), bootstrap }) {
  const main = mainCheckout(cwd);
  const dir = worktreePath(main, branch);
  const created = !existsSync(dir);
  if (created) {
    tryGit(main, 'fetch', '-q', 'origin');
    if (tryGit(main, 'rev-parse', '--verify', '--quiet', `refs/heads/${branch}`)) {
      git(main, 'worktree', 'add', dir, branch);
    } else {
      // --no-track: a bare `git push` from the new branch can never land on the default branch.
      git(main, 'worktree', 'add', '--no-track', '-b', branch, dir, defaultBranch(main));
    }
  } else if (tryGit(dir, 'branch', '--show-current') !== branch) {
    throw new Error(`${dir} exists but is not a worktree on ${branch}; move it aside first.`);
  }
  bootstrap(dir);
  return { path: dir, branch, created, port: portFor(dir) };
}

export function removeWorktree(branch, { cwd = process.cwd() }) {
  const main = mainCheckout(cwd);
  const dir = worktreePath(main, branch);
  if (!existsSync(dir)) return { path: dir, removed: false };
  if (git(dir, 'status', '--porcelain')) {
    throw new Error(`${dir} has uncommitted work; commit or discard it first.`);
  }
  git(main, 'worktree', 'remove', dir); // never --force: that is how work gets lost
  return { path: dir, removed: true };
}
