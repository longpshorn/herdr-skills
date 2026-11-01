---
name: spawn
description: Start a new Herdr agent that works one ticket or task end to end in its own git worktree and workspace, from a written brief, on any project (its specifics come from a herdr profile). Use for "/herdr:spawn <ticket or task>", "start a session for PROJ-123", "hand this to a new agent", "spin up a worktree agent".
---

# Spawn — one ticket, one worktree, one agent

Helper: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools"`, written `herdr_tools.py` below; it runs the helper with the first Python 3.9+ it finds (`python3`, `python` or `py -3`). It refuses to run outside a Herdr pane. The flow is the same on every project; what differs comes from the project's profile.

## 1. Settle the inputs

- **Repo and profile:** the current repo unless the user names another. Run `herdr_tools.py profile --path "<repo>"` and use its fields below (`forge`, `tracker`, `worktree`, `brief`). A repo without a profile resolves to working defaults; [PROFILES.md](${CLAUDE_PLUGIN_ROOT}/PROFILES.md) explains the fields.
- **Ticket and branch:**
  - `tracker.kind == jira`: read the ticket (`acli jira workitem view KEY`).
  - Branch: with `tracker.branch_command`, run it with `{type}` and `{key}` filled in and the ticket title on stdin, and read `.branch` from its JSON. Otherwise a short `<type>/<KEY-or-topic>-<desc>` branch (conventional-commit type, hyphen-case).
- **Names:** workspace label `<KEY or topic> <two-word topic>` (e.g. `PROJ-105 language`); agent name the key lower-cased without the dash (`proj105`), or a short topic, matching `[a-z][a-z0-9_-]{0,31}`. Check `herdr_tools.py status` so the name is not already taken.
- **Kind:** `claude` unless the user asks for another.
- **Shared concepts, across a batch:** list the concepts each ticket will define or change (rules, thresholds, mappings, value sets, vocabularies other code must agree with) and find their current owners in the code. Agents in parallel worktrees can't see each other, so a concept touched by two tickets gets one owner ticket. The others reuse its definition, landing after it or rebasing onto it. Say which in each brief.

## When the repo has its own launcher

If the profile sets `spawn.command`, the repository starts work its own way: its launcher creates the worktree, opens the workspace, starts the agent and may claim the ticket itself. Skip steps 2–4 and run:

```bash
herdr_tools.py repo-launch --path "<repo>" --key <KEY> [--branch <branch>]
```

Pass `--branch` only when the launcher's command uses `{branch}`. It runs the launcher from the main checkout and waits for the agent named by `spawn.agent`. Read its JSON:

- A refusal is printed as the launcher said it, for example a ticket that is already claimed or a workspace that is already open. Show it to the user and stop. Never work around it with `launch`, `git worktree add` or `herdr agent start`.
- `trust_prompt: true`: the agent is at Claude's folder-trust prompt. Tell the user which workspace to open, and never answer it for them.
- `brief: true` (the profile's `spawn.brief`): write the brief as in step 3, then send it with `herdr_tools.py send-prompt --name <agent> --prompt "Read the brief at <brief path> …"` once the agent is idle.
- `brief: false`: send nothing. The launcher gave the agent its task.

Then report as in step 5: the workspace, the agent and the launcher's output. The names and branch come from the launcher, not from step 1.

## 2. Create the worktree

Use the profile's `worktree.new` with `{branch}` filled in and its `worktree.env` exported first (values are shell expressions such as a CLI's token; never print a token). With no `worktree.new`, run `git worktree add -b <branch> "../<repo>-<KEY-or-topic>-<slug>" origin/<default branch>` from the repo, then the repo's own setup command (look in its `CLAUDE.md`, README, `package.json` scripts or `Makefile`). Confirm the branch is checked out there before going on.

## 3. Write the brief

The brief is the agent's whole task, so write it fully. Use [references/brief-template.md](references/brief-template.md), filling its Process from the profile's `brief.implement` and `brief.mr_create` and adding every `brief.notes` entry. Fill its status report path with `herdr_tools.py report-path --name <agent>`, which also creates the directory; keep the Status report section intact. Fill its Shared concepts section by pointing at each concept's source of truth (file and export), and never restate the concept's values. Values restated in several briefs drift apart, and each agent then builds its own copy faithfully. Put it in your scratchpad as `brief-<KEY>.md`, and check the file exists before launching; keep it until the agent has read it. Show the user the brief's key decisions in one short paragraph before launching only if the ticket left a real choice open; otherwise launch.

## 4. Launch

```bash
herdr_tools.py launch --path <worktree> --label "<label>" --name <agent> \
  --prompt "Read the brief at <brief path> and carry it out end to end. It is your full task. Plan first and stop at the plan gate for approval in this pane, then work test-first."
```

It opens the worktree as a background workspace (Herdr reuses the repo's parent workspace on its main checkout, or creates one), starts the agent, and sends the prompt. A new workspace's shell can take a few seconds to come up; launch retries `herdr agent start` until it is ready (up to 30s) and otherwise fails with a message naming the command to run by hand. Read its JSON:

- `trust_prompt: true` — the agent is at Claude's folder-trust prompt, and launch saved the prompt instead of sending it. Never answer the trust prompt for the user. Tell them which workspace to open, then run `herdr_tools.py send-prompt --name <agent>` in the background (`run_in_background`): it waits up to 10 minutes for them to answer, sends the saved prompt and confirms the agent is working. If it times out, re-run it once they have answered.
- `status: working` (or `idle` with `prompted: true`, a prompt it already finished) — done. `status: blocked` soon after is usually the agent's own question; `/herdr:peek` it.

## 5. Report

Workspace label, agent name, worktree path, branch, brief path, status report path, and what the agent will stop for (its plan gate). The user keeps focus; never focus the new workspace unless asked.
