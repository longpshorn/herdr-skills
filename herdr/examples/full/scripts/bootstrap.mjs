#!/usr/bin/env node
// npm run bootstrap: make this checkout ready to work in. Every step is safe to repeat.
import { ensureHerdrPlugin, realClaude } from './lib/herdr-plugin.mjs';
import { ensureEnvLocal } from './lib/worktree.mjs';

const steps = [
  ['.env.local', () => ({ status: 'ok', detail: ensureEnvLocal(process.cwd()) })],
  // A real project installs its dependencies here (this sample has none).
  ['herdr plugin', () => (process.env.CI ? { status: 'ok', detail: 'skipped in CI' } : ensureHerdrPlugin(realClaude))],
];

for (const [name, step] of steps) {
  const { status, detail } = step();
  console.log(`${status === 'ok' ? 'ok  ' : 'warn'} ${name}: ${detail}`);
}
