#!/usr/bin/env python3
"""Deterministic helpers behind the /herdr:* skills. Talks to the running Herdr session through the `herdr` CLI.

  herdr_tools.py status
      Every agent, the ones needing attention (blocked, done) first, with its workspace and a target to address it by.
  herdr_tools.py parent-workspace --path PATH [--create [--wait-seconds 5]]
      The workspace holding PATH's repo main checkout. Herdr opens worktree workspaces only from it. --create opens
      one when there is none and waits up to --wait-seconds for Herdr to list it (closing it again if it never does).
  herdr_tools.py launch --path WORKTREE --label LABEL --name AGENT [--kind claude] (--prompt TEXT | --prompt-file FILE)
      Open WORKTREE as a background workspace, start a named agent in it, stop if a folder-trust prompt shows,
      otherwise send the prompt and confirm the agent started working. Prints a JSON summary. Herdr finds or creates
      the repo's parent workspace. At a trust prompt the prompt is saved for send-prompt.
  herdr_tools.py repo-launch --path REPO --key KEY [--branch BRANCH] [--timeout-seconds 120]
      Spawn through the repo's own launcher (its profile's spawn.command, with {key} and {branch} filled in) instead
      of launch, then wait for the agent it starts (spawn.agent). The launcher owns the worktree and the workspace;
      its refusal is shown as it said it.
  herdr_tools.py send-prompt --name AGENT [--prompt TEXT | --prompt-file FILE] [--timeout-seconds 600]
      Finish a launch that stopped at the folder-trust prompt: wait until the person running the session answers it
      and the agent is idle, then send the prompt launch saved (or the one given) and confirm the agent started working.
  herdr_tools.py report-path --name AGENT
      Create the status-report directory and print the file AGENT keeps its status report in.
  herdr_tools.py retire-info AGENT|WORKSPACE_ID|LABEL
      JSON for /herdr:retire: workspace, worktree, main checkout, branch, MR (the worktree branch's, else the one the
      agent's status report names, in any repo), the agent's parsed status report, and every retire check.
  herdr_tools.py land [AGENT|WORKSPACE_ID|LABEL] [--mr URL|GROUP/PROJECT!IID] [--timeout-minutes 30]
      Merge the agent's validated MR (its worktree branch's, else the one its status report names), or the MR --mr
      names in any repo: rebase if behind, wait for the head pipeline, squash-merge pinned to that commit. Stops on a
      draft, conflicts, unresolved threads, a failed pipeline or a missing approval. Refused outright (as are the
      forms below and watch) for a repo whose profile sets retire.land to "repo": its own process merges.
  herdr_tools.py land AGENT AGENT... [--max-hours 3] [--timeout-minutes 30]
      Land several agents' MRs in this order. An agent whose MR conflicts is asked once to rebase and retried after
      the others until --max-hours; any other refusal fails that agent. Prints one line per agent; exits 1 unless
      all merged.
  herdr_tools.py watch AGENT... [--max-hours 4] [--poll-seconds 120]
      Watch the agents' status reports and land the first one that is ready (MR open, pipeline passed, "ready to
      land", no open questions), then exit. An agent whose MR conflicts is asked once to rebase; an MR waiting on an
      approval keeps being watched.
  herdr_tools.py update-main --path REPO
      Fast-forward the main checkout (restoring files that differ only by JSON key order) and list the changed files
      a running dev server needs a restart for.
  herdr_tools.py main-server start|restart|status --path REPO [--command CMD]
      Run the main checkout's dev server (its profile's dev_server.command) in a Herdr pane labelled
      "main dev server", restart it, or report it.
  herdr_tools.py setup-scan --path REPO
      For /herdr:setup (and outside Herdr too): the repo's forge, a tracker guessed from its ticket keys, its package
      manager, which practices from examples/full it has, and a proposed profile when it has none.
  herdr_tools.py profile --path REPO
      The project's resolved profile: the repo's .claude/herdr-profile.json, with a personal overlay from
      ~/.config/herdr/profiles/ (matched by origin URL or path) laid over it, over the defaults.

Python 3.9 compatible (the macOS Command Line Tools python3). The skills run it through `sh herdr-tools`, which finds
a Python 3.9+ as `python3`, `python` or `py -3`.
"""
import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

ATTENTION_ORDER = {"blocked": 0, "done": 1, "working": 2, "idle": 3, "unknown": 4}
TRUST_PROMPT = re.compile(r"trust (the files in )?this folder|Do you trust", re.IGNORECASE)


class HerdrError(Exception):
    pass


# ---- pure logic (tested) ----------------------------------------------------------------------------------------


def find_parent_workspace(workspaces, repo_root, panes=()):
    """The workspace on the repo's main checkout (not a linked worktree), or None.

    Herdr fills in a workspace's `worktree` info only once a worktree is opened from it, so a workspace without that
    info counts when one of its panes sits in the main checkout. Reported worktree info wins.
    """
    root = repo_root.rstrip("/")
    for ws in workspaces:
        wt = ws.get("worktree") or {}
        if wt.get("repo_root", "").rstrip("/") == root and not wt.get("is_linked_worktree", False):
            return ws
    in_root = {p["workspace_id"] for p in panes if (p.get("cwd") or "").rstrip("/") == root}
    for ws in workspaces:
        if "worktree" not in ws and ws["workspace_id"] in in_root:
            return ws
    return None


def status_rows(workspaces, agents):
    labels = {ws["workspace_id"]: ws.get("label", ws["workspace_id"]) for ws in workspaces}
    rows = [
        {
            "status": a.get("agent_status", "unknown"),
            "target": a.get("name") or a["pane_id"],
            "kind": a.get("agent", "?"),
            "workspace": labels.get(a.get("workspace_id"), a.get("workspace_id", "?")),
            "title": a.get("terminal_title_stripped", ""),
        }
        for a in agents
    ]
    return sorted(rows, key=lambda r: (ATTENTION_ORDER.get(r["status"], 9), r["workspace"].lower()))


def format_status(rows):
    if not rows:
        return "No agents are running."
    cols = [("STATUS", "status"), ("AGENT", "target"), ("KIND", "kind"), ("WORKSPACE", "workspace"), ("TITLE", "title")]
    widths = [max(len(head), *(len(r[key]) for r in rows)) for head, key in cols[:-1]]
    def line(values):
        return "  ".join(v.ljust(w) for v, w in zip(values[:-1], widths)) + "  " + values[-1]
    out = [line([head for head, _ in cols])]
    out += [line([r[key] for _, key in cols]) for r in rows]
    return "\n".join(out)


def has_trust_prompt(screen_text):
    return bool(TRUST_PROMPT.search(screen_text))


def repo_root_from_common_dir(common_dir):
    """`git rev-parse --git-common-dir` is <main checkout>/.git for the main checkout and every linked worktree."""
    return os.path.dirname(common_dir.rstrip("/"))


def status_report_path(agent, home=None):
    """Where a spawned agent keeps its status report: outside the worktree, so it survives removal and stays out of git."""
    return os.path.join(home or os.path.expanduser("~"), ".cache", "herdr", "status", agent + ".md")


def parse_status_report(text):
    """A report is a `---` header of `key: value` lines, then `## Section` blocks. None if the header is missing."""
    match = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    sections = {}
    for block in re.split(r"^## ", match.group(2), flags=re.MULTILINE)[1:]:
        heading, _, body = block.partition("\n")
        sections[heading.strip()] = body.strip()
    return {"fields": fields, "sections": sections}


def report_followups(report):
    """The items /herdr:retire must triage: each Follow-ups bullet, and each change the agent marked Unverified.

    A bullet's continuation lines join it; "None" means nothing.
    """
    if not report:
        return []

    def bullets(text):
        items = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                items.append(stripped[2:].strip())
            elif stripped and items:
                items[-1] += " " + stripped
        return [i for i in items if i.rstrip(".").lower() != "none"]

    sections = report["sections"]
    found = [{"kind": "follow-up", "text": t} for t in bullets(sections.get("Follow-ups"))]
    found += [{"kind": "unverified", "text": t} for t in bullets(sections.get("Changed")) if "unverified" in t.lower()]
    return found


def report_ready(report):
    """The agent says its MR can land: open, pipeline passed, "ready to land" in its Summary, no open questions."""
    if not report:
        return False
    fields, sections = report["fields"], report["sections"]
    questions = sections.get("Open questions", "").strip().rstrip(".").lower()
    return (fields.get("state") == "mr-open" and fields.get("pipeline") == "passed"
            and "ready to land" in sections.get("Summary", "").lower() and questions in ("", "none"))


def approval_detail(approval):
    if approval["approved"]:
        return "approved by %s" % ", ".join(approval["by"]) if approval["by"] else "no approval required"
    left = approval.get("left") or 1
    return "needs %d approval%s from someone other than its author" % (left, "" if left == 1 else "s")


def head_landed_how(head, mr_sha, on_default, branch_patch, merged_patch):
    """How the worktree's HEAD landed, or None when it didn't. land rebases an MR that is behind before merging, so
    the merged commit is usually not HEAD; the same patch (`git patch-id --stable`) then shows nothing was lost."""
    if head and head == mr_sha:
        return "same commit"
    if on_default:
        return "on the default branch"
    if branch_patch and branch_patch == merged_patch:
        return "same patch"
    return None


def merged_commit(mr):
    """The commit a merged MR or PR put on its target: GitLab's squash or merge commit, or GitHub's merge commit."""
    return mr.get("squash_commit_sha") or mr.get("merge_commit_sha")


