# Minimal example

The least a repo needs for the herdr skills: one file, `.claude/herdr-profile.json`, committed. It names the forge, and adds one note every agent's brief carries. Everything else (worktree command, branch names, how agents plan and open PRs) comes from the defaults in [PROFILES.md](../../PROFILES.md).

## When this is enough

- One person runs a few agents at a time on a small repo.
- The default worktree (`git worktree add` next to the repo) is fine: the app has no dev server, or two of them on one machine never collide.
- Review catches stale docs and duplicated rules, because there are few agents and few files.

Move to the [full example](../full/README.md) when several agents work in parallel: they need their own ports, a hook that stops them handing over red work, one owner per shared rule, and a doc check. Run `/herdr:setup` to see which of those a repo lacks.

## Personal overlay

`overlay.example.json` is a sample for your own `~/.config/herdr/profiles/`, never the repo. It is laid over the repo's profile for the repo its `match` names (`match.remote` is a substring of the `origin` URL). It holds the two fields that belong to one person:

- `planning`: your own planning page, where `/herdr:retire` adds captured follow-ups. Teammates have their own, or none.
- `mirrors`: your copies of repo docs kept outside the repo (notes, a wiki), refreshed after each merge. They are paths on your machine.

Committed to the repo, either would point every teammate's retire at your page and your home folder. Copy the file to `~/.config/herdr/profiles/acme-app.json`, change `match` and the paths, and check the result with the helper's `profile --path "<repo>"` command.
