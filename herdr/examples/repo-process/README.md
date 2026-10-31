# Example: a repository with its own process

Some repositories already have a way to start work and a way to land it. For example:
- a launcher that creates the worktree, opens the session and claims the ticket itself;
- a merge that has to pass the project's own review gate, which the forge's checks can't express;
- a cleanup script that removes the worktree and deletes the branch only after checking it landed.

For such a repository, the herdr skills should hand those steps over instead of running their own. This profile shows how:

| Field | Effect |
| - | - |
| `spawn.command` | `/herdr:spawn` runs this launcher (`herdr_tools.py repo-launch`) instead of creating a worktree and starting the agent itself. `{key}` is the ticket or task key, and `{branch}` the branch, when the skill settled one. Each placeholder fills one argument and is never read as shell. |
| `spawn.agent` | The name of the agent the launcher starts. The helper waits for it to appear in `herdr agent list` and reports its workspace. |
| `spawn.brief` | `false`: the launcher brings its own task context, so no brief is written or sent. With no brief the agent keeps no status report, so retire doesn't ask for one. |
| `retire.land` | `repo`: the repository's own process merges. `herdr_tools.py land`, `land` with several agents, and `watch` refuse before calling the forge. `/herdr:retire` waits until the MR is merged, then cleans up. |
| `retire.branch_cleanup` | `repo`: `worktree.remove` also deletes the branch, so retire runs no `git branch -D`. A refusal from that command is reported, and retire never falls back to removing the worktree by hand. |

Leave all of these out and the default flow runs as before.

Anything personal to one person, such as a value only their machine knows, goes in an overlay in `~/.config/herdr/profiles/` (see [`../minimal/overlay.example.json`](../minimal/overlay.example.json)), not in the repository's file.
