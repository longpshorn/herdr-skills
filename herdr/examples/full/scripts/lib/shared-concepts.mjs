// Each shared concept (a rule or set of values other code must agree with) has one owner, listed in
// docs/shared-concepts.md. This check fails when the owner is missing, or when another file re-lists its values.
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REGISTRY = 'docs/shared-concepts.md';
const GUARDED = /^src\/.*\.(ts|mts|js|mjs)$/;
const TEST_FILE = /\.test\.[^.]+$/;

const cells = (row) => row.trim().replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map((cell) => cell.trim());
const codeSpans = (cell) => [...cell.matchAll(/`([^`]+)`/g)].map((m) => m[1].replace(/\\\|/g, '|'));

// The table under "## Registry": Concept | Owner (a link, then the exports as code spans) | Rule | Tell-tales.
export function registryEntries(text) {
  const section = text.split(/^## Registry\s*$/m)[1]?.split(/^## /m)[0] ?? '';
  const rows = section.split('\n').filter((line) => line.trim().startsWith('|')).slice(2);
  return rows.map((row) => {
    const [concept, owner, , telltales = ''] = cells(row);
    const link = owner.match(/\]\(([^)]+)\)/)?.[1] ?? '';
    return {
      concept,
      owner: path.posix.normalize(path.posix.join(path.posix.dirname(REGISTRY), link)),
      exports: codeSpans(owner),
      patterns: codeSpans(telltales),
    };
  });
}

function exportsName(source, name) {
  return new RegExp(`export\\s+(?:const|let|var|function\\*?|class|type|interface|enum)\\s+${name}\\b`).test(source)
    || new RegExp(`export\\s*\\{[^}]*\\b${name}\\b`).test(source);
}

export function checkSharedConcepts({ read, list }) {
  const registry = read(REGISTRY);
  if (registry === undefined) return [`${REGISTRY} is missing`];
  const problems = [];
  const sources = list().filter((name) => GUARDED.test(name) && !TEST_FILE.test(name));
  for (const { concept, owner, exports, patterns } of registryEntries(registry)) {
    const ownerSource = read(owner);
    if (ownerSource === undefined) {
      problems.push(`${concept}: owner ${owner} is missing`);
      continue;
    }
    for (const name of exports.filter((name) => !exportsName(ownerSource, name))) {
      problems.push(`${concept}: ${owner} does not export ${name}`);
    }
    // A tell-tale that can't compile, or that matches the empty string (and so every line), is reported instead of run.
    const telltales = patterns.flatMap((pattern) => {
      let regex;
      try {
        regex = new RegExp(pattern);
      } catch (error) {
        problems.push(`${concept}: tell-tale ${pattern} is not a valid regex (${error.message})`);
        return [];
      }
      if (!regex.test('')) return [{ pattern, regex }];
      problems.push(`${concept}: tell-tale ${pattern} matches the empty string, so it would flag every line`);
      return [];
    });
    for (const file of sources.filter((name) => name !== owner)) {
      (read(file) ?? '').split('\n').forEach((line, index) => {
        for (const { pattern } of telltales.filter(({ regex }) => regex.test(line))) {
          problems.push(`${file}:${index + 1} re-lists ${concept} (${pattern}): use ${exports.join(', ')} from ${owner}`);
        }
      });
    }
  }
  return problems;
}

// read and list for the repository this script lives in.
export function repoFiles(root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')) {
  const walk = (dir) => readdirSync(path.join(root, dir), { withFileTypes: true }).flatMap((entry) => {
    const name = dir ? `${dir}/${entry.name}` : entry.name;
    if (entry.isDirectory()) return ['node_modules', '.git'].includes(entry.name) ? [] : walk(name);
    return [name];
  });
  const read = (name) => {
    try {
      return readFileSync(path.join(root, name), 'utf8');
    } catch {
      return undefined;
    }
  };
  return { read, list: () => walk('') };
}
