<!--
Title: `<type>: <KEY> <description>`. It becomes the single squashed commit (squash merge).
Before handing over: rebase on the default branch, green checks, every review comment answered. Agents never merge.
-->

## What and why

<What changed and why. Link the ticket: ACME-123.>

## Verification

Mark each item Verified (you ran it) or Unverified (it still needs checking, and by whom).

- [ ] `npm run check` passes
- [ ] Tried the change in the running app (`npm run dev`)
- [ ] Rebased on the default branch; checks green

## Docs reviewed

<!--
Run `npm run docs:impact` and list every doc it prints, marked "updated" or "no change needed".
A stale doc is worse than none: agents follow it.
-->

| Doc | Result |
| - | - |
