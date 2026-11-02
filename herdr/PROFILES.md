# Herdr project profiles

The herdr skills run the same flow on every project: spawn a ticket agent in its own worktree, track it, then `/herdr:retire` lands the validated MR, cleans up, fast-forwards main and refreshes the running app. What differs per project comes from its profile, resolved in this order:

1. **The repo's profile:** `.claude/herdr-profile.json` in the repo, read from the main checkout. It holds what everyone on the project shares: forge, tracker, worktree commands, dev server, how agents work and open MRs. It needs no `match`.
2. **A personal overlay:** the first `~/.config/herdr/profiles/*.json` whose `match` fits the repo (a substring of the `origin` URL, or the main checkout's path). Its fields replace the repo's; a field that is an object in both is merged one level deep (`brief.notes` from the overlay replaces the repo's list). Use it for what belongs to one person, such as their planning page and their copies of the repo's docs, or for a repo without a profile file.
3. **The defaults** in the table below.

`/herdr:setup` proposes a repo's profile from [examples/minimal](examples/minimal) or [examples/full](examples/full), which also has a sample personal overlay. A repository that already has its own launcher and merge process can hand those steps over instead: see [examples/repo-process](examples/repo-process). A repo with neither file still works on the defaults. See what a repo resolves to: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools" profile --path "<repo>"` (outside Claude Code, `${CLAUDE_PLUGIN_ROOT}` is the `herdr` folder of your clone; see the README's "Other coding agents"). In its output, `defaulted` lists the fields that came from defaults and `sources` names the two files used.

| Field | Meaning | Default |
| - | - | - |
| `name` | Profile name | none |
| `match.remote` / `match.path` | Overlays only: a substring of the `origin` URL, or the main checkout's path | required in an overlay |
| `forge` | `gitlab` (glab) or `github` (gh): finding, rebasing, approving and squash-merging MRs/PRs | from the `origin` host |
| `tracker` | `{kind: jira, project, parent, done_path, branch_command}`: retire closes the ticket one status at a time and files follow-ups in `project` under `parent`. `branch_command` names the branch at spawn (`{type}` and `{key}` filled in, ticket title on stdin, prints JSON with `.branch`). `{kind: none}` skips the ticket steps | `none` |
| `planning` | `{artifact, local_copy, check, facts, facts_on}`: the planning page retire adds captured follow-ups to, in priority order (`check` runs on the edited copy and on the published page, `{file}` filled in). `facts` names the page's sections that restate the repo; `facts_on` globs the repo files they summarise, and `update-main` lists the changed ones as `facts_changed` so retire refreshes those sections | none |
| `mirrors` | `[{source, copy, from}]`: copies of repo docs kept outside the repo. `sync-mirrors` brings each `copy` up to date with `source` (a path in the main checkout) after a merge, backing up the old copy to `~/.cache/herdr/mirror-backups`; with `from`, the copy keeps its own lines above that line and takes the rest from the source | none |
| `worktree` | `new` / `remove` commands with `{branch}`, and `env` for credentials they need | `git worktree add -b {branch} "../<repo>-<slug>" origin/<default>` / `git worktree remove` |
| `dev_server` | `command` run in the "main dev server" pane and the `ready` regex that says it is serving | none: retire reports what changed instead of restarting |
| `restart_on` | Globs of files a running dev server only reads at startup (no slash or `**/` = any folder) | lockfiles and manifests for JS, Python, Go, Ruby, .NET and Rust, compose files, `.env.example`, tool-version files |
| `brief.implement` | How a spawned agent works the ticket | plan first, stop for approval, then test-first |
| `brief.mr_create` | How it opens the MR | `glab mr create` or `gh pr create` by forge |
| `brief.notes` | Project facts every brief carries (auth, done criteria, merge rules) | none |
| `spawn.command` | The repository's own launcher, for a repo that starts work its own way (it creates the worktree and session, and may claim the ticket). `/herdr:spawn` runs it through `herdr_tools.py repo-launch` instead of creating a worktree and calling `launch`. `{key}` is the ticket or task key, `{branch}` the branch when one was settled; each placeholder fills one argument and is never read as shell. A non-zero exit, a launcher that can't start, or one that runs past 5 minutes is shown as a refusal, never retried or worked around. The key must start with a letter, digit or `#`, so it can't read as an option. On Windows, write paths with forward slashes or quote them: the command is split like a POSIX shell would | none: spawn's own flow |
| `spawn.agent` | Required with `spawn.command`: the name of the agent the launcher starts, with the same placeholders (`session-{key}`). The helper waits for it in `herdr agent list` | none |
| `spawn.brief` | Whether spawn writes and sends a brief. `false` when the launcher brings its own task context; the agent then keeps no status report, and retire does not ask for one | `true` |
| `retire.land` | Who merges: `helper` (retire squash-merges through `land`) or `repo` (the repository's own process merges; `land`, `land` with several agents, and `watch` refuse before any forge call, and retire waits for the MR to be merged). `land --mr` is checked against the repo it runs in, so run it from that repo's checkout; an MR named from elsewhere, or one an agent's report names in another repo, can't be traced to this field. With any agent in a batch (`land a b`, `watch`) whose repo can't be resolved, the batch stops before landing any | `helper` |
| `retire.branch_cleanup` | Who deletes the branch: `helper` (retire's `git branch -D`) or `repo` (`worktree.remove` deletes it too, and a refusal from it is reported, never worked around by removing the worktree by hand) | `helper` |

A personal overlay may add fields but can't loosen the repository's process: an overlay that sets `retire.land` or `retire.branch_cleanup` back to `helper` over a repo file's `repo` is refused. A profile file that is invalid JSON or fails validation is an error everywhere, including `land --mr`; it is never read as "no profile".

Keep only the fields that differ from the defaults. Never put secrets in a profile: `env` values are shell expressions evaluated at run time.
