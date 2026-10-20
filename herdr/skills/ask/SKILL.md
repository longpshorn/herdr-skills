---
name: ask
description: Send a message to a running Herdr agent by name and, optionally, wait for and read its reply. Use for "/herdr:ask <agent> <message>", "tell res1105 to ...", "ask the ui agent whether ...", "send the prompt to the new agent".
---

# Ask — talk to another agent

Targets are agent names or pane ids from `/herdr:status`. Herdr refuses commands outside a Herdr pane (`HERDR_ENV` must be 1); if so, say so and stop.

1. Check the agent's state: `herdr agent get <agent>` (`.result.agent.agent_status`).
   - `blocked`: run `/herdr:peek` first. If it is a folder-trust, security or permission prompt, do not answer it; tell the user. If it is the agent's own question (a plan gate, a choice) and the user's message answers it, send it.
   - `working`: say so; sending now queues the message after its current turn. Ask the user whether to send anyway unless they said to.
2. Send. Pass the message as one argument, quoted for the shell:
   - Fire and forget: `herdr agent prompt <agent> "<message>"`.
   - Wait for the reply (only when the user wants it, or the message is a quick question): `herdr agent prompt <agent> "<message>" --wait --timeout 600000`. `--wait` returns at the next settled state (idle, done or blocked). Don't add `--until`.
3. If you waited, read the reply: `herdr agent read <agent> --source recent-unwrapped --lines 200` (plain text, not JSON) and summarize it. If the reply is cut off and more lines don't help, the agent draws on the alternate screen: ask it to write its full answer to a temporary Markdown file and reply only with the path, then read the file.

Errors: `agent_prompt_stalled` means no state change within five seconds of sending; read the pane before retrying. Never send keys such as `ctrl+c` or `esc` unless the user asks.
