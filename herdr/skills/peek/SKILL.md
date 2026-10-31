---
name: peek
description: Read what a Herdr agent is doing or waiting for, without sending it anything. Use for "/herdr:peek <agent>", "what is proj105 up to", "why is the ui agent blocked", "show me the agent's last answer". Read-only.
---

# Peek — read an agent without disturbing it

Targets are agent names or pane ids from `/herdr:status`. Herdr refuses commands outside a Herdr pane; if so, say so and stop.

1. `herdr agent get <agent>`: note `agent_status`, `terminal_title_stripped` and `cwd`.
2. Read the screen. Output is plain text, not JSON.
   - Blocked or idle: `herdr agent read <agent> --source visible --lines 80`, which shows the prompt or question.
   - Working or for its history: `herdr agent read <agent> --source recent-unwrapped --lines 200`.
3. Report briefly: its state, what it is working on, and, if blocked, exactly what it is asking and the options it offers. Quote a question verbatim. Call out a folder-trust, security or permission prompt as the user's to answer.

Never send input from this skill; that is `/herdr:ask`. Reads do not mark the agent as seen.