def retire_checks(agent_status, porcelain, mr_state, head_landed, report, worktree_removed=False, approval=None,
                  land_mode="helper", report_expected=True):
    """Every check /herdr:retire reports, in order. The workspace may be retired only when all are ok.

    head_landed: how the worktree's HEAD landed (see head_landed_how), or a falsy value when it did not.
    worktree_removed: the worktree folder is already gone, so nothing local can be lost; the agent's report still gates.
    approval: an open MR's approval state (gitlab_approval's shape); None when there is no open MR to approve.
    land_mode: the profile's retire.land. With "repo" the repository's own process merges and approves, so an open MR
    is waited on, never landed, and its approval is not this helper's check.
    report_expected: false when the agent got no brief (spawn.brief false) and so keeps no status report; a report it
    wrote anyway still gates.
    """
    fields = report["fields"] if report else {}
    questions = report["sections"].get("Open questions", "") if report else ""
    open_questions = questions.strip().rstrip(".").lower() not in ("", "none")

    def check(name, ok, detail):
        return {"check": name, "ok": ok, "detail": detail}

    by_repo = land_mode == "repo"

    if worktree_removed:
        local = [
            check("worktree", True, "already removed"),
            check("merged", True, "worktree already removed; MR is %s" % (mr_state or "not found")),
            check("head", True, "worktree already removed"),
        ]
    else:
        local = [
            check("worktree", not porcelain.strip(), "clean" if not porcelain.strip() else "uncommitted:\n" + porcelain.rstrip()),
            check("merged", mr_state == "merged",
                  "MR is opened; the repository's own process merges it (retire.land: repo), so retire waits for that"
                  if by_repo and mr_state == "opened" else "MR is %s" % (mr_state or "not found")),
            check(
                "head",
                bool(head_landed),
                ("HEAD landed: %s" % head_landed if isinstance(head_landed, str) else
                 "HEAD is the merged commit or on origin/main") if head_landed else
                "HEAD has commits the merge did not take",
            ),
        ]
    if by_repo:
        local.append(check("approved", True, "approval belongs to the repository's own process (retire.land: repo)"))
    elif approval is None:
        local.append(check("approved", True, "no open MR to approve"))
    else:
        local.append(check("approved", approval["approved"], approval_detail(approval)))
    agent = [check("agent", agent_status in ("idle", "done"), "agent is %s" % agent_status)]
    if report is None and not report_expected:
        return agent + local + [
            check("report", True, "no status report expected: the agent got no brief (spawn.brief: false)"),
            check("safe_to_retire", True, "not reported; the worktree, merged and head checks decide"),
            check("open_questions", True, "none reported"),
        ]
    return agent + local + [
        check("report", report is not None, "status report present" if report else "no status report"),
        check(
            "safe_to_retire",
            fields.get("safe_to_retire") == "yes",
            "agent says safe_to_retire: %s" % fields.get("safe_to_retire", "unknown"),
        ),
        check("open_questions", not open_questions, questions if open_questions else "none"),
    ]


WAITING_PIPELINE = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
WAITING_MERGE_STATUS = {"checking", "unchecked", "ci_still_running", "preparing", "approvals_syncing"}

# ---- per-project profiles ----------------------------------------------------------------------------------------
# The flow is the same for every project; what differs (forge, tracker, worktree commands, dev server, restart
# triggers, how an agent implements and opens its MR) comes from the repo's own profile file, a personal overlay laid
# over it (for what belongs to one person, such as their planning page), and these defaults.

REPO_PROFILE = os.path.join(".claude", "herdr-profile.json")
OVERLAY_DIR = os.path.expanduser("~/.config/herdr/profiles")
PROFILE_KEYS = {"name", "match", "forge", "tracker", "worktree", "dev_server", "restart_on", "brief", "planning",
                "mirrors", "spawn", "retire"}
# Who does a step: "helper" is this plugin's own flow (the default); "repo" is the repository's own process.
STEP_OWNERS = ("helper", "repo")
SPAWN_KEYS = {"command", "agent", "brief"}
RETIRE_KEYS = {"land", "branch_cleanup"}
MIRROR_BACKUPS = os.path.expanduser("~/.cache/herdr/mirror-backups")
DEFAULT_RESTART_ON = [
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "**/package.json",
    "go.mod", "go.sum", "requirements*.txt", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock",
    "Gemfile.lock", "**/*.csproj", "Directory.Packages.props", "Directory.Build.props", "**/packages.lock.json",
    "Cargo.lock", "docker-compose*.yml", "compose*.yml", "**/.env.example", ".nvmrc", ".tool-versions",
    ".python-version",
]
MR_CREATE = {"gitlab": "glab mr create", "github": "gh pr create"}


def forge_from_remote(url):
    """gitlab or github from the origin URL; None when the host says neither (set `forge` in a profile)."""
    host = re.sub(r"^[a-z+]+://", "", url or "").split("@")[-1].split("/")[0].split(":")[0].lower()
    if "gitlab" in host:
        return "gitlab"
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    return None


def select_profile(profiles, main_checkout, remote_url):
    """The profile matching this repo by origin URL or main-checkout path, or None."""
    for profile in profiles:
        match = profile.get("match", {})
        if match.get("remote") and match["remote"] in (remote_url or ""):
            return profile
        if match.get("path") and os.path.normpath(match["path"]) == os.path.normpath(main_checkout):
            return profile
    return None


def resolve_profile(profile, remote_url):
    """A profile with every field filled: the profile's own values, else the defaults (listed in `defaulted`)."""
    profile = profile or {}
    unknown = set(profile) - PROFILE_KEYS
    if unknown:
        raise HerdrError("Profile %r has unknown keys: %s" % (profile.get("name"), ", ".join(sorted(unknown))))
    forge = profile.get("forge") or forge_from_remote(remote_url)
    defaults = {
        "name": None,
        "forge": forge,
        "tracker": {"kind": "none", "done_path": []},
        "worktree": {"new": None, "remove": None, "env": {}},
        "dev_server": None,
        "planning": None,
        "mirrors": [],
        "restart_on": DEFAULT_RESTART_ON,
        "brief": {"implement": "Plan first and stop for the approval of the person running the session, then work "
                               "test-first.",
                  "mr_create": MR_CREATE.get(forge), "notes": []},
        # A repository with its own launcher or merge process hands those steps to it (see PROFILES.md).
        "spawn": {"command": None, "agent": None, "brief": True},
        "retire": {"land": "helper", "branch_cleanup": "helper"},
    }
    resolved = {"defaulted": [k for k in defaults if k not in profile]}
    for key, default in defaults.items():
        value = profile.get(key, default)
        if isinstance(default, dict) and isinstance(value, dict):
            value = dict(default, **value)
        resolved[key] = value
    check_process_fields(profile, resolved)
    return resolved


def check_process_fields(profile, resolved):
    """Refuse a spawn or retire field the helper would misread: a typo there could quietly fall back to the default
    flow, and the default retire merges."""
    name = profile.get("name")
    for field, keys in (("spawn", SPAWN_KEYS), ("retire", RETIRE_KEYS)):
        if not isinstance(resolved[field], dict):
            raise HerdrError("Profile %r: %s must be an object." % (name, field))
        unknown = set(resolved[field]) - keys
        if unknown:
            raise HerdrError("Profile %r has unknown %s keys: %s" % (name, field, ", ".join(sorted(unknown))))
    for key in sorted(RETIRE_KEYS):
        if resolved["retire"][key] not in STEP_OWNERS:
            raise HerdrError("Profile %r: retire.%s must be %s, not %r." % (
                name, key, " or ".join('"%s"' % o for o in STEP_OWNERS), resolved["retire"][key]))
    spawn = resolved["spawn"]
    if not isinstance(spawn["brief"], bool):
        raise HerdrError("Profile %r: spawn.brief must be true or false." % name)
    if spawn["command"] and not spawn["agent"]:
        raise HerdrError("Profile %r: spawn.command needs spawn.agent, the name of the agent it starts (it may use "
                         "the same {placeholders})." % name)
    if spawn["agent"] and not spawn["command"]:
        raise HerdrError("Profile %r: spawn.agent only names the agent spawn.command starts; set spawn.command too."
                         % name)


PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill_command(template, values):
    """The argv for a profile command: split like a shell would, then each {placeholder} filled inside its own
    argument. A value is never split or read as shell, so a ticket title can't become a second command."""
    def fill(match):
        if values.get(match.group(1)) is None:
            raise HerdrError("%r needs a value for {%s}." % (template, match.group(1)))
        return str(values[match.group(1)])
    return [PLACEHOLDER.sub(fill, token) for token in shlex.split(template)]


def read_json(path):
    with open(path) as handle:
        try:
            return json.load(handle)
        except ValueError as error:
            raise HerdrError("%s is not valid JSON: %s" % (path, error))


def merge_profiles(base, overlay):
    """The overlay's fields over the base's; a field that is an object in both is merged one level deep."""
    merged = dict(base)
    for key, value in overlay.items():
        if key == "match":
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            value = dict(merged[key], **value)
        merged[key] = value
    return merged


def load_profile(main_checkout, remote_url, overlay_dir=OVERLAY_DIR):
    """The repo's profile file with a matching personal overlay laid over it, and where each came from.

    The profile is None when neither exists: the defaults apply."""
    repo_path = os.path.join(main_checkout, REPO_PROFILE)
    profile = read_json(repo_path) if os.path.isfile(repo_path) else None
    sources = {"repo": repo_path if profile is not None else None, "overlay": None}
    if os.path.isdir(overlay_dir):
        for name in sorted(os.listdir(overlay_dir)):
            path = os.path.join(overlay_dir, name)
            if name.endswith(".json") and select_profile([read_json(path)], main_checkout, remote_url):
                repo_retire = (profile or {}).get("retire") or {}
                profile = merge_profiles(profile or {}, read_json(path))
                sources["overlay"] = path
                merged_retire = profile.get("retire") or {}
                for retire, where in ((repo_retire, repo_path), (merged_retire, path)):
                    if not isinstance(retire, dict):
                        raise HerdrError("%s: retire must be an object, not %r." % (where, retire))
                loosened = [k for k in sorted(RETIRE_KEYS) if repo_retire.get(k) == "repo"
                            and (merged_retire.get(k) or "helper") != "repo"]
                if loosened:
                    raise HerdrError(
                        "The overlay %s sets retire.%s back to the helper, but the repo's profile %s hands it to the "
                        "repository's own process. An overlay can add personal fields; it can't loosen the repo's "
                        "process. Remove that field from the overlay." % (path, "/".join(loosened), repo_path))
                break
    return profile, sources


