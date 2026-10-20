# Herdr project profiles

The herdr skills run the same flow on every project: spawn a ticket agent in its own worktree, track it, then `/herdr:retire` lands the validated MR, cleans up, fast-forwards main and refreshes the running app. What differs per project comes from its profile, resolved in this order:

1. **The repo's profile:** `.claude/herdr-profile.json` in the repo, read from the main checkout. It holds what everyone on the project shares: forge, tracker, worktree commands, dev server, how agents work and open MRs. It needs no `match`.
2. **A personal overlay:** the first `~/.config/herdr/profiles/*.json` whose `match` fits the repo (a substring of the `origin` URL, or the main checkout's path). Its fields replace the repo's; a field that is an object in both is merged one level deep (`brief.notes` from the overlay replaces the repo's list). Use it for what belongs to one person, such as their planning page and their copies of the repo's docs, or for a repo without a profile file.
3. **The defaults** in the table below.

A repo with neither file still works on the defaults. See what a repo resolves to: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools" profile --path <repo>`. In its output, `defaulted` lists the fields that came from defaults and `sources` names the two files used.

| Field | Meaning | Default |
| - | - | - |
| `name` | Profile name | none |
| `match.remote` / `match.path` | Overlays only: a substring of the `origin` URL, or the main checkout's path | required in an overlay |
| `forge` | `gitlab` (glab) or `github` (gh): finding, rebasing, approving and squash-merging MRs/PRs | from the `origin` host |
| `tracker` | `{kind: jira, project, parent, done_path, branch_command}`: retire closes the ticket one status at a time and files follow-ups in `project` under `parent`. `branch_command` names the branch at spawn (`{type}` and `{key}` filled in, ticket title on stdin, prints JSON with `.branch`). `{kind: none}` skips the ticket steps | `none` |
| `planning` | `{artifact, local_copy, check, facts, facts_on}`: the planning page retire adds captured follow-ups to, in priority order (`check` runs on the edited copy and on the published page, `{file}` filled in). `facts` names the page's sections that restate the repo; `facts_on` globs the repo files they summarise, and `update-main` lists the changed ones as `facts_changed` so retire refreshes those sections | none |
| `mirrors` | `[{source, copy, from}]`: copies of repo docs kept outside the repo. `sync-mirrors` brings each `copy` up to date with `source` (a path in the main checkout) after a merge, backing up the old copy to `~/.cache/herdr/mirror-backups`; with `from`, the copy keeps its own lines above that line and takes the rest from the source | none |
| `worktree` | `new` / `remove` commands with `{branch}`, and `env` for credentials they need | `git worktree add -b {branch} ../<repo>-<slug> origin/<default>` / `git worktree remove` |
| `dev_server` | `command` run in the "main dev server" pane and the `ready` regex that says it is serving | none: retire reports what changed instead of restarting |
| `restart_on` | Globs of files a running dev server only reads at startup (no slash or `**/` = any folder) | lockfiles and manifests for JS, Python, Go, Ruby, .NET and Rust, compose files, `.env.example`, tool-version files |
| `brief.implement` | How a spawned agent works the ticket | plan first, stop for approval, then test-first |
| `brief.mr_create` | How it opens the MR | `glab mr create` or `gh pr create` by forge |
| `brief.notes` | Project facts every brief carries (auth, done criteria, merge rules) | none |

Keep only the fields that differ from the defaults. Never put secrets in a profile: `env` values are shell expressions evaluated at run time.
