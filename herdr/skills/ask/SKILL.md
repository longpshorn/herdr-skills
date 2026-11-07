---
name: ask
description: Send a message to a running Herdr agent by name and, optionally, wait for and read its reply. Use for "/herdr:ask <agent> <message>", "tell proj105 to ...", "ask the ui agent whether ...", "send the prompt to the new agent".
---

# Ask — talk to another agent

Helper: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools"`, written `herdr_tools.py` below; it runs the helper with the first Python 3.9+ it finds (`python3`, `python` or `py -3`). Targets are agent names or pane ids from `/herdr:status`. Herdr refuses commands outside a Herdr pane (`HERDR_ENV` must be 1); if so, say so and stop.

Every message goes as a file plus one pointer line, whatever its length: text typed into a pane can arrive cut short. Keys (Esc, Enter) are not messages.

1. Check the agent's state: `herdr agent get <agent>` (`.result.agent.agent_status`).
   - `blocked`: run `/herdr:peek` first. If it is a folder-trust, security or permission prompt, do not answer it; tell the user. If it is the agent's own question (a plan gate, a choice) and the user's message answers it, send the answer as below. When the question is a dialog with options, never answer it with arrow keys or Enter: press Esc (`herdr agent send-keys <agent> esc`), wait until it is idle (`herdr_tools.py wait --name <agent> --until idle --timeout-seconds 60`), then send the answer as a message.
   - `working`: say so; the message queues until its current turn ends. Ask the user whether to send anyway unless they said to, and pass a longer `--timeout-seconds` (such as 1800) so the check covers the wait.
2. Send: `herdr_tools.py deliver --name <agent> --topic <two-word topic> --text "<message>"` (or `--file <path>` for a message already written). It writes the file, sends `Message from <you>: read <path> (sha256 …) and act on it.`, and prints JSON:
   - `verified: yes`: the line arrived intact and the agent read the file.
   - `verified: no`: show `detail`. A pointer that never arrived was already resent once; one that arrived but wasn't read is not resent, since the agent may still be busy. Peek before doing anything else.
   - `verified: unverified`: an agent other than Claude; it was sent, but its arrival can't be checked.
3. To wait for the reply (only when the user wants it, or the message is a quick question): `herdr_tools.py wait --name <agent> --until idle,done,blocked --timeout-seconds 600`. A blank or unknown status counts as still waiting. Then read the reply: `herdr agent read <agent> --source recent-unwrapped --lines 200` (plain text, not JSON) and summarize it. If the reply is cut off and more lines don't help, the agent draws on the alternate screen: ask it (as a message) to write its full answer to a temporary Markdown file and reply only with the path, then read the file.

Never send keys such as `ctrl+c`, or `esc` outside the dialog rule above, unless the user asks.
