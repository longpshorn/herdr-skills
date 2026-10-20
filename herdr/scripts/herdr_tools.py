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
      draft, conflicts, unresolved threads, a failed pipeline or a missing approval.
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
  herdr_tools.py profile --path REPO
      The project's resolved profile: the repo's .claude/herdr-profile.json, with a personal overlay from
      ~/.config/herdr/profiles/ (matched by origin URL or path) laid over it, over the defaults.

Python 3.9 compatible (the macOS Command Line Tools python3).
"""
import argparse
import fnmatch
import json
import os
import re
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


def retire_checks(agent_status, porcelain, mr_state, head_landed, report, worktree_removed=False, approval=None):
    """Every check /herdr:retire reports, in order. The workspace may be retired only when all are ok.

    head_landed: the worktree's HEAD is the commit the MR merged, or is already on origin/main.
    worktree_removed: the worktree folder is already gone, so nothing local can be lost; the agent's report still gates.
    approval: an open MR's approval state (gitlab_approval's shape); None when there is no open MR to approve.
    """
    fields = report["fields"] if report else {}
    questions = report["sections"].get("Open questions", "") if report else ""
    open_questions = questions.strip().rstrip(".").lower() not in ("", "none")

    def check(name, ok, detail):
        return {"check": name, "ok": ok, "detail": detail}

    if worktree_removed:
        local = [
            check("worktree", True, "already removed"),
            check("merged", True, "worktree already removed; MR is %s" % (mr_state or "not found")),
            check("head", True, "worktree already removed"),
        ]
    else:
        local = [
            check("worktree", not porcelain.strip(), "clean" if not porcelain.strip() else "uncommitted:\n" + porcelain.rstrip()),
            check("merged", mr_state == "merged", "MR is %s" % (mr_state or "not found")),
            check(
                "head",
                head_landed,
                "HEAD is the merged commit or on origin/main" if head_landed else "HEAD has commits the merge did not take",
            ),
        ]
    if approval is None:
        local.append(check("approved", True, "no open MR to approve"))
    else:
        local.append(check("approved", approval["approved"], approval_detail(approval)))
    return [check("agent", agent_status in ("idle", "done"), "agent is %s" % agent_status)] + local + [
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
                "mirrors"}
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
    }
    resolved = {"defaulted": [k for k in defaults if k not in profile]}
    for key, default in defaults.items():
        value = profile.get(key, default)
        if isinstance(default, dict) and isinstance(value, dict):
            value = dict(default, **value)
        resolved[key] = value
    return resolved


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
                profile = merge_profiles(profile or {}, read_json(path))
                sources["overlay"] = path
                break
    return profile, sources


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

    def merge(self, iid, sha):
        merged = self.api("%s/%d/merge" % (self.base, iid), "PUT",
                          [("sha", sha), ("squash", "true"), ("should_remove_source_branch", "true")])
        return {"state": merged.get("state"), "commit": merged.get("squash_commit_sha") or merged.get("merge_commit_sha"),
                "url": merged.get("web_url")}


class GitHubForge:
    """Pull requests through gh, normalized to the merge-request shape land_decision reads. With a repo (a PR named by
    URL) every call names it; without one gh resolves it from the main checkout."""

    FIELDS = "number,state,url,headRefOid,isDraft,mergeable,mergeStateStatus,statusCheckRollup,reviewDecision"

    def __init__(self, main_checkout, repo=None):
        self.main = main_checkout
        self.repo_args = ["--repo", repo] if repo else []

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

    def merge(self, iid, sha):
        proc = subprocess.run(["gh", "pr", "merge", str(iid), "--squash", "--match-head-commit", sha, "--delete-branch"]
                              + self.repo_args, capture_output=True, text=True, cwd=self.main)
        if proc.returncode != 0:
            raise HerdrError("gh pr merge failed: %s" % (proc.stderr or proc.stdout).strip())
        pr = run_json(["gh", "pr", "view", str(iid), "--json", "state,url,mergeCommit"] + self.repo_args, self.main)
        return {"state": {"MERGED": "merged"}.get(pr.get("state"), pr.get("state")),
                "commit": (pr.get("mergeCommit") or {}).get("oid"), "url": pr.get("url")}


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
    approval = forge.view(mr["iid"])[0]["approvals"] if mr and mr["state"] == "opened" else None
    if not removed:
        default = git(main_checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")[1] or "origin/main"
        on_default = git(path, "merge-base", "--is-ancestor", "HEAD", default)[0] == 0
        head_landed = on_default or bool(mr and mr.get("sha") == head)
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
        "checks": retire_checks(
            agent.get("agent_status", "unknown") if agent else "gone",
            porcelain, mr["state"] if mr else None, head_landed, report, worktree_removed=removed, approval=approval,
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
                merged = forge.merge(iid, mr["sha"])
                return dict(merged, iid=iid, rebased=rebased_from is not None)
            if decision["action"] == "rebase" and rebased_from != mr["sha"]:
                forge.rebase(iid)
                rebased_from = mr["sha"]
        if time.time() > deadline:
            raise HerdrError("Gave up on !%d after %d minutes: %s." % (iid, timeout_seconds // 60, decision["reason"]))
        print("!%d: %s" % (iid, decision["reason"]), file=sys.stderr, flush=True)
        sleep(poll_seconds)


def land(target, timeout_minutes=30, poll_seconds=20, mr=None):
    """Merge a validated MR or PR: rebase when it is behind, wait for the head pipeline, then squash-merge pinned to that
    commit. The MR is the agent's (its worktree branch's, else the one its status report names) or the one `mr` names
    by URL or `group/project!iid`, which may live in any repo."""
    if mr is not None:
        ref = parse_mr_ref(mr)
        return land_loop(forge_for_ref(ref, os.getcwd()), ref["iid"], timeout_minutes * 60, poll_seconds)
    if not target:
        raise HerdrError("Name the agent whose MR to land, or the MR itself with --mr <url or group/project!iid>.")
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


def watch(agents, max_hours=4.0, poll_seconds=120):
    return watch_loop(
        agents, land, lambda agent, message: herdr("agent", "prompt", agent, message, parse=False),
        lambda agent: read_report(agent)[1], max_hours * 3600, poll_seconds)


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
    la.add_argument("target", nargs="?")
    la.add_argument("--mr", help="the MR to land, by URL or group/project!iid, when it isn't the agent's branch's")
    la.add_argument("--timeout-minutes", type=int, default=30)
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
    sm = sub.add_parser("sync-mirrors")
    sm.add_argument("--path", required=True)
    sm.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

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
        elif args.command == "send-prompt":
            prompt = open(args.prompt_file).read().strip() if args.prompt_file else args.prompt
            print(json.dumps(send_prompt(args.name, prompt, args.timeout_seconds), indent=2))
        elif args.command == "report-path":
            path = status_report_path(args.name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            print(path)
        elif args.command == "retire-info":
            print(json.dumps(retire_info(args.target), indent=2))
        elif args.command == "land":
            print(json.dumps(land(args.target, args.timeout_minutes, mr=args.mr), indent=2))
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
