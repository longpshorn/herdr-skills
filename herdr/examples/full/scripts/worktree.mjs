#!/usr/bin/env node
// npm run worktree -- new <branch>     a bootstrapped worktree next to the main checkout, with its own port
// npm run worktree -- remove <branch>  remove it (refuses uncommitted work; keeps the branch)
import { execFileSync } from 'node:child_process';
import { newWorktree, removeWorktree } from './lib/worktree.mjs';

const [action, branch] = process.argv.slice(2);
const bootstrap = (cwd) => execFileSync(process.execPath, ['scripts/bootstrap.mjs'], { cwd, stdio: 'inherit' });

try {
  if (action === 'new' && branch) {
    const wt = newWorktree(branch, { bootstrap });
    console.log(`${wt.created ? 'Created' : 'Reused'} ${wt.path} on ${branch}; its dev server uses port ${wt.port}.`);
  } else if (action === 'remove' && branch) {
    const wt = removeWorktree(branch, {});
    console.log(wt.removed ? `Removed ${wt.path}; the branch ${branch} is kept.` : `${wt.path} was already removed.`);
  } else {
    console.error('usage: npm run worktree -- new|remove <branch>');
    process.exit(2);
  }
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