# ---- /herdr:setup: what a repo has, and what it lacks ------------------------------------------------------------
# Each practice the examples/full sample repo shows: why it matters, and the files to copy from it.

PRACTICES = [
    ("worktree", "A worktree command gives each agent its own checkout, bootstrapped, with its own ports, so parallel "
                 "agents never share a dev server or node_modules.",
     ["scripts/worktree.mjs", "scripts/lib/worktree.mjs", "scripts/lib/worktree.test.mjs", "scripts/bootstrap.mjs",
      "scripts/lib/herdr-plugin.mjs", "scripts/lib/herdr-plugin.test.mjs", ".gitignore"]),
    ("agent_lifecycle", "Agents follow the repo's CLAUDE.md: test first, never merge, rebase and hand over, run the "
                        "doc check. Without it each agent improvises its own process.",
     ["CLAUDE.md"]),
    ("stop_hook", "A Stop hook runs the project's checks before an agent may finish, so it can't hand over red work.",
     [".claude/settings.json", ".claude/hooks/verify-on-stop.mjs", "scripts/lib/stop-hook.test.mjs"]),
    ("shared_concepts", "A registry gives each shared rule or value set one owner, and its check fails when another "
                        "file re-lists the values. Parallel agents otherwise each build their own copy.",
     ["docs/shared-concepts.md", "scripts/lib/shared-concepts.mjs", "scripts/lib/shared-concepts.test.mjs"]),
    ("doc_checks", "A doc-parity check fails the build when docs name commands or files that no longer exist, and "
                   "docs-impact lists the docs a change may have made untrue.",
     ["scripts/lib/doc-parity.mjs", "scripts/lib/doc-parity.test.mjs", "scripts/docs-impact.mjs",
      "scripts/lib/docs-impact.mjs", "scripts/lib/docs-impact.test.mjs", "scripts/lib/shared-concepts.mjs"]),
    ("mr_template", "An MR or PR template with a \"Docs reviewed\" section makes the agent record each doc it reread.",
     None),
    ("personal_install", "Bootstrap installs the herdr plugin into each developer's own settings when it is missing, "
                         "never into the project's committed settings.",
     ["scripts/bootstrap.mjs", "scripts/lib/herdr-plugin.mjs", "scripts/lib/herdr-plugin.test.mjs",
      "scripts/lib/worktree.mjs", "scripts/lib/worktree.test.mjs"]),
]
MR_TEMPLATE = {"github": ".github/pull_request_template.md", "gitlab": ".gitlab/merge_request_templates/Default.md"}
MR_TEMPLATE_GLOBS = [".gitlab/merge_request_templates/*.md", ".github/pull_request_template.md",
                     ".github/PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE/*.md",
                     "docs/pull_request_template.md", "pull_request_template.md"]
LOCKFILES = [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun"),
             ("package-lock.json", "npm"), ("package.json", "npm"), ("uv.lock", "uv"), ("poetry.lock", "poetry"),
             ("pyproject.toml", "pip"), ("requirements.txt", "pip"), ("go.mod", "go"), ("Cargo.toml", "cargo")]
RUN_SCRIPT = {"npm": "npm run %s", "pnpm": "pnpm %s", "yarn": "yarn %s", "bun": "bun run %s"}
TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+\b")


def setup_scan(path, remote, history):
    """What /herdr:setup proposes for the repo at PATH: its forge (from the origin URL), a tracker guessed from ticket
    keys in HISTORY (branch names and commit subjects), its package manager, each practice from examples/full with
    whether the repo has it, and a profile from the closest example (None when the repo has one)."""
    def read(name):
        try:
            with open(os.path.join(path, name), encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def files_under(*folders):
        for folder in folders:
            for root, dirs, names in os.walk(os.path.join(path, folder)):
                dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
                for name in names:
                    yield os.path.relpath(os.path.join(root, name), path).replace(os.sep, "/")

    def json_file(name):
        try:
            return json.loads(read(name) or "{}")
        except ValueError:
            return {}

    forge = forge_from_remote(remote)
    keys = {}
    for line in history:
        for key in set(TICKET_KEY.findall(line)):
            keys[key] = keys.get(key, 0) + 1
    project = max(keys, key=lambda k: keys[k]) if keys else None
    tracker = {"kind": "jira", "project": project} if project and keys[project] >= 2 else None
    manager = next((pm for name, pm in LOCKFILES if os.path.isfile(os.path.join(path, name))), None)
    scripts = json_file("package.json").get("scripts") or {}
    settings = json_file(os.path.join(".claude", "settings.json"))
    script_files = list(files_under("scripts", "bin"))
    top_files = [n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n))]
    guide = (read("CLAUDE.md") or "") + (read("AGENTS.md") or "")
    templates = [n for n in list(files_under(".gitlab", ".github", "docs")) + top_files
                 if any(fnmatch.fnmatch(n, g) for g in MR_TEMPLATE_GLOBS)]
    installers = [n for n in script_files + top_files
                  if not n.endswith(".md") and "herdr@herdr-skills" in (read(n) or "")]  # docs only describe it

    present = {
        "worktree": any("worktree" in s for s in scripts) or any("worktree" in n.lower() for n in script_files)
                    or bool(re.search(r"(?m)^worktree[\w-]*:", read("Makefile") or "")),
        "agent_lifecycle": bool(re.search(r"(?im)^#{1,6} .*lifecycle", guide)),
        "stop_hook": bool((settings.get("hooks") or {}).get("Stop")),
        "shared_concepts": os.path.isfile(os.path.join(path, "docs", "shared-concepts.md")),
        "doc_checks": any(s.startswith("docs") for s in scripts)
                      or any(re.search(r"docs?[-_](parity|impact)", n) for n in script_files),
        "mr_template": any("docs reviewed" in (read(n) or "").lower() for n in templates),
        "personal_install": bool(installers),
    }
    evidence = {"mr_template": "templates: %s" % ", ".join(templates) if templates else "no template",
                "personal_install": ", ".join(installers)}
    practices = [{"practice": name, "present": present[name], "why": why,
                  "copy": copy or [MR_TEMPLATE.get(forge or "github", MR_TEMPLATE["github"])],
                  "evidence": evidence.get(name, "")} for name, why, copy in PRACTICES]
    plugins = settings.get("enabledPlugins") or {}
    in_settings = any(k.startswith("herdr@") for k in plugins) or "herdr-skills" in (settings.get(
        "extraKnownMarketplaces") or {})

    profile_path = os.path.join(path, REPO_PROFILE)
    proposed = None
    if not os.path.isfile(profile_path):
        proposed = {"name": os.path.basename(os.path.abspath(path)), "forge": forge}
        if tracker:
            proposed["tracker"] = dict(tracker)
        run = RUN_SCRIPT.get(manager)
        if run and "worktree" in scripts:
            proposed["worktree"] = {"new": run % "worktree new {branch}", "remove": run % "worktree remove {branch}"}
        dev = next((s for s in ("dev", "start") if s in scripts), None)
        if run and dev:
            proposed["dev_server"] = {"command": run % dev}
    return {
        "path": os.path.abspath(path), "remote": remote, "forge": forge, "tracker": tracker,
        "package_manager": manager, "profile": profile_path if proposed is None else None,
        "closest_example": "full" if tracker or forge == "gitlab" else "minimal",
        "proposed_profile": proposed, "plugin_in_committed_settings": in_settings, "practices": practices,
    }


def scan_repo(path):
    """setup_scan for the repo PATH is in, with its origin URL and the ticket keys its branches and commits use."""
    root = repo_root(path)
    history = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")[1].splitlines()
    history += git(root, "log", "-200", "--format=%s")[1].splitlines()
    return setup_scan(root, git(root, "remote", "get-url", "origin")[1], history)


def github_pr_to_mr(pr):
    """A `gh pr view --json` result in the shape land_decision reads (GitLab's merge-request fields)."""
    sha = pr.get("headRefOid")
    checks = pr.get("statusCheckRollup") or []
    pipeline = "success"
    for check in checks:
        if check.get("__typename") == "StatusContext":
            state = check.get("state")
            if state in ("FAILURE", "ERROR"):
                pipeline = "failed"
                break
            if state in ("PENDING", "EXPECTED"):
                pipeline = "running"
        elif check.get("status") != "COMPLETED":
            pipeline = "running"
        elif check.get("conclusion") not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            pipeline = "failed"
            break
    merge_state = {
        "BEHIND": "need_rebase", "CLEAN": "mergeable", "HAS_HOOKS": "mergeable", "UNSTABLE": "mergeable",
        "UNKNOWN": "checking", "BLOCKED": "blocked (reviews or required checks)", "DIRTY": "conflicting",
        "DRAFT": "draft",
    }.get(pr.get("mergeStateStatus"), pr.get("mergeStateStatus"))
    needs_review = pr.get("reviewDecision") in ("REVIEW_REQUIRED", "CHANGES_REQUESTED")
    return {
        "approvals": {"approved": not needs_review, "required": 1 if needs_review else 0,
                      "left": 1 if needs_review else 0, "by": []},
        "iid": pr.get("number"), "web_url": pr.get("url"), "sha": sha,
        "state": {"OPEN": "opened", "MERGED": "merged", "CLOSED": "closed"}.get(pr.get("state"), pr.get("state")),
        "draft": pr.get("isDraft", False),
        "has_conflicts": pr.get("mergeable") == "CONFLICTING",
        "blocking_discussions_resolved": True,
        "detailed_merge_status": merge_state,
        "head_pipeline": {"status": pipeline, "sha": sha},
        "merge_commit_sha": (pr.get("mergeCommit") or {}).get("oid"),
        "source_branch": pr.get("headRefName"),
    }


