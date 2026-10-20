// Install the herdr skills into the developer's own Claude Code settings (user scope) when they are missing.
// Never into the project's committed .claude/settings.json: the plugin is each developer's tool, not a dependency.
import { spawnSync } from 'node:child_process';

const MARKETPLACE = 'herdr-skills';
const SOURCE = 'longpshorn/herdr-skills';
const PLUGIN = 'herdr@herdr-skills';

export function ensureHerdrPlugin(claude) {
  if (!claude.available()) return { status: 'ok', detail: 'skipped: the claude CLI is not installed' };
  // Only a user-scope install counts: a project or local one serves a single checkout.
  if (claude.plugins().some(({ id, scope }) => id === PLUGIN && scope === 'user')) {
    return { status: 'ok', detail: `${PLUGIN} already installed` };
  }
  try {
    // Keep any marketplace already registered under this name, such as a local clone someone works on.
    if (!claude.marketplaces().some(({ name }) => name === MARKETPLACE)) claude.addMarketplace(SOURCE);
    claude.install(PLUGIN);
  } catch (error) {
    return { status: 'warn', detail: `could not install ${PLUGIN} (${error.message}); run: claude plugin marketplace ` +
      `add ${SOURCE} --scope user && claude plugin install ${PLUGIN} --scope user` };
  }
  return { status: 'ok', detail: `installed ${PLUGIN} at user scope` };
}

function claudeCli(...args) {
  // shell on Windows, where an npm-installed claude is a .cmd file
  const proc = spawnSync('claude', args, { encoding: 'utf8', shell: process.platform === 'win32' });
  if (proc.error || proc.status !== 0) throw new Error((proc.stderr || proc.error?.message || '').trim());
  return proc.stdout;
}

export const realClaude = {
  available: () => { try { claudeCli('--version'); return true; } catch { return false; } },
  plugins: () => JSON.parse(claudeCli('plugin', 'list', '--json')),
  marketplaces: () => JSON.parse(claudeCli('plugin', 'marketplace', 'list', '--json')),
  addMarketplace: (source) => claudeCli('plugin', 'marketplace', 'add', source, '--scope', 'user'),
  install: (id) => claudeCli('plugin', 'install', id, '--scope', 'user'),
};
