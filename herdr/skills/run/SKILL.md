---
name: run
description: Run a shell command in a new sibling Herdr pane (a dev server, a long test run, a watcher) and read its output, without blocking this session. Use for "/herdr:run <command>", "start the dev server in a pane", "run the e2e suite next to me", "tail this in another pane".
---

# Run — a command in a sibling pane

Herdr refuses commands outside a Herdr pane; if so, say so and stop.

1. Pick the split from the caller pane's shape: `herdr pane layout --pane "$HERDR_PANE_ID"`. Split a wide pane `right`, a tall or narrow one `down`. Honour a direction the user names.
2. Create the pane, keeping the user's focus and this working directory:
   `herdr pane split --current --direction <right|down> --cwd "$PWD" --no-focus` and take `.result.pane.pane_id`. Name it for the command: `herdr pane rename <pane> "<short label>"`.
3. Run it: `herdr pane run <pane> "<command>"`.
4. Wait when there is something to wait for: `herdr pane wait-output <pane> --match "<literal text>" --timeout <ms>` (or `--regex`), e.g. a server's ready line or the test summary. Always pass a timeout. A match on output already on screen returns immediately.
5. Read and summarize: `herdr pane read <pane> --source recent-unwrapped --lines 200`. Report the result and the pane id.

Leave the pane running unless the user asks to close it; close only panes you created (`herdr pane close <pane>`). For coding agents use `/herdr:spawn`, not this.