def gitlab_approval(approvals):
    """GitLab's merge-request approvals in the shape retire and land read: approved, required, left, by."""
    return {
        "approved": bool(approvals.get("approved")) and not approvals.get("approvals_left"),
        "required": approvals.get("approvals_required", 0),
        "left": approvals.get("approvals_left", 0),
        "by": [a.get("user", {}).get("username") for a in approvals.get("approved_by") or []],
    }


MR_URL = re.compile(r"^https?://([^/\s]+)/(.+?)/-/merge_requests/(\d+)(?:[/?#]\S*)?$")
PR_URL = re.compile(r"^https?://(github\.com)/([^/\s]+/[^/\s]+)/pull/(\d+)(?:[/?#]\S*)?$")
MR_SHORT = re.compile(r"^([\w.-]+(?:/[\w.-]+)+)!(\d+)$")


def parse_mr_ref(text):
    """A merge request named by its GitLab URL, its GitHub pull-request URL, or `group/project!iid` (gitlab.com)."""
    text = (text or "").strip()
    match = MR_URL.match(text)
    if match:
        return {"forge": "gitlab", "host": match.group(1), "project": match.group(2), "iid": int(match.group(3))}
    match = PR_URL.match(text)
    if match:
        return {"forge": "github", "host": match.group(1), "project": match.group(2), "iid": int(match.group(3))}
    match = MR_SHORT.match(text)
    if match:
        return {"forge": "gitlab", "host": "gitlab.com", "project": match.group(1), "iid": int(match.group(2))}
    raise HerdrError("%r names no merge request: pass its URL or `group/project!iid`." % text)


def report_mr_ref(report):
    """The first merge request the agent's status report names in its `mr:` line, or None."""
    for word in re.split(r"[\s,;]+", (report or {}).get("fields", {}).get("mr", "") if report else ""):
        try:
            return parse_mr_ref(word)
        except HerdrError:
            continue
    return None


def land_decision(mr):
    """What landing an MR needs next: merge, rebase, wait, done, or stop (with a reason).

    A rebase comes before the pipeline check: on a fast-forward project it starts the pipeline that matters.
    """
    def decide(action, reason=""):
        return {"action": action, "reason": reason}

    if mr.get("state") == "merged":
        return decide("done", "MR is merged")
    if mr.get("state") != "opened":
        return decide("stop", "MR is %s" % mr.get("state"))
    if mr.get("draft") or mr.get("work_in_progress"):
        return decide("stop", "MR is a draft")
    if mr.get("has_conflicts"):
        return decide("stop", "MR has conflicts; the agent must resolve them")
    if not mr.get("blocking_discussions_resolved", True):
        return decide("stop", "MR has unresolved threads")
    status = mr.get("detailed_merge_status")
    # Before a rebase: there is no point waiting for a new pipeline on an MR that can't merge without a reviewer.
    approvals = mr.get("approvals")
    if status == "not_approved" or (approvals and not approvals["approved"]):
        return decide("stop", "MR %s" % approval_detail(approvals or {"approved": False, "left": 1, "by": []}))
    if status == "need_rebase":
        return decide("rebase", "MR is behind the target branch")
    pipeline = mr.get("head_pipeline") or {}
    if pipeline.get("sha") != mr.get("sha"):
        return decide("wait", "no pipeline on the head commit yet")
    if pipeline.get("status") in WAITING_PIPELINE:
        return decide("wait", "head pipeline is %s" % pipeline["status"])
    if pipeline.get("status") != "success":
        return decide("stop", "head pipeline %s" % pipeline.get("status"))
    if status in WAITING_MERGE_STATUS:
        return decide("wait", "GitLab is %s" % status)
    if status != "mergeable":
        return decide("stop", "MR is %s" % status)
    return decide("merge", "pipeline passed on %s" % mr.get("sha", "")[:8])


def json_only_reordered(head_text, work_text):
    """True when a modified JSON file holds exactly HEAD's content (tools rewrite settings files with keys reordered)."""
    try:
        return json.loads(head_text) == json.loads(work_text)
    except ValueError:
        return False


def restart_relevant(paths, globs):
    """Changed paths a running dev server reads only at startup, so it needs a restart to see them.

    A glob without a slash, or one starting with `**/`, matches the file name in any folder; others match the path.
    """
    def matches(path, glob):
        if glob.startswith("**/"):
            glob = glob[3:]
        if "/" not in glob:
            return fnmatch.fnmatch(os.path.basename(path), glob)
        return fnmatch.fnmatch(path, glob)

    return [p for p in paths if any(matches(p, g) for g in globs)]


def facts_changed(paths, planning):
    """Changed paths the planning page summarises (its `facts_on` globs), so its repository facts need a refresh."""
    return restart_relevant(paths, (planning or {}).get("facts_on") or [])


def mirror_text(copy, source, marker):
    """The copy's new text: the source whole, or with a marker, the copy's own lines above the marker line
    followed by the source from its marker line on. The marker must be a whole line in both."""
    if marker is None:
        return source
    copy_lines, source_lines = copy.splitlines(True), source.splitlines(True)
    for name, lines in (("copy", copy_lines), ("source", source_lines)):
        if not any(line.rstrip("\n") == marker for line in lines):
            raise HerdrError("The %s has no line %r." % (name, marker))
    keep = next(i for i, line in enumerate(copy_lines) if line.rstrip("\n") == marker)
    start = next(i for i, line in enumerate(source_lines) if line.rstrip("\n") == marker)
    return "".join(copy_lines[:keep] + source_lines[start:])


def sync_mirrors(main, mirrors, backup_dir=MIRROR_BACKUPS, stamp=None, dry_run=False):
    """Bring each mirrored copy of a repo doc up to date with the main checkout, backing up the old copy first.

    A copy whose marker or file is missing is reported as a problem and left as it is."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    result = {"synced": [], "unchanged": [], "problems": []}
    for mirror in mirrors:
        copy = os.path.expanduser(mirror["copy"])
        source = os.path.join(main, mirror["source"])
        try:
            for path in (source, copy):
                if not os.path.isfile(path):
                    raise HerdrError("%s is missing." % path)
            with open(copy) as handle:
                old = handle.read()
            with open(source) as handle:
                new = mirror_text(old, handle.read(), mirror.get("from"))
        except HerdrError as error:
            result["problems"].append({"copy": copy, "problem": str(error)})
            continue
        if new == old:
            result["unchanged"].append(copy)
            continue
        backup = os.path.join(backup_dir, "%s.%s" % (os.path.basename(copy), stamp))
        if not dry_run:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copyfile(copy, backup)
            with open(copy, "w") as handle:
                handle.write(new)
        result["synced"].append({"copy": copy, "backup": None if dry_run else backup})
    return result


def find_retire_target(workspaces, agents, target, own_workspace):
    """The linked-worktree workspace named by an agent name, workspace id or label. Never a main checkout or our own."""
    by_agent = {a["name"]: a.get("workspace_id") for a in agents if a.get("name")}
    wanted = by_agent.get(target, target)
    ws = next((w for w in workspaces if w["workspace_id"] == wanted or w.get("label") == target), None)
    if ws is None:
        raise HerdrError("No workspace or agent named %r." % target)
    if ws["workspace_id"] == own_workspace:
        raise HerdrError("%s is the workspace this session runs in; retire it from another one." % ws["workspace_id"])
    worktree = ws.get("worktree")
    if not worktree:
        raise HerdrError("Workspace %r has no worktree to retire." % ws.get("label"))
    if not worktree.get("is_linked_worktree", False):
        raise HerdrError("Workspace %r is on the repo's main checkout; only linked worktrees are retired." % ws.get("label"))
    return ws


# ---- herdr / git I/O ---------------------------------------------------------------------------------------------


def herdr(*args, parse=True):
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise HerdrError((proc.stderr or proc.stdout).strip() or "herdr %s failed" % " ".join(args))
    if not parse:
        return proc.stdout
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise HerdrError("`herdr %s` output was not JSON: %s" % (" ".join(args[:2]), proc.stdout.strip()[:200] or "(empty)"))


def repo_root(path):
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise HerdrError("%s is not inside a git repository" % path)
    return repo_root_from_common_dir(proc.stdout.strip())


def workspaces():
    return herdr("workspace", "list")["result"]["workspaces"]


def agent_status(target):
    return herdr("agent", "get", target)["result"]["agent"]["agent_status"]


PARENT_WAIT_SECONDS = 5


def parent_workspace(path, create=False, wait_seconds=PARENT_WAIT_SECONDS, sleep=time.sleep):
    """The workspace on PATH's repo main checkout; with create, open one when there is none.

    A created workspace is identified by the id `workspace create` returns: it gets no worktree info until a worktree
    is opened from it. If it is not listed within wait_seconds it is closed again, so a failure leaves nothing behind.
    """
    root = repo_root(path)
    ws = find_parent_workspace(workspaces(), root, herdr("pane", "list")["result"]["panes"])
    if ws is not None:
        return ws
    if not create:
        raise HerdrError("No workspace is open on %s; re-run with --create to open one." % root)
    created = herdr("workspace", "create", "--cwd", root, "--label", os.path.basename(root), "--no-focus")["result"]
    ws_id = (created.get("workspace") or {}).get("workspace_id")
    if not ws_id:
        raise HerdrError("`herdr workspace create --cwd %s` returned no workspace id; check `herdr workspace list` "
                         "for a workspace labelled %r." % (root, os.path.basename(root)))
    interval = 0.25
    for _ in range(int(wait_seconds / interval) + 1):
        listed = [w for w in workspaces() if w["workspace_id"] == ws_id]
        if listed:
            return listed[0]
        sleep(interval)
    try:
        herdr("workspace", "close", ws_id, parse=False)
    except HerdrError:
        pass
    raise HerdrError("Created workspace %s on %s, but it did not appear in `herdr workspace list` within %gs; "
                     "closed it." % (ws_id, root, wait_seconds))


def git(path, *args):
    proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def patch_id(path, diff_args):
    """`git patch-id --stable` of a diff, or None when the diff fails or is empty. Bytes throughout: a patch need not
    be valid text in the locale's encoding."""
    diff = subprocess.run(["git", "-C", path, *diff_args], capture_output=True)
    if diff.returncode != 0 or not diff.stdout.strip():
        return None
    out = subprocess.run(["git", "-C", path, "patch-id", "--stable"], input=diff.stdout, capture_output=True).stdout
    return out.split()[0].decode() if out.split() else None


