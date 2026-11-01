# herdr-skills

[![AgentAvow](https://agentavow.com/api/v1/public/scan/longpshorn/herdr-skills/badge)](https://agentavow.com/check/longpshorn/herdr-skills)

Skills for running coding agents in [Herdr](https://herdr.dev), a terminal workspace manager for AI coding agents. The workflow is **one ticket, one worktree, one agent**: you hand a ticket to a new agent in its own git worktree, from a written brief. It plans, stops for your approval, builds test-first and opens an MR. When the MR is approved and green, `/herdr:retire` lands it, cleans up and captures the follow-ups. You stay in one orchestrating session and see every agent from it.

This repository is a Claude Code plugin marketplace (`herdr-skills`) with one plugin, `herdr`, whose skills are `/herdr:setup`, `/herdr:status`, `/herdr:spawn`, `/herdr:ask`, `/herdr:peek`, `/herdr:run` and `/herdr:retire`. The same skills run in Codex, Gemini CLI, OpenCode and omp: see [Other coding agents](#other-coding-agents).

## Requirements

- macOS. The skills have only been used on macOS so far; their helper and the examples are tested on Linux, macOS and Windows.
- [Herdr](https://herdr.dev) (`brew install herdr`), [Claude Code](https://claude.com/claude-code) or [another coding agent](#other-coding-agents), `git`, and Python 3.9 or later as `python3`, `python` or `py -3` (the macOS Command Line Tools one is enough). The skills run the helper through `herdr/scripts/herdr-tools`, which picks the first that works.
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

## Other coding agents

Claude Code installs the skills as a plugin (above), and that stays the main way in. Other coding agents read plain skill folders: Codex, Gemini CLI, OpenCode and omp all read `~/.agents/skills`. `install-skills` writes a copy of each skill there.

```bash
git clone https://github.com/longpshorn/herdr-skills ~/code/herdr-skills   # any folder that stays put
sh ~/code/herdr-skills/herdr/scripts/herdr-tools install-skills             # into ~/.agents/skills: all four tools
sh ~/code/herdr-skills/herdr/scripts/herdr-tools install-skills --update    # later: pull the clone, refresh the copies
```

- **What it writes.** One folder per skill, named `herdr-<skill>` (`herdr-status`, `herdr-spawn`, ...), so generic names such as `status` and `run` can't collide with another package's skills. In each copy `${CLAUDE_PLUGIN_ROOT}`, which only Claude Code fills in, is replaced by the clone's absolute `herdr` folder (forward slashes on Windows too), so every skill finds its helper, examples and references. A note under the heading says that `/herdr:<name>` in the text means the `herdr-<name>` skill.
- **Where.** `--tool` picks a tool's own folder instead of the shared one, and `--dest` any folder. Run it once per tool you want, or once for all four with the default.

  | `--tool` | Folder | Source |
  | - | - | - |
  | `agents` (default), `codex` | `~/.agents/skills` | [Codex skills](https://learn.chatgpt.com/docs/build-skills) (`~/.codex/skills` is deprecated); also read by Gemini CLI, OpenCode and omp |
  | `gemini` | `~/.gemini/skills` | [Gemini CLI skills](https://geminicli.com/docs/cli/skills/) |
  | `opencode` | `$XDG_CONFIG_HOME/opencode/skills` (default `~/.config/opencode/skills`) | [OpenCode skills](https://opencode.ai/docs/skills/) |
  | `omp` | `~/.omp/agent/skills` | [omp skills](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md) |

- **Safe to re-run.** Each copy holds a `.skills-install.json` marker (plugin, skill, clone, tool, commit). Only folders with herdr's marker are ever replaced or removed; a `herdr-*` folder without one stops the install before anything is written. `--dry-run` shows what would change. `--update` refuses a clone with local changes (`--no-pull` re-renders without pulling). `--uninstall` removes the copies. A copy made with `--dest` is updated and removed with the same `--dest`.
- **Install from a clone.** It refuses a folder outside a git clone, such as Claude Code's plugin cache, because that folder moves on every update and the copies would point at nothing.
- **Never edits another tool's settings.** When a tool needs one, it prints it. Gemini CLI reads `GEMINI.md`, not `AGENTS.md`, unless `~/.gemini/settings.json` has `"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}`. Codex, OpenCode and omp read `AGENTS.md` as they are.
- **Agents of other kinds.** `/herdr:spawn` starts any kind `herdr agent` lists (`codex`, `gemini`, `opencode`, `omp`, ...). A spawned agent reads its brief as text; it needs the copies only to run the skills itself.
- **Claude Code-only parts.** Where a skill names a Claude Code tool (`run_in_background`), the same line says what to do elsewhere. Hooks have no common equivalent: the full example's Stop hook runs only in Claude Code, and its `AGENTS.md` tells every agent to run the check itself, so nothing depends on it. The herdr plugin itself ships no hooks.

Codex and omp can also add this repository as a Claude Code-style marketplace (`codex plugin marketplace add`, `omp plugin marketplace add`). That route is untested; `install-skills` is the supported one.

**For sibling plugins** (work-skills, adlc-skills): copy this mechanism as it is. The plugin's helper owns it (`render_skill`, `install_skills`, `SKILL_DIRS` and `SKILLS_MARKER` in `herdr/scripts/herdr_tools.py`), run as `sh <plugin>/scripts/<plugin>-tools install-skills` with the same flags. Copies are named `<plugin>-<skill>`, and the marker's `plugin` field is the plugin's name. `/herdr:retire` finds the work plugin's launcher from a work copy's marker when the Claude Code plugin isn't installed.

## Getting started

1. Run `herdr` in a terminal. It starts (or reattaches to) a persistent session.
2. In a Herdr pane, `cd` to your repo's main checkout and start `claude`. This is your orchestrating session. The skills refuse to run outside a Herdr pane, except `/herdr:setup`.
3. Run `/herdr:setup` once per repo. It proposes the repo's profile and lists the practices it lacks (below), and writes or copies only what you approve.
4. Run `/herdr:status`. With nothing running it says so.

## The workflow

1. **Spawn.** `/herdr:spawn PROJ-123` reads the ticket, creates a worktree and branch (with the project's own worktree command when it has one), writes a brief, opens a background workspace and starts a named agent with the brief.
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

Each project describes itself in a profile: forge, tracker, worktree commands, dev server, and how its agents work and open MRs. A repository that already has its own launcher or merge process can hand those steps to it (`spawn.command`, `retire.land: repo`), so the skills never start or merge work around it; [`herdr/examples/repo-process`](herdr/examples/repo-process) shows how. Without those fields the default flow runs. The repo keeps it in `.claude/herdr-profile.json`. You can lay your own personal overlay over it in `~/.config/herdr/profiles/`. A repo without either still works on detected defaults. [herdr/PROFILES.md](herdr/PROFILES.md) explains the fields.

## Examples

The plugin works on any repo, but much of what makes parallel agents safe lives in the repo itself. Two examples, which `/herdr:setup` reads from, show it:

- [`herdr/examples/minimal`](herdr/examples/minimal): one profile file for a GitHub repo, and a sample personal overlay. Enough for one person running a few agents.
- [`herdr/examples/repo-process`](herdr/examples/repo-process): a profile for a repository with its own launcher and merge process. Spawn runs the launcher, and retire waits for the repo's own merge, then cleans up with the repo's command.
- [`herdr/examples/full`](herdr/examples/full): a small Node sample repo with every practice, each small and tested: a worktree command with per-worktree ports, the agent lifecycle in `AGENTS.md` (which `CLAUDE.md` imports), a Stop hook that runs the checks, one owner per shared concept with a check, doc parity and docs-impact, MR and PR templates with "Docs reviewed", and a bootstrap that installs this plugin in each developer's own settings. Its README shows the GitHub variant.

## Development

The skills are short; their deterministic parts live in `herdr/scripts/herdr_tools.py` (Python 3.9, no dependencies). CI runs its unit tests and the full example's tests on Linux, macOS and Windows. Linux is pinned to Ubuntu 24.04, because Python 3.9 has no Ubuntu 26.04 build:

```bash
python3 -m unittest discover -s herdr/scripts -p 'test_*.py'
(cd herdr/examples/full && npm test)
```

When a CI job is cancelled because no hosted runner picked it up (most often on macOS), `.github/workflows/retry-unacquired.yml` re-runs those jobs, at most twice. If any job in the run failed on a runner, it retries nothing, so a flaky failure stays visible. Its tests run in CI, or locally:

```bash
python3 -m unittest discover -s .github/tests -p 'test_*.py'
```

To work on the skills, clone this repo and register the clone as a marketplace (`claude plugin marketplace add ./herdr-skills`). Claude Code reads a local marketplace in place, so a new session picks up your edits.

## License

[MIT](LICENSE).
