# Full example: every practice

A small Node sample repo (no dependencies; TypeScript runs through Node's built-in type stripping, so Node 24 or later) that shows each practice that makes parallel agents safe. Each piece is small and tested; `npm test` runs them all, and the herdr-skills CI runs it on Linux, macOS and Windows. Copy the pieces you lack, or let `/herdr:setup` list them and copy them for you.

Use the [minimal example](../minimal/README.md) instead when one person runs a few agents on a small repo.

| Practice | Why | Files |
| - | - | - |
| Profile | Tells the herdr skills the forge, tracker, worktree commands, dev server, and what every brief must say. | `.claude/herdr-profile.json` |
| Worktrees | Each agent gets its own checkout, bootstrapped, with its own port, so parallel agents never share a dev server. `new` is safe to re-run; `remove` refuses uncommitted work and keeps the branch, so `.gitignore` must hide the `.env.local` that bootstrap writes. | `scripts/worktree.mjs`, `scripts/lib/worktree.mjs`, `scripts/bootstrap.mjs`, `.gitignore` |
| Agent rules | The lifecycle every agent follows: test first, never merge, rebase and hand over, run the doc check. | `CLAUDE.md` |
| Stop hook | Runs `npm run check` before an agent may finish with changed files; a failure sends it back with the output. | `.claude/settings.json`, `.claude/hooks/verify-on-stop.mjs` |
| One owner per shared concept | A registry names each shared rule's owner; the check fails when another file re-lists its values. | `docs/shared-concepts.md`, `scripts/lib/shared-concepts.mjs` |
| Docs stay true | Doc parity fails the build when a doc names a script or file that doesn't exist; `npm run docs:impact` lists the docs a change may have made untrue; the template records each one under "Docs reviewed". | `scripts/lib/doc-parity.mjs`, `scripts/docs-impact.mjs`, `scripts/lib/docs-impact.mjs`, `.gitlab/merge_request_templates/Default.md`, `.github/pull_request_template.md` |
| Personal install | `npm run bootstrap` installs the herdr plugin into each developer's own Claude Code settings when it is missing. It keeps an existing `herdr-skills` marketplace (such as a local clone), and never touches the committed `.claude/settings.json`. | `scripts/bootstrap.mjs`, `scripts/lib/herdr-plugin.mjs` |

Each `scripts/lib/*.mjs` has its tests next to it (`*.test.mjs`). Some pieces lean on others (docs-impact reads the file list from `scripts/lib/shared-concepts.mjs`, bootstrap writes `.env.local` through `scripts/lib/worktree.mjs`), so when you copy one by hand, copy what it imports; `/herdr:setup` does that for you. The shared-concepts and doc-parity tests also run against this repo itself, which is what makes `npm run check` fail on drift.

## Commands

```bash
npm run bootstrap                              # .env.local with this checkout's port; the herdr plugin if missing
npm run worktree -- new feat/ACME-123-login    # a bootstrapped worktree next to this checkout
npm run worktree -- remove feat/ACME-123-login
npm run dev                                    # serves on this checkout's port
npm run check                                  # what the Stop hook and CI run
npm run docs:impact                            # docs to reread before handing over
```

## On GitHub

The profile here is for GitLab and Jira. On GitHub, change the profile and keep the template that matches:

```json
{
  "name": "acme-app",
  "forge": "github",
  "tracker": { "kind": "none" },
  "worktree": {
    "new": "npm run worktree -- new {branch}",
    "remove": "npm run worktree -- remove {branch}"
  },
  "dev_server": { "command": "npm run dev", "ready": "listening on" },
  "brief": {
    "mr_create": "gh pr create --fill",
    "notes": ["Done means `npm run check` passes and the PR's \"Docs reviewed\" section lists every doc `npm run docs:impact` printed."]
  }
}
```

- Keep `.github/pull_request_template.md` and delete `.gitlab/`.
- Turn on "Allow squash merging" and branch protection that requires the test workflow. `/herdr:retire` squash-merges a PR pinned to its tested head commit and waits for its checks.
- Use `"tracker": {"kind": "jira", ...}` as above if the tickets live in Jira; the skills don't read GitHub Issues.

## Make it yours

- Replace `ACME`, `acme-app` and `example.com` with your own names.
- Swap `npm` for your package manager in `package.json`, the profile, the hook and `CLAUDE.md`.
- Add your real checks to `npm run check`: lint, type checks, the test suites.