def branch_patch_id(path, default):
    """The patch of everything the branch at PATH adds since it left DEFAULT."""
    code, base = git(path, "merge-base", "HEAD", default)
    return patch_id(path, ["diff", "--no-color", base, "HEAD"]) if code == 0 else None


def commit_patch_id(path, commit):
    """The patch COMMIT applied to its first parent: a squash commit's whole change, or a merge commit's branch."""
    return patch_id(path, ["diff", "--no-color", commit + "^1", commit])


def project_profile(main_checkout):
    """The resolved profile for the repo whose main checkout this is (see resolve_profile)."""
    remote = git(main_checkout, "remote", "get-url", "origin")[1]
    profile, sources = load_profile(main_checkout, remote)
    resolved = resolve_profile(profile, remote)
    resolved["main_checkout"], resolved["remote"], resolved["sources"] = main_checkout, remote, sources
    return resolved


def run_json(args, cwd):
    proc = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise HerdrError("%s failed: %s" % (" ".join(args[:3]), (proc.stderr or proc.stdout).strip()))
    return json.loads(proc.stdout or "null")


class GitLabForge:
    """Merge requests through glab. Without a project it runs in the main checkout, so `:fullpath` resolves to the
    checkout's project; with one (an MR named by URL, maybe in another repo) it names the project and host itself."""

    def __init__(self, main_checkout, project=None, host=None):
        self.main, self.host = main_checkout, host
        self.base = "projects/%s/merge_requests" % (quote(project, safe="") if project else ":fullpath")

    def api(self, path, method="GET", fields=()):
        args = ["glab", "api", "-X", method, path]
        if self.host:
            args += ["--hostname", self.host]
        for key, value in fields:
            args += ["-f", "%s=%s" % (key, value)]
        return run_json(args, self.main) or {}

    def find(self, branch):
        """The branch's merge request (the merged one if several), or None."""
        mrs = run_json(["glab", "mr", "list", "--source-branch", branch, "--all", "-F", "json"], self.main) or []
        mrs.sort(key=lambda m: (m.get("state") != "merged", -m.get("iid", 0)))
        return {"iid": mrs[0]["iid"], "state": mrs[0]["state"], "url": mrs[0]["web_url"], "sha": mrs[0].get("sha")} if mrs else None

    def view(self, iid):
        mr = self.api("%s/%d?include_rebase_in_progress=true" % (self.base, iid))
        mr["approvals"] = gitlab_approval(self.api("%s/%d/approvals" % (self.base, iid)))
        return mr, bool(mr.get("rebase_in_progress"))

    def rebase(self, iid):
        self.api("%s/%d/rebase" % (self.base, iid), "PUT")

    def merge(self, iid, sha, branch):
        """Squash-merge pinned to SHA; GitLab deletes the source branch itself, so `branch` is unused."""
        merged = self.api("%s/%d/merge" % (self.base, iid), "PUT",
                          [("sha", sha), ("squash", "true"), ("should_remove_source_branch", "true")])
        return {"state": merged.get("state"), "commit": merged.get("squash_commit_sha") or merged.get("merge_commit_sha"),
                "url": merged.get("web_url")}


GITHUB_RETRY_SECONDS = 3  # the wait before retrying a GitHub call that failed right after a merge


class GitHubForge:
    """Pull requests through gh, normalized to the merge-request shape land_decision reads. With a repo (a PR named by
    URL) every call names it; without one gh resolves it from the main checkout."""

    FIELDS = ("number,state,url,headRefOid,isDraft,mergeable,mergeStateStatus,statusCheckRollup,reviewDecision,"
              "mergeCommit,headRefName")

    def __init__(self, main_checkout, repo=None, sleep=time.sleep):
        self.main = main_checkout
        self.repo_args = ["--repo", repo] if repo else []
        self.sleep = sleep

    def find(self, branch):
        prs = run_json(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state,url,headRefOid"]
                       + self.repo_args, self.main) or []
        prs.sort(key=lambda p: (p.get("state") != "MERGED", -p.get("number", 0)))
        if not prs:
            return None
        mr = github_pr_to_mr(prs[0])
        return {"iid": mr["iid"], "state": mr["state"], "url": mr["web_url"], "sha": mr["sha"]}

    def view(self, iid):
        return github_pr_to_mr(run_json(["gh", "pr", "view", str(iid), "--json", self.FIELDS] + self.repo_args,
                                        self.main)), False

    def rebase(self, iid):
        subprocess.run(["gh", "pr", "update-branch", str(iid), "--rebase"] + self.repo_args, capture_output=True,
                       text=True, cwd=self.main)

    def merge(self, iid, sha, branch):
        """Squash-merge pinned to SHA, then delete only the remote BRANCH (the PR's head as read before the merge), as
        GitLab's should_remove_source_branch does. Not gh's --delete-branch: it also deletes the local branch and the
        agent's worktree, which retire removes. Once gh pr merge succeeds nothing raises: the merge happened."""
        proc = subprocess.run(["gh", "pr", "merge", str(iid), "--squash", "--match-head-commit", sha] + self.repo_args,
                              capture_output=True, text=True, cwd=self.main)
        if proc.returncode != 0:
            raise HerdrError("gh pr merge failed: %s" % (proc.stderr or proc.stdout).strip())
        self.delete_remote_branch(branch)
        pr = self.read_merged(iid)
        return {"state": {"MERGED": "merged"}.get(pr.get("state"), pr.get("state")),
                "commit": (pr.get("mergeCommit") or {}).get("oid"), "url": pr.get("url")}

    def read_merged(self, iid):
        """The merged PR's state, url and merge commit, read once more after a short wait if the first read fails. A
        last failure is a warning, and the PR counts as merged with its commit unknown."""
        for attempt in range(2):
            try:
                return run_json(["gh", "pr", "view", str(iid), "--json", "state,url,mergeCommit"] + self.repo_args,
                                self.main) or {}
            except HerdrError as error:
                failure = error
                if attempt == 0:
                    self.sleep(GITHUB_RETRY_SECONDS)
        print("warning: merged, but could not read the merge commit of #%d: %s" % (iid, failure), file=sys.stderr,
              flush=True)
        return {"state": "MERGED"}

    def delete_remote_branch(self, branch):
        """A failed delete is retried once after a short wait, unless the branch is already gone (the repo's auto-delete
        may have removed it). The same 422 can mean either, so the ref is checked. A last failure is a warning: the
        merge happened."""
        ref = "repos/%s/git/refs/heads/%s" % (self.api_repo(), quote(branch))
        for attempt in range(2):
            proc = subprocess.run(["gh", "api", "-X", "DELETE", ref], capture_output=True, text=True, cwd=self.main)
            if proc.returncode == 0 or self.remote_branch_gone(branch):
                return
            if attempt == 0:
                self.sleep(GITHUB_RETRY_SECONDS)
        print("warning: merged, but could not delete the remote branch %s: %s"
              % (branch, (proc.stderr or proc.stdout).strip()), file=sys.stderr, flush=True)

    def remote_branch_gone(self, branch):
        """Only a 404 means gone; a failed check leaves the branch possibly there."""
        proc = subprocess.run(["gh", "api", "repos/%s/git/ref/heads/%s" % (self.api_repo(), quote(branch))],
                              capture_output=True, text=True, cwd=self.main)
        return proc.returncode != 0 and "(HTTP 404)" in proc.stderr

    def api_repo(self):
        return self.repo_args[1] if self.repo_args else "{owner}/{repo}"


def forge_for(profile):
    forges = {"gitlab": GitLabForge, "github": GitHubForge}
    if profile["forge"] not in forges:
        raise HerdrError("No forge for %s (origin %s); set \"forge\" in its profile." % (profile["main_checkout"], profile["remote"]))
    return forges[profile["forge"]](profile["main_checkout"])


def forge_for_ref(ref, cwd):
    """A forge bound to the project a parsed MR reference names (see parse_mr_ref), usable from any directory."""
    if ref["forge"] == "github":
        return GitHubForge(cwd, ref["project"])
    return GitLabForge(cwd, ref["project"], ref["host"])


def resolve_mr(forge, branch, report, cwd, forge_factory=None):
    """The agent's merge request and a forge for it: its worktree branch's, else the first one its status report
    names. The second may live in another repo (an agent working in one checkout can open its MR in another), so it
    gets a forge bound to that project. (None, forge) when neither has one."""
    mr = forge.find(branch) if branch else None
    if mr:
        return mr, forge
    ref = report_mr_ref(report)
    if not ref:
        return None, forge
    other = (forge_factory or forge_for_ref)(ref, cwd)
    viewed = other.view(ref["iid"])[0]
    return {"iid": ref["iid"], "state": viewed.get("state"), "url": viewed.get("web_url"), "sha": viewed.get("sha"),
            "ref": "%s!%d" % (ref["project"], ref["iid"]) if ref["forge"] == "gitlab" else viewed.get("web_url")}, other


def read_report(agent):
    path = status_report_path(agent) if agent else None
    if not path or not os.path.exists(path):
        return path, None
    with open(path) as handle:
        return path, parse_status_report(handle.read())


