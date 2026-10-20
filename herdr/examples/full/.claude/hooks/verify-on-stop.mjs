#!/usr/bin/env node
// Claude Code Stop hook: before an agent finishes with changed files, run `npm run check`. If it fails, exit 2 so the
// agent gets the output and keeps working. Written in Node so it runs the same on macOS, Linux and Windows.
import { execFileSync, spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const TAIL_LINES = 150;

export function verify({ payload, dirty, run }) {
  // Claude Code sets stop_hook_active when the agent is already continuing because of this hook: never loop.
  if (payload.stop_hook_active || !dirty) return { code: 0 };
  const { ok, output } = run();
  if (ok) return { code: 0 };
  const tail = output.trimEnd().split('\n').slice(-TAIL_LINES).join('\n');
  return { code: 2, message: `verify-on-stop: \`npm run check\` failed. Fix it before you finish.\n${tail}` };
}

function main() {
  const root = process.env.CLAUDE_PROJECT_DIR || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
  let payload = {};
  try {
    payload = JSON.parse(readFileSync(0, 'utf8') || '{}');
  } catch {
    // no payload: treat it as a first stop
  }
  const dirty = execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim() !== '';
  const run = () => {
    const proc = spawnSync('npm', ['run', 'check'], { cwd: root, encoding: 'utf8', shell: process.platform === 'win32' });
    return { ok: proc.status === 0, output: `${proc.stdout ?? ''}${proc.stderr ?? ''}` };
  };
  const { code, message } = verify({ payload, dirty, run });
  if (message) process.stderr.write(`${message}\n`);
  process.exit(code);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
