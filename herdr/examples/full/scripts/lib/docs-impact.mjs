// Which docs a change may have made untrue. It decides nothing: it lists docs to reread, each with its reasons, and
// the agent records each one in the MR's "Docs reviewed" section as updated or no change needed.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

// AGENTS.md holds the agent rules; CLAUDE.md is a one-line import of it, so it is never a doc to reread.
const SUMMARY_DOCS = ['README.md', 'AGENTS.md'];
const GUIDES = ['README.md', 'AGENTS.md'];
const GENERIC_NAMES = new Set(['index.ts', 'index.js', 'index.mjs', 'package.json', 'README.md', 'AGENTS.md',
  'CLAUDE.md']);
const EXPORT = /^[+-]\s*export\s+(?:async\s+)?(?:const|let|var|function\*?|class|type|interface|enum)\s+(\w{3,})/gm;

const codeSpans = (text) => [...text.matchAll(/`([^`]+)`|\]\(([^)]+)\)/g)].map((m) => m[1] ?? m[2]);

export function docsImpact({ changed, diff, docs }) {
  const reasons = new Map(Object.keys(docs).map((doc) => [doc, []]));
  const add = (doc, reason) => reasons.get(doc)?.includes(reason) || reasons.get(doc)?.push(reason);
  for (const doc of SUMMARY_DOCS) add(doc, 'summary doc');
  for (const file of changed) {
    for (let dir = path.posix.dirname(file); dir !== '.'; dir = path.posix.dirname(dir)) {
      const guide = GUIDES.map((name) => `${dir}/${name}`).find((name) => name in docs);
      if (guide) {
        add(guide, `nearest guide to ${file}`);
        break;
      }
    }
  }
  const exportsChanged = new Set([...diff.matchAll(EXPORT)].map((m) => m[1]));
  for (const [doc, text] of Object.entries(docs)) {
    const spans = codeSpans(text);
    for (const file of changed.filter((name) => name !== doc)) {
      const base = path.posix.basename(file);
      if (spans.some((span) => span.includes(file) || (!GENERIC_NAMES.has(base) && span.endsWith(base)))) {
        add(doc, `names ${file}`);
      }
    }
    for (const name of exportsChanged) {
      if (spans.some((span) => new RegExp(`\\b${name}\\b`).test(span))) add(doc, `names the export ${name}`);
    }
  }
  const order = (doc) => (SUMMARY_DOCS.includes(doc) ? SUMMARY_DOCS.indexOf(doc) : SUMMARY_DOCS.length);
  return [...reasons]
    .filter(([, why]) => why.length)
    .map(([doc, why]) => ({ doc, changed: changed.includes(doc), reasons: why }))
    .sort((a, b) => order(a.doc) - order(b.doc) || a.doc.localeCompare(b.doc));
}

export function formatImpact(impact) {
  const lines = impact.map(({ doc, changed, reasons }) => `${doc}${changed ? ' (changed)' : ''}: ${reasons.join('; ')}`);
  return [...lines, '',
    "Reread each one. In the MR's \"Docs reviewed\" section, mark it updated or no change needed.",
    "A rule that says 'always' or 'only' is the likeliest to be wrong now."].join('\n');
}

// What the branch changed since it left BASE, including uncommitted and untracked files (as added lines).
export function impactInputs(root, base) {
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  const fork = git('merge-base', base, 'HEAD').trim();
  const untracked = git('ls-files', '--others', '--exclude-standard').split('\n').filter(Boolean);
  const changed = [...git('diff', '--name-only', fork).split('\n').filter(Boolean), ...untracked];
  const added = untracked.map((file) => `+++ b/${file}\n` +
    readFileSync(path.join(root, file), 'utf8').split('\n').map((line) => `+${line}`).join('\n'));
  return { changed: [...new Set(changed)], diff: [git('diff', fork), ...added].join('\n') };
}