def retire_info(target):
    """Everything /herdr:retire needs to decide, as one JSON-ready dict."""
    agents = herdr("agent", "list")["result"]["agents"]
    ws = find_retire_target(workspaces(), agents, target, os.environ.get("HERDR_WORKSPACE_ID"))
    path = ws["worktree"]["checkout_path"]
    agent = next((a for a in agents if a.get("workspace_id") == ws["workspace_id"]), None)
    name = agent.get("name") if agent else None
    report_path, report = read_report(name)
    reported_branch = report["fields"].get("branch", "") if report else ""
    removed = not os.path.isdir(path)
    if removed:
        # The folder is gone (the agent may have cleaned up after itself): Herdr still records the main checkout.
        main_checkout = ws["worktree"]["repo_root"]
        branch, head, porcelain, head_landed = reported_branch, None, None, None
    else:
        main_checkout = repo_root(path)
        _, branch = git(path, "branch", "--show-current")
        _, head = git(path, "rev-parse", "HEAD")
        branch = branch or reported_branch  # detached HEAD: trust the branch the agent reported
        _, porcelain = git(path, "status", "--porcelain")
    profile = project_profile(main_checkout)
    looks_like_branch = bool(branch) and " " not in branch
    mr, forge = resolve_mr(forge_for(profile), branch if looks_like_branch else None, report, main_checkout)
    viewed = forge.view(mr["iid"])[0] if mr and mr["state"] in ("opened", "merged") else {}
    by_repo = profile["retire"]["land"] == "repo"
    approval = viewed["approvals"] if mr and mr["state"] == "opened" and not by_repo else None
    if not removed:
        default = git(main_checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")[1] or "origin/main"
        on_default = git(path, "merge-base", "--is-ancestor", "HEAD", default)[0] == 0
        branch_patch = merged_patch = None
        merged = merged_commit(viewed)
        if not on_default and merged and head != (mr or {}).get("sha"):
            if git(path, "cat-file", "-e", merged + "^{commit}")[0] != 0:
                git(main_checkout, "fetch", "-q", "origin", merged)
            branch_patch, merged_patch = branch_patch_id(path, default), commit_patch_id(path, merged)
        head_landed = head_landed_how(head, (mr or {}).get("sha"), on_default, branch_patch, merged_patch)
    return {
        "profile": profile["name"] or "(defaults)",
        "forge": profile["forge"],
        "workspace_id": ws["workspace_id"],
        "label": ws.get("label"),
        "agent": name or (agent["pane_id"] if agent else None),
        "agent_status": agent.get("agent_status", "unknown") if agent else "gone",
        "worktree": path,
        "worktree_removed": removed,
        "main_checkout": main_checkout,
        "branch": branch or None,
        "head": head,
        "mr": {"iid": mr["iid"], "state": mr["state"], "url": mr["url"], "ref": mr.get("ref")} if mr else None,
        "report_path": report_path,
        "report": report,
        "followups": report_followups(report),
        "land": profile["retire"]["land"],
        "branch_cleanup": profile["retire"]["branch_cleanup"],
        "worktree_remove": profile["worktree"].get("remove"),
        "checks": retire_checks(
            agent.get("agent_status", "unknown") if agent else "gone",
            porcelain, mr["state"] if mr else None, head_landed, report, worktree_removed=removed, approval=approval,
            land_mode=profile["retire"]["land"], report_expected=profile["spawn"]["brief"],
        ),
    }


def land_loop(forge, iid, timeout_seconds, poll_seconds=20, sleep=time.sleep, max_errors=5):
    """Poll the MR until it can merge; rebase once per head when it is behind; merge pinned to the head commit.

    A forge error while polling (a network blip, a 5xx) is retried up to `max_errors` times in a row.
    """
    deadline = time.time() + timeout_seconds
    rebased_from, errors = None, 0
    while True:
        try:
            mr, rebasing = forge.view(iid)
            errors = 0
        except HerdrError as error:
            errors += 1
            if errors >= max_errors:
                raise
            mr, rebasing = None, False
            decision = {"action": "wait", "reason": "forge error, retrying: %s" % str(error).splitlines()[-1][:120]}
        if mr is not None:
            decision = {"action": "wait", "reason": "rebase in progress"} if rebasing else land_decision(mr)
            if decision["action"] == "done":
                return {"iid": iid, "state": "merged", "url": mr.get("web_url"), "already_merged": True}
            if decision["action"] == "stop":
                raise HerdrError("Not landing !%d: %s." % (iid, decision["reason"]))
            if decision["action"] == "merge":
                merged = forge.merge(iid, mr["sha"], mr.get("source_branch"))
                return dict(merged, iid=iid, url=merged.get("url") or mr.get("web_url"),
                            rebased=rebased_from is not None)
            if decision["action"] == "rebase" and rebased_from != mr["sha"]:
                forge.rebase(iid)
                rebased_from = mr["sha"]
        if time.time() > deadline:
            raise HerdrError("Gave up on !%d after %d minutes: %s." % (iid, timeout_seconds // 60, decision["reason"]))
        print("!%d: %s" % (iid, decision["reason"]), file=sys.stderr, flush=True)
        sleep(poll_seconds)


def agent_profile(target):
    """The resolved profile of the repo an agent's worktree belongs to (the agent named as retire-info takes it)."""
    agents = herdr("agent", "list")["result"]["agents"]
    ws = find_retire_target(workspaces(), agents, target, os.environ.get("HERDR_WORKSPACE_ID"))
    path = ws["worktree"]["checkout_path"]
    return project_profile(repo_root(path) if os.path.isdir(path) else ws["worktree"]["repo_root"])


def cwd_profile():
    """The resolved profile of the repo the helper runs in, or None outside a git repository. Only "not a repository"
    means no profile: a profile that can't be read or fails validation raises, so `land --mr` stops rather than
    merging past a repo whose file was meant to forbid it."""
    try:
        main = repo_root(os.getcwd())
    except HerdrError:
        return None
    return project_profile(main)


def refuse_repo_land(profile, what):
    """Refuse to merge for a repo whose profile hands merging to its own process (retire.land: repo). Checked before any
    forge call, so no rebase, merge or branch delete can happen on that repo's MRs from here."""
    if profile and profile["retire"]["land"] == "repo":
        source = (profile.get("sources") or {}).get("overlay") or (profile.get("sources") or {}).get("repo")
        raise HerdrError(
            "Not landing %s: %s merges through its own process (retire.land: repo%s), so this helper never merges, "
            "rebases or deletes branches there. Land it the repository's way, then run /herdr:retire to clean up."
            % (what, profile.get("main_checkout") or "this repo", ", in %s" % source if source else ""))


def land(target, timeout_minutes=30, poll_seconds=20, mr=None):
    """Merge a validated MR or PR: rebase when it is behind, wait for the head pipeline, then squash-merge pinned to that
    commit. The MR is the agent's (its worktree branch's, else the one its status report names) or the one `mr` names
    by URL or `group/project!iid`, which may live in any repo.

    Refused for a repo whose profile sets retire.land to "repo": the agent's repo, or for --mr the repo the helper runs
    in. Two routes can't be traced to a profile: --mr run from outside that repo's checkout, and an agent whose status
    report names an MR in another repo (only the agent's own repo is checked). Name such an MR from its checkout."""
    if mr is not None:
        refuse_repo_land(cwd_profile(), mr)
        ref = parse_mr_ref(mr)
        return land_loop(forge_for_ref(ref, os.getcwd()), ref["iid"], timeout_minutes * 60, poll_seconds)
    if not target:
        raise HerdrError("Name the agent whose MR to land, or the MR itself with --mr <url or group/project!iid>.")
    refuse_repo_land(agent_profile(target), "%s's MR" % target)
    info = retire_info(target)
    if not info["mr"]:
        raise HerdrError("No merge request found for %s, and the agent's status report names none; pass it with "
                         "--mr <url or group/project!iid>." % info["branch"])
    ref = info["mr"]["ref"] and parse_mr_ref(info["mr"]["ref"])
    forge = forge_for_ref(ref, info["main_checkout"]) if ref else forge_for(project_profile(info["main_checkout"]))
    return land_loop(forge, info["mr"]["iid"], timeout_minutes * 60, poll_seconds)


REBASE_REQUEST = (
    "Your MR now conflicts with its target branch after other merges. Rebase on the target branch, resolve the "
    "conflicts keeping both sides, rerun the checks your brief names, force-push with lease, wait for the pipeline, "
    "and rewrite your status report (state mr-open, pipeline passed, 'ready to land')."
)


def watch_loop(agents, land_fn, ask_fn, report_fn, max_seconds, poll_seconds=120, sleep=time.sleep, now=time.time):
    """Land the first agent whose report says it is ready, then return. A conflicting MR's agent is asked once to
    rebase; any other refusal (a running pipeline, a missing approval) is retried on the next poll."""
    asked = set()
    deadline = now() + max_seconds
    while now() < deadline:
        for agent in agents:
            if not report_ready(report_fn(agent)):
                continue
            try:
                return {"landed": agent, "result": land_fn(agent)}
            except HerdrError as error:
                print("%s: %s" % (agent, str(error).splitlines()[-1]), file=sys.stderr, flush=True)
                if "conflicts" in str(error) and agent not in asked:
                    ask_fn(agent, REBASE_REQUEST)
                    asked.add(agent)
        sleep(poll_seconds)
    raise HerdrError("Nothing landed in %.1f hours." % (max_seconds / 3600.0))


def refuse_repo_land_all(agents):
    """Refuse a batch before landing any of it when one agent's repo merges through its own process."""
    for agent in agents:
        refuse_repo_land(agent_profile(agent), "%s's MR" % agent)


def watch(agents, max_hours=4.0, poll_seconds=120):
    refuse_repo_land_all(agents)
    return watch_loop(
        agents, land, lambda agent, message: herdr("agent", "prompt", agent, message, parse=False),
        lambda agent: read_report(agent)[1], max_hours * 3600, poll_seconds)


def land_queue(agents, land_fn, ask_fn, max_seconds, poll_seconds=300, sleep=time.sleep, now=time.time):
    """Land each agent's MR in the given order. A conflicting MR's agent is asked once to rebase and retried after the
    others until the deadline; any other refusal fails that agent. One result per agent, in the given order."""
    results, pending, asked = {}, list(agents), set()
    deadline = now() + max_seconds
    while pending:
        for agent in list(pending):
            try:
                landed = land_fn(agent)
            except HerdrError as error:
                reason = str(error).splitlines()[-1]
                if "conflicts" not in reason:
                    results[agent] = {"agent": agent, "result": "failed", "detail": reason}
                    pending.remove(agent)
                    continue
                if agent not in asked:
                    ask_fn(agent, REBASE_REQUEST)
                    asked.add(agent)
                results[agent] = {"agent": agent, "result": "conflict",
                                  "detail": "asked to rebase; still conflicts: %s" % reason}
                continue
            commit = (landed.get("commit") or "")[:8]
            results[agent] = {"agent": agent, "result": "merged", "detail": "!%s %s%s" % (
                landed.get("iid"), "was already merged" if landed.get("already_merged") else "merged",
                " as " + commit if commit else "")}
            pending.remove(agent)
        if not pending or now() >= deadline:
            break
        print("waiting on %s to rebase" % ", ".join(pending), file=sys.stderr, flush=True)
        sleep(poll_seconds)
    return [results[agent] for agent in agents]


def format_land_results(results):
    return "\n".join("%s: %s (%s)" % (r["agent"], r["result"], r["detail"]) for r in results)


def land_several(agents, timeout_minutes=30, max_hours=3.0, poll_seconds=300):
    refuse_repo_land_all(agents)
    return land_queue(
        agents, lambda agent: land(agent, timeout_minutes),
        lambda agent, message: herdr("agent", "prompt", agent, message, parse=False), max_hours * 3600, poll_seconds)


def update_main(path):
    """Fast-forward the main checkout to its remote default branch. Files that differ from HEAD only by JSON key
    order are restored first; any other local change stops the update."""
    main = repo_root(path)
    if git(main, "fetch", "-q", "origin")[0] != 0:
        raise HerdrError("git fetch failed in %s" % main)
    default = git(main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")[1] or "origin/main"
    branch = git(main, "branch", "--show-current")[1]
    if "origin/" + branch != default:
        raise HerdrError("%s is on %s, not %s; not updating it." % (main, branch or "a detached HEAD", default))
    restored, blocking = [], []
    # Not git(): its strip() would eat the leading space of the first porcelain line's status column.
    porcelain = subprocess.run(["git", "-C", main, "status", "--porcelain"], capture_output=True, text=True).stdout
    for line in porcelain.splitlines():
        status, name = line[:2], line[3:]
        if status.strip() == "M" and name.endswith(".json"):
            head = subprocess.run(["git", "-C", main, "show", "HEAD:" + name], capture_output=True, text=True).stdout
            with open(os.path.join(main, name)) as handle:
                if json_only_reordered(head, handle.read()):
                    restored.append(name)
                    continue
        blocking.append(line)
    if blocking:
        raise HerdrError("Local changes in %s; not updating it:\n%s" % (main, "\n".join(blocking)))
    for name in restored:
        git(main, "restore", name)
    old = git(main, "rev-parse", "HEAD")[1]
    code, out = git(main, "merge", "--ff-only", "-q", default)
    if code != 0:
        raise HerdrError("Can't fast-forward %s to %s: %s" % (main, default, out))
    new = git(main, "rev-parse", "HEAD")[1]
    changed = git(main, "diff", "--name-only", old, new)[1].splitlines() if old != new else []
    profile = project_profile(main)
    return {"main_checkout": main, "from": old, "to": new, "commits": len(git(main, "rev-list", old + ".." + new)[1].split()),
            "restored": restored, "restart_needed": restart_relevant(changed, profile["restart_on"]),
            "facts_changed": facts_changed(changed, profile["planning"])}


# A dev server prints one of these when it is ready to serve; a profile's dev_server.ready overrides it.
GENERIC_SERVER_READY = r"(?i)(listening on|ready (in|on)|started server|running on|local:\s+http|waiting on|now listening)"


def main_server_state_path(main):
    return os.path.join(os.path.expanduser("~"), ".cache", "herdr", "main-server", os.path.basename(main) + ".json")


def main_server_pane(main):
    """The pane recorded as running this repo's main dev server, if it still exists."""
    state_path = main_server_state_path(main)
    if not os.path.exists(state_path):
        return None
    with open(state_path) as handle:
        pane = json.load(handle).get("pane_id")
    try:
        herdr("pane", "get", pane)
    except HerdrError:
        return None
    return pane


def main_server(path, action, command=None, timeout_seconds=180):
    """Run the main checkout's dev server in a Herdr pane labelled "main dev server", so it can be restarted after a
    merge. The command and its ready line come from the project's profile (`dev_server`); --command overrides."""
    main = repo_root(path)
    pane = main_server_pane(main)
    server = project_profile(main)["dev_server"] or {}
    command = command or server.get("command")
    if action == "status":
        return {"main_checkout": main, "pane_id": pane, "running_in_herdr": pane is not None, "command": command}
    if not command:
        raise HerdrError("No dev server is configured for %s: add dev_server to its profile or pass --command." % main)
    ready_pattern = server.get("ready") or GENERIC_SERVER_READY
    if pane is None:
        ws = parent_workspace(main, create=True)
        anchor = herdr("pane", "list", "--workspace", ws["workspace_id"])["result"]["panes"][0]["pane_id"]
        pane = herdr("pane", "split", anchor, "--direction", "down", "--ratio", "0.3", "--cwd", main, "--no-focus")[
            "result"]["pane"]["pane_id"]
        herdr("pane", "rename", pane, "main dev server", parse=False)
        os.makedirs(os.path.dirname(main_server_state_path(main)), exist_ok=True)
        with open(main_server_state_path(main), "w") as handle:
            json.dump({"pane_id": pane, "command": command}, handle)
    elif action == "restart":
        herdr("pane", "send-keys", pane, "ctrl+c", parse=False)
        time.sleep(2)
    herdr("pane", "run", pane, command, parse=False)
    ready = subprocess.run(
        ["herdr", "pane", "wait-output", pane, "--regex", ready_pattern, "--source", "recent",
         "--timeout", str(timeout_seconds * 1000)],
        capture_output=True, text=True,
    ).returncode == 0
    return {"main_checkout": main, "pane_id": pane, "command": command, "ready": ready}


def prompt_and_confirm(name, prompt, sleep=time.sleep, timeout_seconds=30, poll_seconds=2):
    """Send the prompt, then wait (bounded) for the agent to start working or ask something. Returns its status, which
    is `idle` if the agent finished a short prompt before the first read; the caller reports `prompted` to settle that."""
    herdr("agent", "prompt", name, prompt)
    status = agent_status(name)
    for _ in range(int(timeout_seconds / poll_seconds)):
        if status in ("working", "blocked"):
            break
        sleep(poll_seconds)
        status = agent_status(name)
    return status


def pending_prompt_path(agent):
    """Where launch keeps a prompt it could not send because the agent stopped at the folder-trust prompt."""
    return os.path.join(os.path.expanduser("~"), ".cache", "herdr", "pending-prompt", agent + ".txt")


def send_prompt(name, prompt=None, timeout_seconds=600, poll_seconds=3, sleep=time.sleep):
    """Finish a launch that stopped at the folder-trust prompt: wait (bounded) until the prompt is answered and the
    agent is idle, then send the prompt (by default the one launch saved) and confirm the agent started working."""
    saved = pending_prompt_path(name)
    if prompt is None:
        if not os.path.exists(saved):
            raise HerdrError("No saved prompt for %s (%s); pass --prompt or --prompt-file." % (name, saved))
        with open(saved) as handle:
            prompt = handle.read()
    polls = int(timeout_seconds / poll_seconds)
    for attempt in range(polls + 1):
        screen = herdr("agent", "read", name, "--source", "visible", "--lines", "60", parse=False)
        status = agent_status(name)
        if not has_trust_prompt(screen) and status == "idle":
            break
        if attempt == polls:
            raise HerdrError(
                "Waited %gs for %s to clear the folder-trust prompt and go idle (status: %s, trust prompt %s); the "
                "prompt was not sent%s." % (timeout_seconds, name, status,
                                             "showing" if has_trust_prompt(screen) else "gone",
                                             ", it is still saved at " + saved if os.path.exists(saved) else ""))
        sleep(poll_seconds)
    status = prompt_and_confirm(name, prompt, sleep)
    if os.path.exists(saved):
        os.remove(saved)
    return {"agent": name, "status": status, "prompted": True}


# A just-created workspace's pane may not have its shell yet; herdr then refuses `agent start` with agent_pane_busy.
PANE_BUSY_TIMEOUT = 30


def start_agent(name, kind, pane, sleep=time.sleep, busy_timeout_seconds=PANE_BUSY_TIMEOUT):
    """`herdr agent start`, retrying a busy pane with backoff until its shell is available or the timeout passes."""
    delay, waited = 0.5, 0.0
    while True:
        try:
            herdr("agent", "start", name, "--kind", kind, "--pane", pane, "--timeout", "60000")
            return
        except HerdrError as error:
            if "agent_pane_busy" not in str(error):
                raise
            if waited >= busy_timeout_seconds:
                raise HerdrError(
                    "The new pane %s for agent %s was not ready: its shell did not become available within %gs "
                    "(herdr: agent_pane_busy). Start it by hand once it is: `herdr agent start %s --kind %s --pane %s "
                    "--timeout 60000`." % (pane, name, busy_timeout_seconds, name, kind, pane))
            step = min(delay, busy_timeout_seconds - waited)
            sleep(step)
            waited += step
            delay = min(delay * 2, 5)


def launch(path, label, name, kind, prompt, sleep=time.sleep, busy_timeout_seconds=PANE_BUSY_TIMEOUT):
    path = os.path.abspath(os.path.expanduser(path))
    # --cwd on the main checkout: Herdr reuses the repo's parent workspace (even one that has not reported worktree
    # info yet) or creates it, so the helper never re-lists for it.
    opened = herdr(
        "worktree", "open", "--cwd", repo_root(path), "--path", path, "--label", label, "--no-focus"
    )["result"]
    pane = opened["root_pane"]["pane_id"]
    summary = {"workspace_id": opened["workspace"]["workspace_id"], "pane_id": pane, "agent": name, "label": label}
    if opened.get("already_open"):
        summary["note"] = "The worktree already had a workspace; reused it."
    start_error = None
    try:
        start_agent(name, kind, pane, sleep, busy_timeout_seconds)
    except HerdrError as error:
        # Herdr refuses an agent that blocks during startup, as it does at the folder-trust prompt.
        if "agent_not_ready" not in str(error):
            raise
        start_error = error
    screen = herdr("agent", "read", name, "--source", "visible", "--lines", "60", parse=False)
    if start_error is not None and not has_trust_prompt(screen):
        raise start_error
    if has_trust_prompt(screen):
        saved = pending_prompt_path(name)
        os.makedirs(os.path.dirname(saved), exist_ok=True)
        with open(saved, "w") as handle:
            handle.write(prompt)
        summary.update(status="blocked", trust_prompt=True, prompted=False, next=(
            "The person running the session must answer the folder-trust prompt in that pane. Run "
            "`herdr_tools.py send-prompt --name %s` in the background now: it sends the saved prompt as soon as "
            "they have." % name))
        return summary
    summary.update(status=prompt_and_confirm(name, prompt, sleep), trust_prompt=False, prompted=True)
    return summary


REPO_LAUNCH_TIMEOUT = 120
REPO_LAUNCH_KEY = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9._/#-]*$")


def repo_launch(main_checkout, profile, values, runner=subprocess.run, sleep=time.sleep,
                timeout_seconds=REPO_LAUNCH_TIMEOUT, poll_seconds=2, launcher_timeout_seconds=300):
    """Spawn through the repository's own launcher (spawn.command) instead of `launch`: run it from the main checkout
    with VALUES filled in ({key}, {branch}, ...), then wait for the agent it names (spawn.agent) to appear. The launcher
    owns the worktree, the workspace and starting the agent; this never creates or starts either. A refusal (non-zero
    exit) is raised as the launcher said it, and nothing is retried."""
    spawn = profile["spawn"]
    if not spawn["command"]:
        raise HerdrError("This repo's profile has no spawn.command; use `launch` (the default spawn flow).")
    key = values.get("key")
    if not isinstance(key, str) or not REPO_LAUNCH_KEY.match(key):
        raise HerdrError("The key %r can't be passed to the repo's launcher: use letters, digits, '.', '_', '/', '-' "
                         "or '#', starting with a letter, digit or '#' (so it can't read as an option)." % key)
    argv = fill_command(spawn["command"], values)
    name = PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), spawn["agent"])
    if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", name):
        raise HerdrError("spawn.agent resolved to %r, which is not a Herdr agent name." % name)
    try:
        proc = runner(argv, capture_output=True, text=True, cwd=main_checkout, timeout=launcher_timeout_seconds)
    except subprocess.TimeoutExpired:
        raise HerdrError("The repo's launcher did not finish within %gs: %s. Check the workspace it may have opened "
                         "before running it again." % (launcher_timeout_seconds, " ".join(argv)))
    except OSError as error:
        raise HerdrError("Could not run the repo's launcher %s: %s. On Windows, write spawn.command with forward "
                         "slashes or quote a path with backslashes." % (argv[0], error))
    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    if proc.returncode != 0:
        raise HerdrError("The repo's launcher refused (exit %d): %s\n%s" % (proc.returncode, " ".join(argv),
                                                                          output or "(no output)"))
    waited = 0.0
    while True:
        agent = next((a for a in herdr("agent", "list")["result"]["agents"] if a.get("name") == name), None)
        if agent:
            break
        if waited >= timeout_seconds:
            raise HerdrError("Ran %s, but no agent named %s appeared within %gs. Check the workspace it opened."
                             % (" ".join(argv), name, timeout_seconds))
        sleep(poll_seconds)
        waited += poll_seconds
    screen = herdr("agent", "read", name, "--source", "visible", "--lines", "60", parse=False)
    return {"agent": name, "workspace_id": agent.get("workspace_id"), "pane_id": agent.get("pane_id"),
            "status": agent.get("agent_status"), "command": argv, "output": output[-2000:],
            "brief": spawn["brief"], "trust_prompt": has_trust_prompt(screen)}


# ---- CLI -----------------------------------------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    pw = sub.add_parser("parent-workspace")
    pw.add_argument("--path", required=True)
    pw.add_argument("--create", action="store_true")
    pw.add_argument("--wait-seconds", type=float, default=PARENT_WAIT_SECONDS)
    ln = sub.add_parser("launch")
    ln.add_argument("--path", required=True)
    ln.add_argument("--label", required=True)
    ln.add_argument("--name", required=True)
    ln.add_argument("--kind", default="claude")
    group = ln.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file")
    rl = sub.add_parser("repo-launch")
    rl.add_argument("--path", required=True)
    rl.add_argument("--key", required=True)
    rl.add_argument("--branch")
    rl.add_argument("--timeout-seconds", type=float, default=REPO_LAUNCH_TIMEOUT)
    sp = sub.add_parser("send-prompt")
    sp.add_argument("--name", required=True)
    sp.add_argument("--timeout-seconds", type=float, default=600)
    sp_group = sp.add_mutually_exclusive_group()
    sp_group.add_argument("--prompt")
    sp_group.add_argument("--prompt-file")
    rp = sub.add_parser("report-path")
    rp.add_argument("--name", required=True)
    ri = sub.add_parser("retire-info")
    ri.add_argument("target")
    la = sub.add_parser("land")
    la.add_argument("targets", nargs="*", metavar="target", help="one agent, or several to land in this order")
    la.add_argument("--mr", help="the MR to land, by URL or group/project!iid, when it isn't the agent's branch's")
    la.add_argument("--timeout-minutes", type=int, default=30)
    la.add_argument("--max-hours", type=float, default=3.0, help="with several agents: how long to wait for rebases")
    wa = sub.add_parser("watch")
    wa.add_argument("agents", nargs="+")
    wa.add_argument("--max-hours", type=float, default=4.0)
    wa.add_argument("--poll-seconds", type=float, default=120)
    um = sub.add_parser("update-main")
    um.add_argument("--path", required=True)
    ms = sub.add_parser("main-server")
    ms.add_argument("action", choices=["start", "restart", "status"])
    ms.add_argument("--path", required=True)
    ms.add_argument("--command", dest="server_command", default=None)
    pf = sub.add_parser("profile")
    pf.add_argument("--path", required=True)
    ss = sub.add_parser("setup-scan")
    ss.add_argument("--path", required=True)
    sm = sub.add_parser("sync-mirrors")
    sm.add_argument("--path", required=True)
    sm.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "setup-scan":  # reads a repo only, so it also works outside Herdr
        try:
            print(json.dumps(scan_repo(args.path), indent=2))
        except HerdrError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if os.environ.get("HERDR_ENV") != "1":
        print("Not running inside a Herdr-managed pane (HERDR_ENV != 1); refusing to control Herdr.", file=sys.stderr)
        return 2
    try:
        if args.command == "status":
            agents = herdr("agent", "list")["result"]["agents"]
            print(format_status(status_rows(workspaces(), agents)))
        elif args.command == "parent-workspace":
            ws = parent_workspace(args.path, args.create, args.wait_seconds)
            print(json.dumps({"workspace_id": ws["workspace_id"], "label": ws.get("label")}))
        elif args.command == "launch":
            prompt = args.prompt if args.prompt is not None else open(args.prompt_file).read().strip()
            print(json.dumps(launch(args.path, args.label, args.name, args.kind, prompt), indent=2))
        elif args.command == "repo-launch":
            main = repo_root(args.path)
            print(json.dumps(repo_launch(main, project_profile(main), {"key": args.key, "branch": args.branch},
                                         timeout_seconds=args.timeout_seconds), indent=2))
        elif args.command == "send-prompt":
            prompt = open(args.prompt_file).read().strip() if args.prompt_file else args.prompt
            print(json.dumps(send_prompt(args.name, prompt, args.timeout_seconds), indent=2))
        elif args.command == "report-path":
            path = status_report_path(args.name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            print(path)
        elif args.command == "retire-info":
            print(json.dumps(retire_info(args.target), indent=2))
        elif args.command == "land" and len(args.targets) > 1:
            if args.mr:
                raise HerdrError("--mr lands one MR; name several agents without it.")
            results = land_several(args.targets, args.timeout_minutes, args.max_hours)
            print(format_land_results(results))
            return 0 if all(r["result"] == "merged" for r in results) else 1
        elif args.command == "land":
            target = args.targets[0] if args.targets else None
            print(json.dumps(land(target, args.timeout_minutes, mr=args.mr), indent=2))
        elif args.command == "watch":
            print(json.dumps(watch(args.agents, args.max_hours, args.poll_seconds), indent=2))
        elif args.command == "update-main":
            print(json.dumps(update_main(args.path), indent=2))
        elif args.command == "main-server":
            print(json.dumps(main_server(args.path, args.action, args.server_command), indent=2))
        elif args.command == "profile":
            print(json.dumps(project_profile(repo_root(args.path)), indent=2))
        elif args.command == "sync-mirrors":
            main = repo_root(args.path)
            print(json.dumps(sync_mirrors(main, project_profile(main)["mirrors"], dry_run=args.dry_run), indent=2))
    except HerdrError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
