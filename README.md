# herdr-skills

[![AgentAvow](https://agentavow.com/api/v1/public/scan/longpshorn/herdr-skills/badge)](https://agentavow.com/check/longpshorn/herdr-skills)

Claude Code skills for running coding agents in [Herdr](https://herdr.dev), a terminal workspace manager for AI coding agents. The workflow is **one ticket, one worktree, one agent**: you hand a ticket to a new agent in its own git worktree, from a written brief. It plans, stops for your approval, builds test-first and opens an MR. When the MR is approved and green, `/herdr:retire` lands it, cleans up and captures the follow-ups. You stay in one orchestrating session and see every agent from it.

This repository is a Claude Code plugin marketplace (`herdr-skills`) with one plugin, `herdr`, whose skills are `/herdr:setup`, `/herdr:status`, `/herdr:spawn`, `/herdr:ask`, `/herdr:peek`, `/herdr:run` and `/herdr:retire`.

## Requirements

- macOS. The skills have only been used on macOS so far; their helper and the examples are tested on Linux, macOS and Windows.
- [Herdr](https://herdr.dev) (`brew install herdr`), [Claude Code](https://claude.com/claude-code), `git`, and Python 3.9 or later as `python3`, `python` or `py -3` (the macOS Command Line Tools one is enough). The skills run the helper through `herdr/scripts/herdr-tools`, which picks the first that works.
- The CLI for your forge: `glab` (GitLab) or `gh` (GitHub), signed in. For Jira tickets, `acli`, signed in.

## Install

Install it into your personal Claude Code settings (user scope), so it works in every repo:

```bash
brew install herdr
claude plugin marketplace add longpshorn/herdr-skills --scope user
claude plugin install herdr@herdr-skills --scope user
```

**For a project's team**, have the project's setup script run those commands for each developer, only when they are missing. Treat an existing `herdr-skills` marketplace (such as a local clone someone edits) or an installed `herdr@herdr-skills` as present, and leave it alone. Keep the plugin out of the project's committed `.claude/settings.json`: it is each developer's tool, not a project dependency. What the project shares goes in its profile (below).

The plugin has no pinned version, so every commit here is a new version. Run `claude plugin update herdr@herdr-skills` to take it, or turn on auto-update for the `herdr-skills` marketplace in `/plugin` → Marketplaces.

## Getting started

1. Run `herdr` in a terminal. It starts (or reattaches to) a persistent session.
2. In a Herdr pane, `cd` to your repo's main checkout and start `claude`. This is your orchestrating session. The skills refuse to run outside a Herdr pane, except `/herdr:setup`.
3. Run `/herdr:setup` once per repo. It proposes the repo's profile and lists the practices it lacks (below), and writes or copies only what you approve.
4. Run `/herdr:status`. With nothing running it says so.

## The workflow

1. **Spawn.** `/herdr:spawn RES-123` reads the ticket, creates a worktree and branch (with the project's own worktree command when it has one), writes a brief, opens a background workspace and starts a named agent with the brief.
2. **Trust prompt.** A new worktree is a folder Claude Code hasn't seen, so the agent may stop at the folder-trust prompt. Answer it yourself in that workspace; the skills never answer it for you. The brief is then sent automatically.
3. **Plan gate.** The agent reads the brief, plans, and stops. `/herdr:status` shows it as `blocked`; `/herdr:peek <agent>` shows the plan. Approve it, or change it, with `/herdr:ask <agent> "<your answer>"` or by typing in its pane.
4. **Build.** The agent works test-first, commits, pushes, opens the MR and waits for the pipeline. It never merges.
5. **Status report.** Every time the agent stops, it rewrites its status report in `~/.cache/herdr/status/<agent>.md`: state (`in-progress`, `waiting-on-owner`, `mr-open`, `merged`, `abandoned`), MR, pipeline, summary, open questions, follow-ups. It ends with "ready to land" when the MR is open with a passing pipeline and nothing is left on its side.
6. **Validate and approve.** Review the MR and try the change. If the project requires an approval, it must come from someone other than the MR's author: ask a teammate.
7. **Retire.** `/herdr:retire <agent>` checks the report and the MR. After one confirmation it does the following:
   - squash-merges the MR pinned to its tested commit, rebasing and waiting for the pipeline first if the MR is behind;
   - closes the agent's workspace and removes its worktree and branch;
   - moves the ticket to done and fast-forwards your main checkout;
   - restarts the main dev server if dependencies or config changed;
   - turns each valid follow-up into a ticket.

   It stops and says why on anything unsafe: a failed or missing pipeline, conflicts, unresolved threads, a missing approval, uncommitted work, or open questions.

An agent's MR doesn't have to be in the repo it works in. When its worktree branch has no MR, retire uses the one its status report names, and `herdr_tools.py land --mr <url or group/project!iid>` lands any MR directly, with the same checks.

To land several agents in a set order, run `herdr_tools.py land <agent> <agent>...`. It lands each in turn. An agent whose MR conflicts is asked once to rebase, the rest carry on, and it is retried at the end. It prints one line per agent. To land whichever is ready first, run `herdr_tools.py watch <agent>...` (the retire skill shows how). It lands the first agent whose report says "ready to land" and then exits. An agent whose MR conflicts is asked once to rebase, and an MR waiting for an approval keeps waiting.

Retire checks that the worktree's HEAD landed. When `land` rebased the MR first, the merged commit differs from HEAD, so it compares the change itself (`git patch-id`) with the squash or merge commit the forge reports, and says how it matched.

## The skills

| Skill | Use it to |
| - | - |
| `/herdr:setup` | Propose a repo's profile and list the practices it lacks, from the examples; write or copy what you approve |
| `/herdr:status` | See every agent, the ones needing you (`blocked`, `done`) first, with its workspace and what it is doing |
| `/herdr:spawn <ticket>` | Start an agent on a ticket in its own worktree, from a brief |
| `/herdr:peek <agent>` | Read what an agent is doing or asking, without sending it anything |
| `/herdr:ask <agent> <message>` | Answer a plan gate or question, or send an instruction; optionally wait for the reply |
| `/herdr:run <command>` | Run a dev server, test run or watcher in a sibling pane and read its output |
| `/herdr:retire <agent>` | Land a validated MR, clean up, close the ticket, update main, capture follow-ups |

## What the agent needs from you

- Answers to folder-trust and permission prompts in its workspace. The skills never answer them for you.
- A decision at the plan gate.
- Answers to the questions it asks. They show as `blocked` in `/herdr:status`.
- A validated MR, and an approval from a teammate when the project requires one.
- `/herdr:retire` when its MR is ready to land.

## Project profiles

Each project describes itself in a profile: forge, tracker, worktree commands, dev server, and how its agents work and open MRs. The repo keeps it in `.claude/herdr-profile.json`. You can lay your own personal overlay over it in `~/.config/herdr/profiles/`. A repo without either still works on detected defaults. [herdr/PROFILES.md](herdr/PROFILES.md) explains the fields.

## Examples

The plugin works on any repo, but much of what makes parallel agents safe lives in the repo itself. Two examples, which `/herdr:setup` reads from, show it:

- [`herdr/examples/minimal`](herdr/examples/minimal): one profile file for a GitHub repo, and a sample personal overlay. Enough for one person running a few agents.
- [`herdr/examples/full`](herdr/examples/full): a small Node sample repo with every practice, each small and tested: a worktree command with per-worktree ports, the agent lifecycle in `CLAUDE.md`, a Stop hook that runs the checks, one owner per shared concept with a check, doc parity and docs-impact, MR and PR templates with "Docs reviewed", and a bootstrap that installs this plugin in each developer's own settings. Its README shows the GitHub variant.

## Development

The skills are short; their deterministic parts live in `herdr/scripts/herdr_tools.py` (Python 3.9, no dependencies). CI runs its unit tests and the full example's tests on Linux, macOS and Windows:

```bash
python3 -m unittest discover -s herdr/scripts -p 'test_*.py'
(cd herdr/examples/full && npm test)
```

When a CI job is cancelled because no hosted runner picked it up (most often on macOS), `.github/workflows/retry-unacquired.yml` re-runs the failed jobs, at most twice. A job that fails its tests is never the trigger.

To work on the skills, clone this repo and register the clone as a marketplace (`claude plugin marketplace add ./herdr-skills`). Claude Code reads a local marketplace in place, so a new session picks up your edits.

## License

[MIT](LICENSE).
