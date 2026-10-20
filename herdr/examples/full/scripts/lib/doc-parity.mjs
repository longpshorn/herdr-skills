// Docs must name only what exists: every `npm run <script>` is in package.json, every repo path in a code span and
// every relative link resolves. A failing check means a doc went stale; fix the doc or the name.
import path from 'node:path';

const REPO_PATH = /^[\w.-]+(\/[\w.@-]+)+\/?$/;

export function checkDocParity({ read, list }) {
  const files = list();
  const scripts = JSON.parse(read('package.json') ?? '{}').scripts ?? {};
  const exists = (name) => {
    const clean = name.replace(/\/$/, '');
    return files.some((file) => file === clean || file.startsWith(`${clean}/`));
  };
  const topDirs = new Set(files.filter((file) => file.includes('/')).map((file) => file.split('/')[0]));
  const problems = [];
  for (const doc of files.filter((file) => file.endsWith('.md'))) {
    const text = read(doc) ?? '';
    for (const match of text.matchAll(/\bnpm (?:run ([\w:.-]+)|(test)\b)/g)) {
      if (!((match[1] ?? match[2]) in scripts)) problems.push(`${doc}: \`${match[0]}\` is not a script in package.json`);
    }
    for (const [, span] of text.matchAll(/`([^`\s]+)`/g)) {
      // Only paths into this repo's own folders: home paths, placeholders and globs never match REPO_PATH.
      if (REPO_PATH.test(span) && topDirs.has(span.split('/')[0]) && !exists(span)) {
        problems.push(`${doc}: \`${span}\` does not exist`);
      }
    }
    for (const [, link] of text.matchAll(/\]\(([^)\s]+)\)/g)) {
      if (/^[a-z]+:|^#/.test(link)) continue;
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(doc), link.split('#')[0]));
      if (!target.startsWith('../') && !exists(target)) problems.push(`${doc}: link ${link} does not exist`);
    }
  }
  return problems;
}
