# Shared concepts

A shared concept is a rule or set of values that more than one part of the code must agree on: durations, limits, status names, role lists, feature flags. Each one has exactly one owner, an export in one file. Everything else imports it. Never re-list its values, even in a comment or a brief: agents working in parallel worktrees can't see each other, and each one faithfully builds its own copy.

`npm test` runs `scripts/lib/shared-concepts.mjs` against this table. It fails when an owner file or export is missing, when a tell-tale is not a valid regex or matches the empty string (it would flag every line), or when a file under `src/` matches a concept's tell-tales (the owner and test files are exempt).

## Registry

| Concept | Owner | Rule | Tell-tales |
| - | - | - | - |
| Durations | [src/durations.ts](../src/durations.ts) `DURATIONS` | Every timeout, TTL and retry delay comes from `DURATIONS`. | `([tT]tl\|[tT]imeout\|[dD]elay)Ms:\s*[1-9]` |

## Adding a concept

1. Define it once, as an export in the lowest module that needs it.
2. Add a row: the owner link and export as code spans, a one-sentence rule, and one regex per code span that matches a hard-coded copy. Inside the table, write `\|` for a regex alternation and `\\|` for a literal pipe: `a\|\|b` becomes `a||b`, which matches every line.
3. Replace the copies with imports, and run `npm test`.
