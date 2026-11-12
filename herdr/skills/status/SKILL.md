---
name: status
description: Every Herdr agent at a glance — which are blocked, done, working or idle, in which workspace, doing what. Use for "/herdr:status", "what are my agents doing", "is anything waiting on me", "check the sessions". Read-only.
---

# Status — every agent at a glance

Helper: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools"`, written `herdr_tools.py` below; it runs the helper with the first Python 3.9+ it finds (`python3`, `python` or `py -3`). It refuses to run outside a Herdr pane (`HERDR_ENV` unset); if so, say you are not inside Herdr and stop.

1. Run `herdr_tools.py status`. Rows are sorted blocked, done, working, idle. `AGENT` is the target to use with `/herdr:ask` and `/herdr:peek` (a name, or a pane id for unnamed agents). A child agent launched with `--parent` (or adopted) is indented under its parent; its target is the name without the indent. A child whose parent is no longer running is titled `orphan of <parent>:`: say so, since no one is answering its gates. `herdr_tools.py children <parent>` shows one parent's children with their worktrees and status reports.
2. Show the table. Then, for each **blocked** agent, run `/herdr:peek` on it and add one line saying what it is waiting for (a plan gate, a question, a permission or trust prompt). For each **done** agent, say it finished unseen work.
3. Never answer a blocked agent's prompt here. Offer `/herdr:ask <agent>` or `/herdr:peek <agent>`.

CLI reads do not mark an agent as seen, so `done` stays `done` until the user focuses it.
