# acme-app

A small sample app. `npm run dev` serves it on this checkout's own port; `npm run check` is what must pass before any handover.

## Development lifecycle (agents)

Every agent, whether spawned by `/herdr:spawn` or started by hand, works the same way.

**Start**
- Work in your own worktree: `npm run worktree -- new <type>/<KEY>-<desc>` creates it next to the main checkout, bootstrapped, with its own port in `.env.local`. Never work in the main checkout.
- Read this file, then the docs your ticket touches.
- Look for an existing owner before you define anything shared: `docs/shared-concepts.md`.

**Build**
- Test first: write a failing test that states the behaviour, make it pass, then tidy.
- Get it working before you abstract it: inline first, extract on the second use.
- The exception is a shared concept (a rule or set of values other code must agree on). Import it from its owner in `docs/shared-concepts.md`, or give a new one an owner and a registry row from the start. Never re-list its values, not even in a comment.

**Before you hand over**
- `npm run check` passes. In Claude Code, the Stop hook runs it when you finish with changed files and sends you back if it fails; other coding agents have no such hook here, so run it yourself.
- `npm run docs:impact` lists the docs your change may have made untrue. Reread each one, and record it in the MR's (or PR's) "Docs reviewed" section as updated or no change needed.

**Hand over**
- Rebase on the default branch, push, wait for a green pipeline, and answer every review thread.
- Never merge. A person validates the change and lands it (`/herdr:retire`), then fast-forwards the main checkout.
- Keep your status report true at every stop; the person landing your work reads it.
