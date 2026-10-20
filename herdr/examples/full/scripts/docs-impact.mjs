#!/usr/bin/env node
// npm run docs:impact [-- <base>]: the docs this branch may have made untrue, since it left <base> (origin/main).
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { docsImpact, formatImpact, impactInputs } from './lib/docs-impact.mjs';
import { repoFiles } from './lib/shared-concepts.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { read, list } = repoFiles(root);
const docs = Object.fromEntries(list().filter((name) => name.endsWith('.md')).map((name) => [name, read(name)]));
const { changed, diff } = impactInputs(root, process.argv[2] ?? 'origin/main');
console.log(formatImpact(docsImpact({ changed, diff, docs })));
