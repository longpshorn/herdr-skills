---
name: setup
description: Prepare a repo for herdr agents — inspect it (forge, tracker, package manager, worktree tooling, AGENTS.md/CLAUDE.md, hooks, doc checks), propose its .claude/herdr-profile.json from the closest example, list the practices it lacks with why each matters, and, after the user approves, write the profile and copy the chosen practices from the full example. Use for "/herdr:setup", "set this repo up for herdr", "what does this repo need for agents", "add a herdr profile".
---

# Setup — make a repo ready for herdr agents

Helper: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools"`, written `herdr_tools.py` below; it runs the helper with the first Python 3.9+ it finds (`python3`, `python` or `py -3`). `setup-scan` only reads the repo, so it also runs outside Herdr. The examples live in `${CLAUDE_PLUGIN_ROOT}/examples/`: `minimal/` (a profile and a personal overlay) and `full/` (a sample repo with every practice, each one tested; its README explains them).

1. **Scan.** `herdr_tools.py setup-scan --path "<repo>"` prints JSON: `forge` (from `origin`), `tracker` (a Jira project guessed from ticket keys in branches and commits, or null), `package_manager`, `profile` (the existing profile file, if any), `closest_example`, `proposed_profile`, `plugin_in_committed_settings`, and `practices`, each with `present`, `why`, `evidence` and the `copy` files from `full`. Then read the repo's `AGENTS.md` and `CLAUDE.md`, `package.json` scripts (or `Makefile`) and `.claude/settings.json` yourself: the scan finds clues, and you confirm them.
2. **Committed plugin.** If `plugin_in_committed_settings` is true, say so first: the herdr plugin is in the project's committed `.claude/settings.json`, so it is forced on every teammate. Recommend moving it to each developer's user scope (the `personal_install` practice). Never add it there yourself.
3. **Propose.**
   - **Profile:** with no `profile`, show `proposed_profile` and complete it from what you read. Use the real worktree and dev-server commands, and their `ready` line if the server prints one. For a Jira `tracker`, ask for its `done_path` (the statuses a ticket moves through to done) and `parent`. Add `brief.notes` for the project's done criteria and merge rules. Keep only the fields that differ from the defaults ([PROFILES.md](${CLAUDE_PLUGIN_ROOT}/PROFILES.md)), and never put a secret in one. `closest_example` names the example to model it on. With an existing `profile`, review it against the repo instead, and propose only changes.
   - **Personal fields:** `planning` and `mirrors` belong to one person. Offer them as an overlay in `~/.config/herdr/profiles/` (see `minimal/overlay.example.json`), never in the repo.
   - **Practices:** for each practice that is not `present`, give one line: what it is, why it matters (`why`), and the files to copy. Mention practices the repo has in a different form (its own worktree script, a lint rule doing the shared-concepts job); don't propose replacing them.
4. **Confirm once.** Ask which to apply: the profile (as shown, or edited), and which practices to copy. Nothing is written before this answer.
5. **Apply** only what was approved.
   - Write `.claude/herdr-profile.json`.
   - Copy each approved practice's files from `${CLAUDE_PLUGIN_ROOT}/examples/full/`, keeping their paths. Never overwrite an existing file: for one that exists (an `AGENTS.md`, a `.claude/settings.json`, a `.gitignore`, a template), merge the example's section into it and show the diff. Practices share some files (the bootstrap, the worktree library); copy each one once. The agent rules go in `AGENTS.md`, which every coding agent reads, and `CLAUDE.md` holds only `@AGENTS.md`. When the repo keeps its rules in `CLAUDE.md` and has no `AGENTS.md`, propose moving them into `AGENTS.md` and leaving that one line, and show the diff.
   - Adapt each copy to the repo: its package manager and script names, its source folders (the shared-concepts and doc-parity checks guard `src/`), its default branch, and the `ACME` placeholders. Add the scripts the copies need to `package.json`.
   - The Stop hook and the checks run `npm run check`; point them at the repo's real check command.
   - For a repo that isn't Node, copy the idea, not the file: say what the practice needs and write the smallest equivalent in the repo's language, test-first.
6. **Verify.** Run `herdr_tools.py profile --path "<repo>"` (inside Herdr) or read back the profile, run each copied practice's tests and the repo's check command, and fix what fails.
7. **Report.** List each file written or changed and the practices still missing, and suggest committing them on a branch through the project's usual review. Don't commit to the default branch.
