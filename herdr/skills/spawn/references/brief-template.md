# Brief template

Fill every section from the ticket, the repo's `CLAUDE.md`, the project's herdr profile (`herdr_tools.py profile --path "<repo>"`) and what the conversation established. Cut a section only when it truly has nothing to say. Write it for an agent with no context: state facts and paths, not history.

```markdown
# Brief: <KEY>, <one-line goal>

You are implementing **<KEY>** "<ticket title>" (<tracker>) in the <repo> repo, for <owner's name>, the owner: the person who approves your plan and lands your MR. Read this brief completely before starting.

## Where you are

- You are running in an isolated git worktree: `<worktree path>`. The branch `<branch>` is already checked out, cut from `origin/<default branch>`, and set up. Work only in this worktree. Never touch the main checkout or another worktree.
- <Each of the profile's `brief.notes`, e.g. how installs authenticate (never print or write a token).>
- <Other agents working in parallel and the shared files where conflicts are likely; resolve them on rebase, keeping both sides.>
- <Reserved numbers, such as an ADR number, if parallel work could collide.>

## Background

<What earlier tickets established that this one builds on: merged MRs, ADRs, the pattern to follow, with file paths.>

## Read first

<The repo CLAUDE.md, the ADRs and package/feature CLAUDE.md files in scope, and the existing code to start from.>

## Acceptance criteria / the owner's direction

<The ticket's ACs verbatim, numbered, plus any decisions the owner made in conversation.>

## Points to get right

<The traps you already know about: compatibility rules, layering, data that must survive, framework gotchas. Each as a short bullet with the reason.>

## Shared concepts

<Each concept this work touches that other code must agree with (a rule, threshold, mapping, value set or vocabulary): its owning definition to reuse (file and export), or "introduced here" with where it will live. Name concepts; never restate their values. List other work in flight that touches the same concept, and which work owns it.>

Before you define any rule, threshold, mapping or list of values, search for an existing definition (the repo's registry if it has one, then the code) and reuse it. Your plan must include a **Shared concepts** section: what you reuse, what you introduce and where it lives. If you find two definitions of one concept, say so at the plan gate instead of adding a third.

## Process

1. <The profile's `brief.implement`, with `{key}` filled in. Whatever it says, planning is a hard stop: present the plan and wait for the owner's approval in this pane.>
2. Commits are `<type>: <KEY> <desc>`, small and logical, ending with the Co-Authored-By line from your attribution instructions.
3. Done means: <the repo's full check and test commands> are clean; docs are updated (<the ones in scope>); the branch is rebased on `origin/<default branch>`.
4. Push and open the MR/PR with <the profile's `brief.mr_create`> targeting `<default branch>`, titled `<type>: <KEY> <summary>`, describing what was built and its verification (each item Verified or Unverified), ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Don't post to Slack. Never merge it: the owner lands it with `/herdr:retire` after validating.
5. Wait for the MR pipeline and report the job results. If a job fails, fix the root cause.
6. Every push runs the whole pipeline and costs compute, so push only what is ready to be checked: run the step 3 checks locally first, and don't push work in progress. When answering review, fix every open thread and push once for the round, not once per thread.

## Constraints

- Never write to the P: or L: drives.
- No secrets in files.
- Don't approve security or folder-trust prompts on the owner's behalf.
- Don't change the forge's project or repository settings.
- <With a ticket tracker: only move <KEY> through the statuses your implement step uses; say whether creating tickets is allowed.>

## Status report

Keep a status report at `<status report path>` (outside the worktree, so it never shows in `git status`). Overwrite the whole file every time you stop and hand back to the owner: at the plan gate, when you ask them something, after opening the MR, after the pipeline result, and when you finish. Then print the same text as your last message. `/herdr:retire` reads this file to decide whether the session can close and to log its outcome, so keep it true and exact.

Use this format. The header lines must stay `key: value`; write "None" in an empty section.

    ---
    agent: <agent name>
    ticket: <KEY>
    branch: <branch>
    state: in-progress | waiting-on-owner | mr-open | merged | abandoned
    safe_to_retire: yes | no
    mr: <MR URL, or none>
    pipeline: passed | failed | running | none
    updated: <YYYY-MM-DD HH:MM>
    ---
    ## Summary
    <Two or three lines: what this session achieved.>

    ## Changed
    - <Each change, marked Verified or Unverified.>

    ## Open questions
    <Each question the owner must answer, or None.>

    ## Follow-ups
    - <Each loose end worth a ticket: what it is, why it matters (the risk or cost), the evidence (file, test, log), and a size (S, M or L). Or None.>

Set `safe_to_retire: yes` only when nothing is left on your side: the MR is merged (or the owner told you to abandon the work), the worktree is clean, and you have no open questions. The owner lands the MR from their own session with `/herdr:retire`, which merges it, so you will usually end on `mr-open` and `no`. Never merge it yourself. When your MR is open with a passing pipeline and nothing is left on your side, say "ready to land" in the Summary; if they tell you it has merged, update the report to `merged` and `yes`.
```
