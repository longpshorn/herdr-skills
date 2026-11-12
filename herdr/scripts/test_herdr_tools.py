"""Tests for herdr_tools: its pure logic, and its herdr flows against a stubbed `herdr`. Run: python3 -m unittest discover -s <this dir> -p 'test_*.py'"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import herdr_tools as ht

REPO = "/code/repo"
# HOME, and USERPROFILE, which is what expanduser reads on Windows.
HOME_VARS = ("HOME", "USERPROFILE")
SUITE_HOME = None


def point_home_at(path):
    """Point the home directory at path; returns what restore_home needs to undo it."""
    saved = {name: os.environ.get(name) for name in HOME_VARS}
    for name in HOME_VARS:
        os.environ[name] = path
    return saved


def restore_home(saved):
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def setUpModule():
    """No test reads the real home folder (installed plugins, settings, status reports): the suite runs against an
    empty one."""
    global SUITE_HOME, SAVED_HOME
    SUITE_HOME = tempfile.mkdtemp()
    SAVED_HOME = point_home_at(SUITE_HOME)


def tearDownModule():
    restore_home(SAVED_HOME)
    shutil.rmtree(SUITE_HOME)
WORKSPACES = [
    {"workspace_id": "w1", "label": "notes", "number": 1},
    {
        "workspace_id": "w2",
        "label": "repo",
        "number": 2,
        "worktree": {"repo_root": REPO, "checkout_path": REPO, "is_linked_worktree": False},
    },
    {
        "workspace_id": "w3",
        "label": "TICKET-1 feature",
        "number": 3,
        "worktree": {"repo_root": REPO, "checkout_path": REPO + "-TICKET-1", "is_linked_worktree": True},
    },
]
AGENTS = [
    {"name": "a", "agent": "claude", "agent_status": "idle", "workspace_id": "w1", "pane_id": "w1:p1", "terminal_title_stripped": "Notes"},
    {"name": "b", "agent": "claude", "agent_status": "blocked", "workspace_id": "w3", "pane_id": "w3:p1", "terminal_title_stripped": "Waiting"},
    {"agent": "codex", "agent_status": "working", "workspace_id": "w2", "pane_id": "w2:p1", "terminal_title_stripped": "Build"},
    {"name": "d", "agent": "claude", "agent_status": "done", "workspace_id": "w2", "pane_id": "w2:p2", "terminal_title_stripped": "Finished"},
]


class FindParentWorkspace(unittest.TestCase):
    def test_returns_the_main_checkout_workspace_of_the_repo(self):
        self.assertEqual(ht.find_parent_workspace(WORKSPACES, REPO)["workspace_id"], "w2")

    def test_ignores_linked_worktree_workspaces_and_other_repos(self):
        only_linked = [WORKSPACES[0], WORKSPACES[2]]
        self.assertIsNone(ht.find_parent_workspace(only_linked, REPO))
        self.assertIsNone(ht.find_parent_workspace(WORKSPACES, "/code/other"))

    def test_tolerates_a_trailing_slash(self):
        self.assertEqual(ht.find_parent_workspace(WORKSPACES, REPO + "/")["workspace_id"], "w2")

    def test_falls_back_to_a_workspace_without_worktree_info_whose_pane_sits_in_the_main_checkout(self):
        # Herdr fills in a workspace's worktree info only once a worktree is opened from it.
        bare = [{"workspace_id": "w5", "label": "repo"}, WORKSPACES[0]]
        panes = [{"workspace_id": "w1", "cwd": "/code"}, {"workspace_id": "w5", "cwd": REPO + "/"}]
        self.assertEqual(ht.find_parent_workspace(bare, REPO, panes)["workspace_id"], "w5")

    def test_prefers_reported_worktree_info_over_a_pane_cwd_match(self):
        panes = [{"workspace_id": "w1", "cwd": REPO}]
        self.assertEqual(ht.find_parent_workspace(WORKSPACES, REPO, panes)["workspace_id"], "w2")

    def test_a_pane_cwd_match_never_picks_a_workspace_on_another_checkout(self):
        panes = [{"workspace_id": "w3", "cwd": REPO}]
        self.assertIsNone(ht.find_parent_workspace([WORKSPACES[2]], REPO, panes))


class FakeHerdr:
    """Stands in for the `herdr` CLI: records each call and answers from in-memory workspaces and panes."""

    def __init__(self, workspaces=(), panes=(), create_result=None, appear_after=0):
        self.workspaces, self.panes, self.calls = list(workspaces), list(panes), []
        self.create_result = create_result or {"workspace": {"workspace_id": "w9", "label": "repo"}}
        self.appear_after, self.pending = appear_after, None

    def __call__(self, *args, parse=True):
        self.calls.append(args)
        if args[:2] == ("workspace", "list"):
            if self.pending is not None:
                if self.appear_after == 0:
                    self.workspaces.append(self.pending)
                    self.pending = None
                self.appear_after -= 1
            return {"result": {"workspaces": list(self.workspaces)}}
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": list(self.panes)}}
        if args[:2] == ("workspace", "create"):
            self.pending = self.create_result.get("workspace")
            return {"result": self.create_result}
        if args[:2] == ("workspace", "close"):
            return ""
        raise AssertionError("unexpected herdr call: %r" % (args,))

    def called(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


class ParentWorkspace(unittest.TestCase):
    def run_with(self, fake, **kw):
        saved = ht.herdr, ht.repo_root
        ht.herdr, ht.repo_root = fake, lambda path: REPO
        try:
            return ht.parent_workspace(REPO, sleep=lambda s: None, **kw)
        finally:
            ht.herdr, ht.repo_root = saved

    def test_returns_a_workspace_that_reports_worktree_info_without_creating(self):
        fake = FakeHerdr(WORKSPACES)
        self.assertEqual(self.run_with(fake, create=True)["workspace_id"], "w2")
        self.assertEqual(fake.called("workspace", "create"), [])

    def test_a_workspace_that_has_not_reported_worktree_info_yet_is_reused_not_duplicated(self):
        fake = FakeHerdr([{"workspace_id": "w5", "label": "repo"}], panes=[{"workspace_id": "w5", "cwd": REPO}])
        self.assertEqual(self.run_with(fake, create=True)["workspace_id"], "w5")
        self.assertEqual(fake.called("workspace", "create"), [])

    def test_identifies_the_created_workspace_by_the_id_create_returns(self):
        # The new workspace has no worktree info, and will not get it until a worktree is opened from it.
        fake = FakeHerdr([WORKSPACES[0]])
        self.assertEqual(self.run_with(fake, create=True)["workspace_id"], "w9")
        self.assertEqual(fake.called("workspace", "create"),
                         [("workspace", "create", "--cwd", REPO, "--label", "repo", "--no-focus")])

    def test_waits_for_a_created_workspace_that_is_listed_late(self):
        fake = FakeHerdr([WORKSPACES[0]], appear_after=3)
        self.assertEqual(self.run_with(fake, create=True, wait_seconds=5)["workspace_id"], "w9")

    def test_a_second_call_reuses_the_workspace_the_first_created(self):
        fake = FakeHerdr([WORKSPACES[0]])
        self.run_with(fake, create=True)
        fake.panes.append({"workspace_id": "w9", "cwd": REPO})
        self.assertEqual(self.run_with(fake, create=True)["workspace_id"], "w9")
        self.assertEqual(len(fake.called("workspace", "create")), 1)

    def test_closes_the_created_workspace_and_says_what_it_waited_for_when_it_never_shows_up(self):
        fake = FakeHerdr([WORKSPACES[0]], appear_after=1000)
        with self.assertRaisesRegex(ht.HerdrError, r"w9.*did not appear in `herdr workspace list` within 2s.*closed it"):
            self.run_with(fake, create=True, wait_seconds=2)
        self.assertEqual(fake.called("workspace", "close"), [("workspace", "close", "w9")])

    def test_a_create_that_returns_no_workspace_id_fails_clearly(self):
        fake = FakeHerdr([WORKSPACES[0]], create_result={"type": "workspace_created"})
        with self.assertRaisesRegex(ht.HerdrError, "returned no workspace id"):
            self.run_with(fake, create=True)

    def test_without_create_it_suggests_create_and_creates_nothing(self):
        fake = FakeHerdr([WORKSPACES[0]])
        with self.assertRaisesRegex(ht.HerdrError, "re-run with --create"):
            self.run_with(fake)
        self.assertEqual(fake.called("workspace", "create"), [])


class FakeAgentHerdr:
    """Stands in for the `herdr` CLI around one agent: each `agent read` returns the next screen (the last repeats)."""

    def __init__(self, screens=("> ",), statuses=("idle", "working"), start_error=None, start_errors=()):
        self.screens, self.statuses, self.calls = list(screens), list(statuses), []
        self.start_error = start_error
        self.start_errors = list(start_errors)  # one per `agent start` call, None for success; then success

    def __call__(self, *args, parse=True):
        self.calls.append(args)
        if args[:2] == ("worktree", "open"):
            return {"result": {"root_pane": {"pane_id": "w8:p1"}, "workspace": {"workspace_id": "w8"}}}
        if args[:2] == ("agent", "read"):
            return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if args[:2] == ("agent", "get"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"result": {"agent": {"agent_status": status}}}
        if args[:2] == ("agent", "start") and self.start_errors:
            error = self.start_errors.pop(0)
            if error:
                raise ht.HerdrError(error)
            return {"result": {}}
        if args[:2] == ("agent", "start") and self.start_error:
            raise ht.HerdrError(self.start_error)
        if args[:2] in (("agent", "start"), ("agent", "prompt")):
            return {"result": {}}
        raise AssertionError("unexpected herdr call: %r" % (args,))

    def called(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


class AgentHerdrTest(unittest.TestCase):
    """Stubs `herdr` and git, and points the home directory at a temporary one of its own for the saved-prompt
    files."""

    def setUp(self):
        self.saved = ht.herdr, ht.repo_root
        ht.repo_root = lambda path: REPO
        self.home = tempfile.mkdtemp()
        self.saved_home = point_home_at(self.home)

    def tearDown(self):
        ht.herdr, ht.repo_root = self.saved
        restore_home(self.saved_home)
        shutil.rmtree(self.home)

    def use(self, fake):
        ht.herdr = fake
        return fake


class Launch(AgentHerdrTest):
    def launch(self):
        return ht.launch(REPO + "-TICKET-2", "TICKET-2 x", "ticket2", "claude", "Read the brief", sleep=lambda s: None)

    def test_lets_herdr_find_or_create_the_parent_from_the_main_checkout(self):
        fake = self.use(FakeAgentHerdr())
        self.launch()
        self.assertEqual(fake.called("worktree", "open"), [("worktree", "open", "--cwd", REPO, "--path", os.path.abspath(REPO + "-TICKET-2"),
                                                            "--label", "TICKET-2 x", "--no-focus")])
        self.assertEqual(fake.called("workspace"), [])

    def test_sends_the_prompt_and_reports_the_agent_working(self):
        fake = self.use(FakeAgentHerdr())
        summary = self.launch()
        self.assertEqual(fake.called("agent", "prompt"), [("agent", "prompt", "ticket2", "Read the brief")])
        self.assertEqual((summary["workspace_id"], summary["status"], summary["trust_prompt"]), ("w8", "working", False))

    def test_an_agent_that_finished_before_the_first_poll_is_reported_as_prompted(self):
        # A short prompt can be answered before the status is first read, so `idle` alone is ambiguous.
        self.use(FakeAgentHerdr(statuses=["idle"]))
        summary = self.launch()
        self.assertEqual((summary["status"], summary["prompted"]), ("idle", True))

    def test_at_a_trust_prompt_it_saves_the_prompt_for_send_prompt_instead_of_sending_it(self):
        fake = self.use(FakeAgentHerdr(screens=[TRUST_SCREEN]))
        summary = self.launch()
        self.assertEqual(fake.called("agent", "prompt"), [])
        self.assertEqual((summary["status"], summary["trust_prompt"], summary["prompted"]), ("blocked", True, False))
        self.assertIn("send-prompt --name ticket2", summary["next"])
        with open(ht.pending_prompt_path("ticket2")) as handle:
            self.assertEqual(handle.read(), "Read the brief")

    def test_an_agent_start_refused_at_the_trust_prompt_is_reported_as_trust_blocked(self):
        # herdr 0.9.5 fails `agent start` when the agent blocks during startup, as it does at the trust prompt.
        fake = self.use(FakeAgentHerdr(screens=[TRUST_SCREEN], start_error=AGENT_NOT_READY))
        summary = self.launch()
        self.assertEqual((summary["status"], summary["trust_prompt"]), ("blocked", True))
        self.assertEqual(fake.called("agent", "prompt"), [])
        self.assertTrue(os.path.exists(ht.pending_prompt_path("ticket2")))

    def test_an_agent_start_refused_for_another_reason_still_fails(self):
        self.use(FakeAgentHerdr(start_error=AGENT_NOT_READY))
        with self.assertRaisesRegex(ht.HerdrError, "agent_not_ready"):
            self.launch()


class LaunchWaitsForThePanesShell(AgentHerdrTest):
    """A just-created workspace's pane may not have its shell yet: herdr refuses `agent start` with agent_pane_busy."""

    def launch(self, timeout=30):
        self.slept = []
        return ht.launch(REPO + "-TICKET-2", "TICKET-2 x", "ticket2", "claude", "Read the brief",
                         sleep=self.slept.append, busy_timeout_seconds=timeout)

    def test_retries_a_busy_pane_with_backoff_then_starts_the_agent(self):
        fake = self.use(FakeAgentHerdr(start_errors=[PANE_BUSY, PANE_BUSY, None]))
        summary = self.launch()
        self.assertEqual(len(fake.called("agent", "start")), 3)
        self.assertEqual(summary["status"], "working")
        self.assertEqual(len(self.slept[:2]), 2)
        self.assertLess(self.slept[0], self.slept[1])

    def test_gives_up_with_a_clear_error_when_the_shell_never_becomes_available(self):
        self.use(FakeAgentHerdr(start_errors=[PANE_BUSY] * 100))
        with self.assertRaisesRegex(ht.HerdrError, r"pane w8:p1 .*not ready.*within 5s"):
            self.launch(timeout=5)
        self.assertLessEqual(sum(s for s in self.slept), 5 + 5)


class HerdrOutput(unittest.TestCase):
    """The herdr wrapper never lets a Python traceback through: non-JSON output becomes a HerdrError."""

    def setUp(self):
        self.saved = ht.subprocess.run
        self.addCleanup(setattr, ht.subprocess, "run", self.saved)

    def respond(self, returncode, stdout, stderr=""):
        class Done:
            pass
        done = Done()
        done.returncode, done.stdout, done.stderr = returncode, stdout, stderr
        ht.subprocess.run = lambda *a, **k: done

    def test_non_json_output_is_reported_as_an_error(self):
        self.respond(0, "Starting server...\n")
        with self.assertRaisesRegex(ht.HerdrError, "not JSON.*Starting server"):
            ht.herdr("agent", "list")

    def test_a_failure_carries_herdrs_own_message(self):
        self.respond(1, "", PANE_BUSY)
        with self.assertRaisesRegex(ht.HerdrError, "agent_pane_busy"):
            ht.herdr("agent", "start", "x")


PANE_BUSY = ('{"error":{"code":"agent_pane_busy","message":"agent target pane w8:p1 is not an available shell"},'
             '"id":"cli:agent:start"}')


class ParseMrRef(unittest.TestCase):
    def test_reads_gitlab_and_github_urls_and_the_short_form(self):
        self.assertEqual(ht.parse_mr_ref("https://gitlab.com/example-org/apps/mobile-app/-/merge_requests/96"),
                         {"forge": "gitlab", "host": "gitlab.com", "project": "example-org/apps/mobile-app", "iid": 96})
        self.assertEqual(ht.parse_mr_ref("https://gitlab.com/g/p/-/merge_requests/7/diffs")["iid"], 7)
        self.assertEqual(ht.parse_mr_ref("example-org/apps/mobile-app!96"),
                         {"forge": "gitlab", "host": "gitlab.com", "project": "example-org/apps/mobile-app", "iid": 96})
        self.assertEqual(ht.parse_mr_ref("https://github.com/owner/repo/pull/12"),
                         {"forge": "github", "host": "github.com", "project": "owner/repo", "iid": 12})

    def test_refuses_anything_else_by_name(self):
        for bad in ("96", "!96", "https://gitlab.com/g/p", "https://example.com/x/pull/1", "none"):
            with self.assertRaisesRegex(ht.HerdrError, "merge request"):
                ht.parse_mr_ref(bad)


class ReportMrRef(unittest.TestCase):
    def report(self, mr):
        return ht.parse_status_report(REPORT.replace("mr: https://gitlab.com/group/repo/-/merge_requests/7", "mr: " + mr))

    def test_takes_the_first_merge_request_the_report_names(self):
        ref = ht.report_mr_ref(self.report("https://gitlab.com/g/p/-/merge_requests/96, https://gitlab.com/g/q/-/merge_requests/3"))
        self.assertEqual((ref["project"], ref["iid"]), ("g/p", 96))

    def test_none_without_one(self):
        self.assertIsNone(ht.report_mr_ref(self.report("none")))
        self.assertIsNone(ht.report_mr_ref(None))


class ForgeForAProject(unittest.TestCase):
    """A forge bound to an MR's own project works from any directory: the MR may live in another repo."""

    def setUp(self):
        self.calls = []
        self.saved = ht.run_json
        ht.run_json = lambda args, cwd: self.calls.append(args) or {}
        self.addCleanup(setattr, ht, "run_json", self.saved)

    def test_gitlab_calls_name_the_project_and_host(self):
        forge = ht.forge_for_ref(ht.parse_mr_ref("https://gitlab.com/g/sub/p/-/merge_requests/96"), "/anywhere")
        forge.rebase(96)
        self.assertIn("projects/g%2Fsub%2Fp/merge_requests/96/rebase", self.calls[0])
        self.assertEqual(self.calls[0][self.calls[0].index("--hostname") + 1], "gitlab.com")

    def test_without_a_project_gitlab_resolves_it_from_the_checkout(self):
        ht.GitLabForge("/repo").rebase(5)
        self.assertIn("projects/:fullpath/merge_requests/5/rebase", self.calls[0])

    def test_github_calls_name_the_repository(self):
        forge = ht.forge_for_ref(ht.parse_mr_ref("https://github.com/o/r/pull/4"), "/anywhere")
        try:
            forge.view(4)
        except Exception:
            pass
        self.assertEqual(self.calls[0][self.calls[0].index("--repo") + 1], "o/r")


class FakeViewForge:
    def __init__(self, found=None, view=None):
        self.found, self.viewed = found, view

    def find(self, branch):
        return self.found

    def view(self, iid):
        return self.viewed, False


class ResolveMr(unittest.TestCase):
    """retire-info and land use the worktree branch's MR, else the one the agent's report names (maybe another repo)."""

    def report(self, mr):
        return ht.parse_status_report(REPORT.replace("mr: https://gitlab.com/group/repo/-/merge_requests/7", "mr: " + mr))

    def test_the_branchs_own_mr_wins(self):
        branch_forge = FakeViewForge(found={"iid": 3, "state": "opened", "url": "u", "sha": "abc"})
        mr, forge = ht.resolve_mr(branch_forge, "feat/x", self.report("https://gitlab.com/g/p/-/merge_requests/96"), "/w",
                                  forge_factory=lambda ref, cwd: self.fail("the report's MR should not be read"))
        self.assertEqual((mr["iid"], forge), (3, branch_forge))

    def test_falls_back_to_the_mr_the_report_names_with_a_forge_for_its_project(self):
        other = FakeViewForge(view={"state": "opened", "web_url": "https://gitlab.com/g/p/-/merge_requests/96", "sha": "def"})
        made = []
        mr, forge = ht.resolve_mr(FakeViewForge(), "feat/herdr-shareable",
                                  self.report("https://gitlab.com/g/p/-/merge_requests/96"), "/w",
                                  forge_factory=lambda ref, cwd: made.append(ref) or other)
        self.assertIs(forge, other)
        self.assertEqual(made[0]["project"], "g/p")
        self.assertEqual((mr["iid"], mr["state"], mr["sha"], mr["ref"]), (96, "opened", "def", "g/p!96"))

    def test_none_when_neither_has_one(self):
        self.assertEqual(ht.resolve_mr(FakeViewForge(), "feat/x", self.report("none"), "/w")[0], None)
        self.assertEqual(ht.resolve_mr(FakeViewForge(), None, None, "/w")[0], None)


class LandAGivenMr(unittest.TestCase):
    def setUp(self):
        self.saved = ht.land_loop
        self.looped = []
        ht.land_loop = lambda forge, iid, timeout, poll: self.looped.append((forge, iid)) or {"state": "merged", "iid": iid}
        self.addCleanup(setattr, ht, "land_loop", self.saved)

    def test_lands_an_mr_named_by_url_or_short_form_with_a_forge_for_its_project(self):
        ht.land(None, mr="https://gitlab.com/g/sub/p/-/merge_requests/96")
        forge, iid = self.looped[0]
        self.assertEqual((iid, forge.base, forge.host), (96, "projects/g%2Fsub%2Fp/merge_requests", "gitlab.com"))
        ht.land(None, mr="g/p!5")
        self.assertEqual(self.looped[1][1], 5)

    def test_needs_an_agent_or_an_mr(self):
        with self.assertRaisesRegex(ht.HerdrError, "--mr"):
            ht.land(None)


AGENT_NOT_READY = ('{"error":{"code":"agent_not_ready","message":"agent ticket2 is blocked during startup and is not '
                   'ready for prompts"},"id":"cli:agent:start"}')
TRUST_SCREEN = "Do you trust the files in this folder?\n> 1. Yes, proceed"


class SendPrompt(AgentHerdrTest):
    def save(self, text):
        path = ht.pending_prompt_path("ticket2")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def send(self, **kw):
        return ht.send_prompt("ticket2", sleep=lambda s: None, poll_seconds=3, **kw)

    def test_waits_for_the_trust_prompt_to_clear_then_sends_the_saved_prompt_once(self):
        path = self.save("Read the brief")
        fake = self.use(FakeAgentHerdr(screens=[TRUST_SCREEN, TRUST_SCREEN, "> "], statuses=["unknown", "unknown", "idle", "working"]))
        result = self.send()
        self.assertEqual(fake.called("agent", "prompt"), [("agent", "prompt", "ticket2", "Read the brief")])
        self.assertEqual((result["status"], result["prompted"]), ("working", True))
        self.assertFalse(os.path.exists(path))

    def test_an_explicit_prompt_overrides_the_saved_one(self):
        self.save("Read the brief")
        fake = self.use(FakeAgentHerdr())
        self.send(prompt="Something else")
        self.assertEqual(fake.called("agent", "prompt"), [("agent", "prompt", "ticket2", "Something else")])

    def test_gives_up_after_the_timeout_without_sending_and_keeps_the_saved_prompt(self):
        path = self.save("Read the brief")
        fake = self.use(FakeAgentHerdr(screens=[TRUST_SCREEN], statuses=["unknown"]))
        with self.assertRaisesRegex(ht.HerdrError, r"Waited 9s for ticket2 to clear the folder-trust prompt.*not sent.*saved"):
            self.send(timeout_seconds=9)
        self.assertEqual(fake.called("agent", "prompt"), [])
        self.assertTrue(os.path.exists(path))

    def test_a_timeout_that_is_not_a_multiple_of_the_poll_interval_still_gives_up(self):
        self.save("Read the brief")
        fake = self.use(FakeAgentHerdr(screens=[TRUST_SCREEN], statuses=["unknown"]))
        with self.assertRaisesRegex(ht.HerdrError, "Waited 10s"):
            self.send(timeout_seconds=10)
        self.assertEqual(fake.called("agent", "prompt"), [])

    def test_without_a_saved_or_given_prompt_it_says_so(self):
        self.use(FakeAgentHerdr())
        with self.assertRaisesRegex(ht.HerdrError, "No saved prompt for ticket2"):
            self.send()


class StatusRows(unittest.TestCase):
    def test_puts_agents_needing_attention_first(self):
        statuses = [row["status"] for row in ht.status_rows(WORKSPACES, AGENTS)]
        self.assertEqual(statuses, ["blocked", "done", "working", "idle"])

    def test_labels_each_agent_with_its_workspace_and_falls_back_to_the_pane_for_unnamed_agents(self):
        rows = {row["status"]: row for row in ht.status_rows(WORKSPACES, AGENTS)}
        self.assertEqual(rows["blocked"]["workspace"], "TICKET-1 feature")
        self.assertEqual(rows["working"]["target"], "w2:p1")
        self.assertEqual(rows["idle"]["target"], "a")

    def test_formats_a_table_with_a_header(self):
        text = ht.format_status(ht.status_rows(WORKSPACES, AGENTS))
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("STATUS"))
        self.assertIn("TICKET-1 feature", lines[1])
        self.assertEqual(ht.format_status([]), "No agents are running.")


class TrustPrompt(unittest.TestCase):
    def test_spots_claude_codes_folder_trust_prompt(self):
        screen = "Do you trust the files in this folder?\n\n ❯ 1. Yes, proceed\n   2. No, exit"
        self.assertTrue(ht.has_trust_prompt(screen))

    def test_ignores_an_ordinary_prompt(self):
        self.assertFalse(ht.has_trust_prompt('❯ Try "refactor create-services.ts"\n  auto mode on'))


class RepoRootFromCommonDir(unittest.TestCase):
    def test_main_checkout_and_linked_worktrees_resolve_to_the_same_root(self):
        self.assertEqual(ht.repo_root_from_common_dir(REPO + "/.git"), REPO)
        self.assertEqual(ht.repo_root_from_common_dir(REPO + "/.git/"), REPO)


REPORT = """---
agent: res1
ticket: TICKET-1
branch: feat/TICKET-1-feature
state: merged
safe_to_retire: yes
mr: https://gitlab.com/group/repo/-/merge_requests/7
pipeline: passed
updated: 2026-10-17 14:05
---
## Summary
Built the feature.

## Changed
- The widget (Verified)

## Open questions
None

## Follow-ups
- File a ticket for the docs gap
"""


class StatusReportPath(unittest.TestCase):
    def test_lives_in_the_herdr_status_cache_named_after_the_agent(self):
        self.assertEqual(ht.status_report_path("res1", home="/home/p"),
                         os.path.join("/home/p", ".cache", "herdr", "status", "res1.md"))


class BriefPath(unittest.TestCase):
    def test_lives_in_the_herdr_briefs_cache_named_after_the_agent(self):
        self.assertEqual(ht.brief_path("res1", home="/home/p"),
                         os.path.join("/home/p", ".cache", "herdr", "briefs", "res1.md"))


class ParseStatusReport(unittest.TestCase):
    def test_reads_the_header_fields_and_sections(self):
        report = ht.parse_status_report(REPORT)
        self.assertEqual(report["fields"]["state"], "merged")
        self.assertEqual(report["fields"]["branch"], "feat/TICKET-1-feature")
        self.assertEqual(report["sections"]["Summary"], "Built the feature.")
        self.assertEqual(report["sections"]["Follow-ups"], "- File a ticket for the docs gap")

    def test_tolerates_missing_sections(self):
        report = ht.parse_status_report("---\nstate: mr-open\n---\n## Summary\nHalf done.\n")
        self.assertEqual(report["sections"], {"Summary": "Half done."})

    def test_returns_none_without_a_header(self):
        self.assertIsNone(ht.parse_status_report("## Summary\nNo header here.\n"))
        self.assertIsNone(ht.parse_status_report(""))


def checks_by_name(checks):
    return {c["check"]: c for c in checks}


class RetireChecks(unittest.TestCase):
    def setUp(self):
        self.report = ht.parse_status_report(REPORT)

    def test_all_clear_when_idle_clean_merged_and_reported_safe(self):
        checks = ht.retire_checks("idle", "", "merged", True, self.report)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertEqual(
            [c["check"] for c in checks], ["agent", "worktree", "merged", "head", "approved", "report", "safe_to_retire",
                                     "open_questions"]
        )

    def test_a_done_agent_counts_as_finished(self):
        self.assertTrue(checks_by_name(ht.retire_checks("done", "", "merged", True, self.report))["agent"]["ok"])

    def test_stops_for_a_working_or_blocked_agent(self):
        for status in ("working", "blocked"):
            self.assertFalse(checks_by_name(ht.retire_checks(status, "", "merged", True, self.report))["agent"]["ok"])

    def test_stops_for_uncommitted_changes_and_lists_them(self):
        check = checks_by_name(ht.retire_checks("idle", " M src/a.ts\n", "merged", True, self.report))["worktree"]
        self.assertFalse(check["ok"])
        self.assertIn("src/a.ts", check["detail"])

    def test_stops_when_the_mr_is_not_merged(self):
        for state in ("opened", None):
            self.assertFalse(checks_by_name(ht.retire_checks("idle", "", state, True, self.report))["merged"]["ok"])

    def test_stops_when_head_has_commits_the_merge_did_not_take(self):
        check = checks_by_name(ht.retire_checks("idle", "", "merged", False, self.report))["head"]
        self.assertFalse(check["ok"])

    def test_stops_without_a_report(self):
        checks = checks_by_name(ht.retire_checks("idle", "", "merged", True, None))
        self.assertFalse(checks["report"]["ok"])
        self.assertFalse(checks["safe_to_retire"]["ok"])

    def test_stops_when_the_agent_says_it_is_not_safe(self):
        report = ht.parse_status_report(REPORT.replace("safe_to_retire: yes", "safe_to_retire: no"))
        self.assertFalse(checks_by_name(ht.retire_checks("idle", "", "merged", True, report))["safe_to_retire"]["ok"])

    def test_stops_for_open_questions_and_quotes_them(self):
        report = ht.parse_status_report(REPORT.replace("## Open questions\nNone", "## Open questions\n- Keep the flag?"))
        check = checks_by_name(ht.retire_checks("idle", "", "merged", True, report))["open_questions"]
        self.assertFalse(check["ok"])
        self.assertIn("Keep the flag?", check["detail"])

    def test_an_empty_or_none_questions_section_is_clear(self):
        for body in ("", "None.", "none"):
            report = ht.parse_status_report(REPORT.replace("## Open questions\nNone", "## Open questions\n" + body))
            self.assertTrue(checks_by_name(ht.retire_checks("idle", "", "merged", True, report))["open_questions"]["ok"], body)

    def test_an_already_removed_worktree_has_nothing_local_to_lose(self):
        checks = checks_by_name(ht.retire_checks("idle", None, None, None, self.report, worktree_removed=True))
        for name in ("worktree", "merged", "head"):
            self.assertTrue(checks[name]["ok"], name)
            self.assertIn("already removed", checks[name]["detail"])

    def test_an_already_removed_worktree_still_shows_the_mr_state(self):
        check = checks_by_name(ht.retire_checks("idle", None, "opened", None, self.report, worktree_removed=True))["merged"]
        self.assertIn("opened", check["detail"])

    def test_an_already_removed_worktree_still_needs_the_agents_report(self):
        checks = checks_by_name(ht.retire_checks("idle", None, None, None, None, worktree_removed=True))
        self.assertFalse(checks["report"]["ok"])
        self.assertFalse(checks["safe_to_retire"]["ok"])


class FindRetireTarget(unittest.TestCase):
    def test_finds_a_worktree_workspace_by_agent_name_workspace_id_or_label(self):
        for target in ("b", "w3", "TICKET-1 feature"):
            self.assertEqual(ht.find_retire_target(WORKSPACES, AGENTS, target, own_workspace="w1")["workspace_id"], "w3")

    def test_refuses_a_main_checkout_workspace(self):
        with self.assertRaisesRegex(ht.HerdrError, "main checkout"):
            ht.find_retire_target(WORKSPACES, AGENTS, "w2", own_workspace="w1")

    def test_refuses_the_workspace_this_session_runs_in(self):
        with self.assertRaisesRegex(ht.HerdrError, "this session"):
            ht.find_retire_target(WORKSPACES, AGENTS, "w3", own_workspace="w3")

    def test_refuses_an_unknown_target_or_one_without_a_worktree(self):
        with self.assertRaisesRegex(ht.HerdrError, "No workspace"):
            ht.find_retire_target(WORKSPACES, AGENTS, "nope", own_workspace="w1")
        with self.assertRaisesRegex(ht.HerdrError, "no worktree"):
            ht.find_retire_target(WORKSPACES, AGENTS, "notes", own_workspace="w2")


def mr(**overrides):
    base = {
        "state": "opened", "draft": False, "sha": "abc", "has_conflicts": False,
        "blocking_discussions_resolved": True, "detailed_merge_status": "mergeable",
        "head_pipeline": {"status": "success", "sha": "abc"},
    }
    base.update(overrides)
    return base


class LandDecision(unittest.TestCase):
    def action(self, **overrides):
        return ht.land_decision(mr(**overrides))["action"]

    def test_merges_a_mergeable_mr_whose_head_pipeline_passed(self):
        self.assertEqual(self.action(), "merge")

    def test_an_already_merged_mr_is_done(self):
        self.assertEqual(self.action(state="merged"), "done")

    def test_rebases_an_mr_that_is_behind_before_looking_at_its_pipeline(self):
        self.assertEqual(self.action(detailed_merge_status="need_rebase"), "rebase")
        self.assertEqual(self.action(detailed_merge_status="need_rebase", head_pipeline={"status": "running", "sha": "abc"}), "rebase")

    def test_waits_while_the_head_pipeline_runs_or_is_not_attached_yet(self):
        for pipeline in ({"status": "running", "sha": "abc"}, {"status": "pending", "sha": "abc"},
                         {"status": "success", "sha": "old"}, None):
            self.assertEqual(self.action(head_pipeline=pipeline), "wait", pipeline)
        for status in ("checking", "unchecked", "ci_still_running", "preparing"):
            self.assertEqual(self.action(detailed_merge_status=status), "wait", status)

    def test_stops_with_a_reason_when_landing_is_unsafe(self):
        cases = {
            "closed": dict(state="closed"),
            "draft": dict(draft=True),
            "conflicts": dict(has_conflicts=True),
            "threads": dict(blocking_discussions_resolved=False),
            "pipeline failed": dict(head_pipeline={"status": "failed", "sha": "abc"}),
            "approval": dict(detailed_merge_status="not_approved"),
        }
        for expected, overrides in cases.items():
            decision = ht.land_decision(mr(**overrides))
            self.assertEqual(decision["action"], "stop", expected)
            self.assertIn(expected, decision["reason"])


class JsonOnlyReordered(unittest.TestCase):
    def test_same_content_in_another_key_order_is_only_reordered(self):
        self.assertTrue(ht.json_only_reordered('{"a": 1, "b": {"c": 2, "d": 3}}', '{"b": {"d": 3, "c": 2}, "a": 1}'))

    def test_a_real_change_or_unparseable_text_is_not(self):
        self.assertFalse(ht.json_only_reordered('{"a": 1}', '{"a": 2}'))
        self.assertFalse(ht.json_only_reordered('{"a": 1}', '{"a": 1'))


class RestartRelevant(unittest.TestCase):
    def test_a_profiles_globs_pick_the_files_its_dev_server_only_reads_at_startup(self):
        globs = ["pnpm-lock.yaml", "**/package.json", "**/metro.config.*", "**/app.json", "**/.env.example", ".nvmrc"]
        paths = [
            "pnpm-lock.yaml", "package.json", "apps/community/package.json", "apps/community/metro.config.js",
            "apps/community/app.json", "apps/community/.env.example", ".nvmrc",
            "apps/community/src/app/index.tsx", "docs/architecture.md",
        ]
        self.assertEqual(ht.restart_relevant(paths, globs), paths[:7])

    def test_the_default_globs_cover_common_ecosystems(self):
        relevant = [
            "pnpm-lock.yaml", "web/package-lock.json", "yarn.lock", "go.mod", "go.sum", "requirements-dev.txt",
            "pyproject.toml", "uv.lock", "Gemfile.lock", "src/Api/Api.csproj", "Directory.Packages.props",
            "Cargo.lock", "docker-compose.override.yml", ".env.example", ".tool-versions",
        ]
        irrelevant = ["src/main.go", "app/models.py", "src/Api/Program.cs", "README.md"]
        self.assertEqual(ht.restart_relevant(relevant + irrelevant, ht.DEFAULT_RESTART_ON), relevant)


class MirrorText(unittest.TestCase):
    def test_without_a_marker_the_copy_becomes_the_source(self):
        self.assertEqual(ht.mirror_text("old\n", "new\nbody\n", None), "new\nbody\n")

    def test_with_a_marker_the_copy_keeps_its_own_lines_above_it_and_takes_the_rest_from_the_source(self):
        copy = "# Guide\nmy notes\n## Contents\nold body\n"
        source = "# Guide\n## Contents\nnew body\n"
        self.assertEqual(ht.mirror_text(copy, source, "## Contents"), "# Guide\nmy notes\n## Contents\nnew body\n")

    def test_the_marker_must_be_a_whole_line_in_both_files(self):
        with self.assertRaisesRegex(ht.HerdrError, "copy"):
            ht.mirror_text("# Guide\n## Contents of x\n", "## Contents\n", "## Contents")
        with self.assertRaisesRegex(ht.HerdrError, "source"):
            ht.mirror_text("## Contents\n", "no marker\n", "## Contents")


class SyncMirrors(unittest.TestCase):
    def setUp(self):
        self.main = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.backups = os.path.join(self.home, "backups")
        os.makedirs(os.path.join(self.main, "docs"))
        self.write(os.path.join(self.main, "docs", "guide.md"), "# Guide\n## Contents\nnew body\n")
        self.copy = os.path.join(self.home, "guide.md")
        self.addCleanup(shutil.rmtree, self.main)
        self.addCleanup(shutil.rmtree, self.home)

    def write(self, path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def read(self, path):
        with open(path) as handle:
            return handle.read()

    def sync(self, marker="## Contents", dry_run=False):
        mirrors = [{"source": "docs/guide.md", "copy": self.copy, "from": marker}]
        return ht.sync_mirrors(self.main, mirrors, self.backups, stamp="20261020-0400", dry_run=dry_run)

    def test_updates_a_stale_copy_and_keeps_a_backup_of_the_old_one(self):
        self.write(self.copy, "# Guide\nmine\n## Contents\nold body\n")
        result = self.sync()
        self.assertEqual(self.read(self.copy), "# Guide\nmine\n## Contents\nnew body\n")
        self.assertEqual([s["copy"] for s in result["synced"]], [self.copy])
        self.assertEqual(self.read(result["synced"][0]["backup"]), "# Guide\nmine\n## Contents\nold body\n")
        self.assertEqual(result["unchanged"], [])

    def test_a_copy_already_in_sync_is_left_alone_without_a_backup(self):
        self.write(self.copy, "# Guide\nmine\n## Contents\nnew body\n")
        result = self.sync()
        self.assertEqual(result["synced"], [])
        self.assertEqual(result["unchanged"], [self.copy])
        self.assertFalse(os.path.exists(self.backups))

    def test_a_dry_run_reports_what_would_change_and_writes_nothing(self):
        self.write(self.copy, "# Guide\n## Contents\nold body\n")
        result = self.sync(dry_run=True)
        self.assertEqual([s["copy"] for s in result["synced"]], [self.copy])
        self.assertEqual(self.read(self.copy), "# Guide\n## Contents\nold body\n")
        self.assertFalse(os.path.exists(self.backups))

    def test_a_missing_marker_or_file_is_a_problem_and_nothing_is_written(self):
        self.write(self.copy, "# Guide\nno marker here\n")
        result = self.sync()
        self.assertEqual(self.read(self.copy), "# Guide\nno marker here\n")
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("copy", result["problems"][0]["problem"])
        os.remove(self.copy)
        self.assertIn("missing", self.sync()["problems"][0]["problem"])


class FactsChanged(unittest.TestCase):
    def test_picks_the_changed_files_a_planning_page_summarises(self):
        planning = {"facts_on": ["docs/adr/*.md", "packages/*/package.json"]}
        changed = ["docs/adr/0029-x.md", "packages/new/package.json", "packages/ui/src/a.ts", "docs/testing.md"]
        self.assertEqual(ht.facts_changed(changed, planning), ["docs/adr/0029-x.md", "packages/new/package.json"])

    def test_nothing_without_a_planning_page_or_its_globs(self):
        self.assertEqual(ht.facts_changed(["docs/adr/0029-x.md"], None), [])
        self.assertEqual(ht.facts_changed(["docs/adr/0029-x.md"], {"artifact": "x"}), [])


class ForgeFromRemote(unittest.TestCase):
    def test_detects_the_forge_from_the_origin_url(self):
        self.assertEqual(ht.forge_from_remote("git@gitlab.com:example-org/apps/x.git"), "gitlab")
        self.assertEqual(ht.forge_from_remote("https://gitlab.example.com/group/x.git"), "gitlab")
        self.assertEqual(ht.forge_from_remote("git@github.com:longpshorn/drydock.git"), "github")
        self.assertEqual(ht.forge_from_remote("https://github.com/org/repo"), "github")
        self.assertIsNone(ht.forge_from_remote("ssh://git.internal/repo.git"))


PROFILES = [
    {"name": "mobile", "match": {"remote": "example-org/apps/mobile-app"}, "forge": "gitlab",
     "tracker": {"kind": "jira", "done_path": ["Review", "Closed"]},
     "dev_server": {"command": "pnpm community", "ready": "Waiting on"}},
    {"name": "local", "match": {"path": "/code/local-thing"}},
]


class SelectProfile(unittest.TestCase):
    def test_matches_by_remote_or_by_main_checkout_path(self):
        remote = "git@gitlab.com:example-org/apps/mobile-app.git"
        self.assertEqual(ht.select_profile(PROFILES, "/x/mobile-app", remote)["name"], "mobile")
        self.assertEqual(ht.select_profile(PROFILES, "/code/local-thing", "")["name"], "local")
        self.assertIsNone(ht.select_profile(PROFILES, "/code/other", "git@github.com:me/other.git"))


class ResolveProfile(unittest.TestCase):
    def test_a_repo_without_a_profile_gets_working_defaults(self):
        resolved = ht.resolve_profile(None, "git@github.com:longpshorn/drydock.git")
        self.assertEqual(resolved["forge"], "github")
        self.assertEqual(resolved["tracker"]["kind"], "none")
        self.assertIsNone(resolved["dev_server"])
        self.assertEqual(resolved["restart_on"], ht.DEFAULT_RESTART_ON)
        self.assertEqual(resolved["brief"]["mr_create"], "gh pr create")
        self.assertIn("forge", resolved["defaulted"])

    def test_a_profile_overrides_defaults_field_by_field(self):
        resolved = ht.resolve_profile(PROFILES[0], "git@gitlab.com:example-org/apps/mobile-app.git")
        self.assertEqual(resolved["name"], "mobile")
        self.assertEqual(resolved["tracker"]["done_path"], ["Review", "Closed"])
        self.assertEqual(resolved["dev_server"]["command"], "pnpm community")
        self.assertEqual(resolved["brief"]["mr_create"], "glab mr create")
        self.assertNotIn("tracker", resolved["defaulted"])
        self.assertIn("worktree", resolved["defaulted"])

    def test_rejects_unknown_keys(self):
        with self.assertRaisesRegex(ht.HerdrError, "unknown"):
            ht.resolve_profile({"name": "x", "dev_sever": {}}, "")


class LoadProfile(unittest.TestCase):
    """A repo's own profile file, then a personal overlay matched to the repo laid over it, then the defaults."""

    REMOTE = "git@gitlab.com:example-org/apps/mobile-app.git"

    def setUp(self):
        self.main = tempfile.mkdtemp()
        self.overlays = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.main)
        self.addCleanup(shutil.rmtree, self.overlays)

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump(data, handle)

    def repo_file(self, data):
        path = os.path.join(self.main, ".claude", "herdr-profile.json")
        self.write_json(path, data)
        return path

    def overlay(self, name, data):
        path = os.path.join(self.overlays, name)
        self.write_json(path, data)
        return path

    def load(self):
        return ht.load_profile(self.main, self.REMOTE, self.overlays)

    def test_reads_the_repos_own_profile_file(self):
        path = self.repo_file({"name": "mobile", "forge": "gitlab", "tracker": {"kind": "jira", "project": "RES"}})
        profile, sources = self.load()
        self.assertEqual(profile["tracker"]["project"], "RES")
        self.assertEqual(sources, {"repo": path, "overlay": None})

    def test_a_matching_overlay_is_laid_over_the_repo_file_one_level_deep(self):
        self.repo_file({"name": "mobile", "tracker": {"kind": "jira", "project": "RES"},
                        "brief": {"implement": "repo's way", "notes": ["a"]}})
        path = self.overlay("mobile.json", {"match": {"remote": "apps/mobile-app"},
                                            "planning": {"artifact": "https://claude.ai/artifact/x"},
                                            "brief": {"notes": ["mine"]}})
        profile, sources = self.load()
        self.assertEqual(profile["name"], "mobile")
        self.assertEqual(profile["tracker"], {"kind": "jira", "project": "RES"})
        self.assertEqual(profile["planning"]["artifact"], "https://claude.ai/artifact/x")
        self.assertEqual(profile["brief"], {"implement": "repo's way", "notes": ["mine"]})
        self.assertEqual(sources["overlay"], path)

    def test_an_overlay_alone_still_applies_when_the_repo_has_no_file(self):
        self.overlay("mine.json", {"name": "mine", "match": {"path": self.main}, "dev_server": {"command": "make run"}})
        profile, sources = self.load()
        self.assertEqual(profile["dev_server"]["command"], "make run")
        self.assertEqual(sources["repo"], None)

    def test_an_overlay_for_another_repo_is_ignored(self):
        self.repo_file({"name": "mobile"})
        self.overlay("other.json", {"match": {"remote": "someone/else"}, "planning": {"artifact": "x"}})
        profile, sources = self.load()
        self.assertNotIn("planning", profile)
        self.assertIsNone(sources["overlay"])

    def test_without_either_file_the_defaults_apply(self):
        profile, sources = self.load()
        self.assertIsNone(profile)
        self.assertEqual(sources, {"repo": None, "overlay": None})
        self.assertEqual(ht.resolve_profile(profile, self.REMOTE)["forge"], "gitlab")

    def test_unknown_keys_in_either_file_are_refused_by_name(self):
        self.repo_file({"name": "mobile"})
        self.overlay("mine.json", {"match": {"path": self.main}, "planing": {}})
        with self.assertRaisesRegex(ht.HerdrError, "planing"):
            ht.resolve_profile(self.load()[0], self.REMOTE)


class ApprovalState(unittest.TestCase):
    def test_reads_gitlabs_approvals(self):
        missing = ht.gitlab_approval({"approved": False, "approvals_required": 1, "approvals_left": 1, "approved_by": []})
        self.assertEqual((missing["approved"], missing["left"]), (False, 1))
        given = ht.gitlab_approval({"approved": True, "approvals_required": 1, "approvals_left": 0,
                                    "approved_by": [{"user": {"username": "alice"}}]})
        self.assertEqual((given["approved"], given["by"]), (True, ["alice"]))
        self.assertTrue(ht.gitlab_approval({"approved": True, "approvals_required": 0, "approvals_left": 0})["approved"])

    def test_reads_githubs_review_decision(self):
        self.assertFalse(ht.github_pr_to_mr(pr(reviewDecision="REVIEW_REQUIRED"))["approvals"]["approved"])
        self.assertFalse(ht.github_pr_to_mr(pr(reviewDecision="CHANGES_REQUESTED"))["approvals"]["approved"])
        self.assertTrue(ht.github_pr_to_mr(pr(reviewDecision="APPROVED"))["approvals"]["approved"])
        self.assertTrue(ht.github_pr_to_mr(pr(reviewDecision=""))["approvals"]["approved"])

    def test_landing_stops_early_and_says_an_approval_is_missing(self):
        approvals = {"approved": False, "required": 1, "left": 1, "by": []}
        for overrides in ({"approvals": approvals}, {"approvals": approvals, "detailed_merge_status": "need_rebase"},
                          {"detailed_merge_status": "not_approved"}):
            decision = ht.land_decision(mr(**overrides))
            self.assertEqual(decision["action"], "stop", overrides)
            self.assertIn("approval from someone other than its author", decision["reason"])

    def test_an_approved_or_approval_free_mr_lands(self):
        self.assertEqual(ht.land_decision(mr(approvals={"approved": True, "required": 1, "left": 0, "by": ["a"]}))["action"], "merge")
        self.assertEqual(ht.land_decision(mr(approvals={"approved": True, "required": 0, "left": 0, "by": []}))["action"], "merge")

    def test_retire_reports_the_approval_check(self):
        report = ht.parse_status_report(REPORT)
        def approved_check(approval):
            checks = ht.retire_checks("idle", "", "opened", True, report, approval=approval)
            return next(c for c in checks if c["check"] == "approved")
        missing = approved_check({"approved": False, "required": 1, "left": 1, "by": []})
        self.assertFalse(missing["ok"])
        self.assertIn("needs 1 approval from someone other than its author", missing["detail"])
        self.assertTrue(approved_check({"approved": True, "required": 1, "left": 0, "by": ["alice"]})["ok"])
        self.assertIn("alice", approved_check({"approved": True, "required": 1, "left": 0, "by": ["alice"]})["detail"])
        self.assertTrue(approved_check(None)["ok"])


READY_REPORT = """---
agent: res1
state: mr-open
pipeline: passed
safe_to_retire: no
---
## Summary
All done; ready to land.

## Open questions
None
"""


class ReportReady(unittest.TestCase):
    def test_ready_when_the_mr_is_open_its_pipeline_passed_it_says_so_and_nothing_is_open(self):
        self.assertTrue(ht.report_ready(ht.parse_status_report(READY_REPORT)))

    def test_not_ready_otherwise(self):
        for change in (("state: mr-open", "state: in-progress"), ("pipeline: passed", "pipeline: running"),
                       ("ready to land", "nearly there"), ("## Open questions\nNone", "## Open questions\nWhich colour?")):
            self.assertFalse(ht.report_ready(ht.parse_status_report(READY_REPORT.replace(*change))), change)
        self.assertFalse(ht.report_ready(None))


class WatchLoop(unittest.TestCase):
    def run_watch(self, reports, land, max_seconds=10):
        self.asked, self.landed_calls = [], []
        clock = {"t": 0}
        def land_fn(agent):
            self.landed_calls.append(agent)
            return land(agent)
        def sleep(seconds):
            clock["t"] += seconds
        return ht.watch_loop(
            list(reports), land_fn, lambda agent, message: self.asked.append((agent, message)),
            lambda agent: reports[agent]() if callable(reports[agent]) else reports[agent],
            max_seconds=max_seconds, poll_seconds=1, sleep=sleep, now=lambda: clock["t"])

    def test_lands_the_first_ready_agent_and_stops(self):
        ready, waiting = ht.parse_status_report(READY_REPORT), ht.parse_status_report(READY_REPORT.replace("passed", "running"))
        result = self.run_watch({"a": waiting, "b": ready}, lambda agent: {"state": "merged", "iid": 3})
        self.assertEqual(result["landed"], "b")
        self.assertEqual(self.landed_calls, ["b"])

    def test_asks_a_conflicting_agent_to_rebase_once_and_keeps_watching(self):
        ready = ht.parse_status_report(READY_REPORT)
        def land(agent):
            raise ht.HerdrError("Not landing !3: MR has conflicts; the agent must resolve them.")
        with self.assertRaisesRegex(ht.HerdrError, "Nothing landed"):
            self.run_watch({"a": ready}, land, max_seconds=3)
        self.assertEqual([a for a, _ in self.asked], ["a"])
        self.assertIn("rebase", self.asked[0][1].lower())
        self.assertGreater(len(self.landed_calls), 1)

    def test_an_unapproved_mr_keeps_waiting_without_prompting_the_agent(self):
        ready = ht.parse_status_report(READY_REPORT)
        calls = iter([ht.HerdrError("Not landing !3: needs approval from someone other than its author."), None])
        def land(agent):
            error = next(calls)
            if error:
                raise error
            return {"state": "merged", "iid": 3}
        self.assertEqual(self.run_watch({"a": ready}, land)["landed"], "a")
        self.assertEqual(self.asked, [])


CONFLICT = ht.HerdrError("Not landing !3: MR has conflicts; the agent must resolve them.")


class LandQueue(unittest.TestCase):
    def run_queue(self, outcomes, max_seconds=10):
        """outcomes: agent -> list of results per attempt (an exception is raised); the last one repeats."""
        self.asked, self.calls = [], []
        clock = {"t": 0}
        def land_fn(agent):
            self.calls.append(agent)
            results = outcomes[agent]
            item = results.pop(0) if len(results) > 1 else results[0]
            if isinstance(item, Exception):
                raise item
            return item
        def sleep(seconds):
            clock["t"] += seconds
        return ht.land_queue(list(outcomes), land_fn, lambda agent, message: self.asked.append((agent, message)),
                             max_seconds=max_seconds, poll_seconds=1, sleep=sleep, now=lambda: clock["t"])

    def test_lands_each_in_the_given_order(self):
        merged = {"state": "merged", "iid": 3, "commit": "c0ffee12345"}
        results = self.run_queue({"c": [merged], "a": [merged], "b": [merged]})
        self.assertEqual(self.calls, ["c", "a", "b"])
        self.assertEqual([(r["agent"], r["result"]) for r in results], [("c", "merged"), ("a", "merged"), ("b", "merged")])

    def test_a_conflict_asks_the_agent_to_rebase_keeps_going_and_retries_it_at_the_end(self):
        merged = {"state": "merged", "iid": 3}
        results = self.run_queue({"a": [CONFLICT, merged], "b": [merged]})
        self.assertEqual(self.calls, ["a", "b", "a"])
        self.assertEqual([a for a, _ in self.asked], ["a"])
        self.assertIn("rebase", self.asked[0][1].lower())
        self.assertEqual([(r["agent"], r["result"]) for r in results], [("a", "merged"), ("b", "merged")])

    def test_an_agent_still_conflicting_at_the_deadline_is_reported_and_asked_only_once(self):
        results = self.run_queue({"a": [CONFLICT], "b": [{"state": "merged", "iid": 4}]}, max_seconds=3)
        self.assertEqual([a for a, _ in self.asked], ["a"])
        self.assertEqual({r["agent"]: r["result"] for r in results}, {"a": "conflict", "b": "merged"})

    def test_any_other_refusal_fails_that_agent_without_retrying_it(self):
        results = self.run_queue({"a": [ht.HerdrError("Not landing !3: head pipeline failed.")],
                                  "b": [{"state": "merged", "iid": 4}]})
        self.assertEqual(self.calls, ["a", "b"])
        self.assertEqual(results[0]["result"], "failed")
        self.assertIn("head pipeline failed", results[0]["detail"])
        self.assertEqual(self.asked, [])

    def test_one_line_per_agent(self):
        lines = ht.format_land_results([
            {"agent": "a", "result": "merged", "detail": "!3 merged as c0ffee12"},
            {"agent": "b", "result": "conflict", "detail": "still conflicts"},
        ])
        self.assertEqual(lines.splitlines(), ["a: merged (!3 merged as c0ffee12)", "b: conflict (still conflicts)"])


WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "herdr-tools")


@unittest.skipUnless(shutil.which("sh"), "needs sh (Git Bash on Windows)")
class Wrapper(unittest.TestCase):
    """The skills run the helper through `sh herdr-tools`, which finds a Python 3.9+ under any of its usual names."""

    def run_wrapper(self, path=None):
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        return subprocess.run([shutil.which("sh"), WRAPPER, "--help"], capture_output=True, text=True, env=env)

    def test_runs_the_helper(self):
        proc = self.run_wrapper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("herdr_tools.py", proc.stdout)

    def fake_bin(self, scripts):
        """A folder holding only `dirname` and the given fake commands, so no real Python is on PATH."""
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        os.symlink(shutil.which("dirname"), os.path.join(folder, "dirname"))
        for name, body in scripts.items():
            with open(os.path.join(folder, name), "w") as handle:
                handle.write("#!/bin/sh\n" + body + "\n")
            os.chmod(os.path.join(folder, name), 0o755)
        return folder

    @unittest.skipIf(os.name == "nt", "fake commands on PATH are POSIX scripts")
    def test_skips_a_python3_that_does_not_run_and_uses_python(self):
        folder = self.fake_bin({"python3": "exit 9009", "python": 'exec "%s" "$@"' % sys.executable})
        proc = self.run_wrapper(folder)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("herdr_tools.py", proc.stdout)

    @unittest.skipIf(os.name == "nt", "fake commands on PATH are POSIX scripts")
    def test_says_what_it_tried_when_there_is_no_python(self):
        proc = self.run_wrapper(self.fake_bin({"python3": "exit 9009"}))
        self.assertEqual(proc.returncode, 127)
        self.assertIn("python3, python, py -3", proc.stderr)


class SetupScan(unittest.TestCase):
    """What /herdr:setup reads off a repo: forge, tracker hints, package manager, and which practices it lacks."""

    def repo(self, files):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(path, name)) or path, exist_ok=True)
            write(path, name, text if isinstance(text, str) else json.dumps(text))
        return path

    def scan(self, files, remote="git@github.com:acme/app.git", history=()):
        return ht.setup_scan(self.repo(files), remote, list(history))

    def practices(self, result):
        return {p["practice"]: p for p in result["practices"]}

    def test_a_bare_github_repo_lacks_every_practice_and_gets_the_minimal_profile(self):
        result = self.scan({"README.md": "# app\n"})
        self.assertEqual(result["forge"], "github")
        self.assertIsNone(result["tracker"])
        self.assertEqual(result["closest_example"], "minimal")
        self.assertEqual(result["proposed_profile"], {"name": os.path.basename(result["path"]), "forge": "github"})
        practices = self.practices(result)
        self.assertEqual(sorted(practices), sorted(["worktree", "agent_lifecycle", "stop_hook", "shared_concepts",
                                                    "doc_checks", "mr_template", "personal_install"]))
        self.assertFalse(any(p["present"] for p in practices.values()))
        for practice in practices.values():
            self.assertTrue(practice["why"])
            self.assertTrue(practice["copy"], practice["practice"])
        self.assertIn(".github/pull_request_template.md", practices["mr_template"]["copy"])

    def test_ticket_keys_in_branches_and_commits_suggest_a_jira_tracker(self):
        result = self.scan({}, remote="https://gitlab.example.com/acme/app.git",
                           history=["feat/ACME-12-login", "fix: ACME-15 crash", "chore: deps", "feat/OTHER-1-x"])
        self.assertEqual(result["tracker"], {"kind": "jira", "project": "ACME"})
        self.assertEqual(result["closest_example"], "full")
        self.assertEqual(result["proposed_profile"]["tracker"]["project"], "ACME")
        self.assertEqual(result["forge"], "gitlab")
        self.assertIn(".gitlab/merge_request_templates/Default.md", self.practices(result)["mr_template"]["copy"])

    def test_a_single_ticket_key_is_not_a_tracker(self):
        self.assertIsNone(self.scan({}, history=["feat/ACME-12-login", "chore: deps"])["tracker"])

    def test_reads_the_package_manager_and_its_scripts_into_the_profile(self):
        result = self.scan({"pnpm-lock.yaml": "", "package.json": {"scripts": {
            "dev": "vite", "worktree": "node scripts/worktree.mjs"}}})
        self.assertEqual(result["package_manager"], "pnpm")
        profile = result["proposed_profile"]
        self.assertEqual(profile["dev_server"], {"command": "pnpm dev"})
        self.assertEqual(profile["worktree"], {"new": "pnpm worktree new {branch}", "remove": "pnpm worktree remove {branch}"})
        self.assertTrue(self.practices(result)["worktree"]["present"])

    def test_spots_the_practices_a_repo_already_has(self):
        practices = self.practices(self.scan({
            "AGENTS.md": "# App\n\n## Development lifecycle (agents)\nNever merge.\n",
            "CLAUDE.md": "@AGENTS.md\n",
            ".claude/settings.json": {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}},
            "docs/shared-concepts.md": "## Registry\n",
            "package.json": {"scripts": {"docs:impact": "node scripts/docs-impact.mjs"}},
            ".github/pull_request_template.md": "## Docs reviewed\n",
            "scripts/bootstrap.mjs": "install('herdr@herdr-skills')",
        }))
        self.assertEqual([name for name, p in practices.items() if not p["present"]], ["worktree"])

    def test_a_lifecycle_still_kept_in_claude_md_counts(self):
        practices = self.practices(self.scan({"CLAUDE.md": "# App\n\n## Development lifecycle (agents)\nNever merge.\n"}))
        self.assertTrue(practices["agent_lifecycle"]["present"])

    def test_flags_the_plugin_in_the_projects_committed_settings(self):
        result = self.scan({".claude/settings.json": {"enabledPlugins": {"herdr@herdr-skills": True}}})
        self.assertTrue(result["plugin_in_committed_settings"])
        self.assertFalse(self.scan({})["plugin_in_committed_settings"])

    def test_reports_an_existing_profile_instead_of_proposing_one(self):
        result = self.scan({".claude/herdr-profile.json": {"name": "app", "forge": "github"}})
        self.assertTrue(result["profile"].endswith("herdr-profile.json"))
        self.assertIsNone(result["proposed_profile"])

    def test_every_file_it_suggests_copying_exists_in_the_full_example(self):
        full = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "full")
        for forge in ("github", "gitlab"):
            for practice in ht.setup_scan(self.repo({}), "git@%s.com:a/b.git" % forge, [])["practices"]:
                for name in practice["copy"]:
                    self.assertTrue(os.path.isfile(os.path.join(full, name)), name)

    def test_a_practice_copied_alone_carries_every_file_its_copies_import_or_run(self):
        full = os.path.join(EXAMPLES, "full")
        relative = re.compile(r"""(?:^import\s[^'"]*?from\s+|new URL\(\s*)['"](\.\.?/[^'"]+)['"]""", re.M)
        from_root = re.compile(r"""['"](scripts/[\w./-]+\.mjs)['"]""")
        for practice in ht.setup_scan(self.repo({}), "git@github.com:a/b.git", [])["practices"]:
            copy = set(practice["copy"])
            for name in [n for n in copy if n.endswith(".mjs")]:
                with open(os.path.join(full, name), encoding="utf-8") as handle:
                    text = handle.read()
                needs = {posixpath.normpath(posixpath.join(posixpath.dirname(name), m)) for m in relative.findall(text)}
                needs |= set(from_root.findall(text))
                self.assertEqual(sorted(needs - copy), [], "%s: %s" % (practice["practice"], name))

    def test_the_worktree_practice_ignores_the_env_file_its_bootstrap_writes(self):
        # an untracked .env.local makes `worktree remove` refuse every worktree it created
        copy = self.practices(self.scan({}))["worktree"]["copy"]
        self.assertIn(".gitignore", copy)
        with open(os.path.join(EXAMPLES, "full", ".gitignore"), encoding="utf-8") as handle:
            self.assertIn(".env.local", handle.read().split())

    def test_doc_checks_written_in_another_language_count(self):
        for name in ("scripts/doc_parity.py", "bin/docs_impact.sh"):
            self.assertTrue(self.practices(self.scan({name: ""}))["doc_checks"]["present"], name)


EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


class ExampleProfiles(unittest.TestCase):
    """The examples are what people copy, so each must load through the same code a real profile does."""

    def load(self, *parts):
        with open(os.path.join(EXAMPLES, *parts)) as handle:
            return json.load(handle)

    def test_the_minimal_profile_is_a_github_repo_on_defaults(self):
        resolved = ht.resolve_profile(self.load("minimal", ".claude", "herdr-profile.json"), "")
        self.assertEqual((resolved["forge"], resolved["tracker"]["kind"]), ("github", "none"))

    def test_the_full_profile_uses_gitlab_and_jira(self):
        resolved = ht.resolve_profile(self.load("full", ".claude", "herdr-profile.json"), "")
        self.assertEqual((resolved["forge"], resolved["tracker"]["kind"]), ("gitlab", "jira"))
        self.assertIn("{branch}", resolved["worktree"]["new"])

    def test_the_overlay_matches_a_repo_and_adds_only_personal_fields(self):
        overlay = self.load("minimal", "overlay.example.json")
        self.assertTrue(ht.select_profile([overlay], "/nowhere", "git@github.com:acme/acme-app.git"))
        merged = ht.merge_profiles(self.load("minimal", ".claude", "herdr-profile.json"), overlay)
        resolved = ht.resolve_profile(merged, "")
        self.assertTrue(resolved["planning"])
        self.assertTrue(resolved["mirrors"])
        self.assertEqual(set(overlay) - {"name", "match"}, {"planning", "mirrors"})


class Shareable(unittest.TestCase):
    """The plugin is shared: nothing in it may name one person or depend on one machine's checkout."""

    def test_no_file_names_a_person_or_a_personal_path(self):
        plugin = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for folder, _, files in os.walk(plugin):
            if os.path.basename(folder) == ".claude-plugin":
                continue  # the manifest's author is attribution, not a person the skills address
            for name in files:
                if name == os.path.basename(__file__) or name.endswith(".pyc"):
                    continue
                with open(os.path.join(folder, name)) as handle:
                    text = handle.read()
                for needle in ("Patrick", "~/code/drydock", "~/code/.work", "~/code/docs"):
                    if needle in text:
                        offenders.append("%s: %s" % (os.path.relpath(os.path.join(folder, name), plugin), needle))
        self.assertEqual(offenders, [])

    def test_commands_in_the_docs_quote_their_paths(self):
        plugin = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path_argument = re.compile(r'(?:--cwd|--path|-C|worktree remove|worktree add -b \S+) (?!")\S*<')
        offenders = []
        for folder, _, files in os.walk(plugin):
            for name in (n for n in files if n.endswith(".md")):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        for command in re.findall(r"`([^`]+)`", line):
                            if path_argument.search(command):
                                offenders.append("%s:%d: %s" % (
                                    os.path.relpath(os.path.join(folder, name), plugin), number, command))
        self.assertEqual(offenders, [])

    def test_the_default_plan_gate_names_no_one(self):
        self.assertNotIn("Patrick", ht.resolve_profile(None, "")["brief"]["implement"])


def pr(**overrides):
    base = {
        "state": "OPEN", "isDraft": False, "headRefOid": "abc", "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "number": 7, "url": "https://github.com/o/r/pull/7",
        "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    base.update(overrides)
    return base


class GitHubNormalization(unittest.TestCase):
    def action(self, **overrides):
        return ht.land_decision(ht.github_pr_to_mr(pr(**overrides)))["action"]

    def test_a_clean_pr_with_passing_checks_merges(self):
        self.assertEqual(self.action(), "merge")
        self.assertEqual(self.action(statusCheckRollup=[]), "merge")

    def test_behind_needs_a_rebase_and_running_checks_wait(self):
        self.assertEqual(self.action(mergeStateStatus="BEHIND"), "rebase")
        self.assertEqual(self.action(statusCheckRollup=[{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]), "wait")
        self.assertEqual(self.action(statusCheckRollup=[{"__typename": "StatusContext", "state": "PENDING"}]), "wait")
        self.assertEqual(self.action(mergeStateStatus="UNKNOWN"), "wait")

    def test_unsafe_prs_stop(self):
        self.assertEqual(self.action(statusCheckRollup=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]), "stop")
        self.assertEqual(self.action(isDraft=True), "stop")
        self.assertEqual(self.action(mergeable="CONFLICTING", mergeStateStatus="DIRTY"), "stop")
        self.assertEqual(self.action(mergeStateStatus="BLOCKED"), "stop")
        self.assertEqual(self.action(state="CLOSED"), "stop")
        self.assertEqual(self.action(state="MERGED"), "done")

    def test_the_head_branch_is_the_source_branch(self):
        self.assertEqual(ht.github_pr_to_mr(pr(headRefName="feat/x"))["source_branch"], "feat/x")


class GitHubMerge(unittest.TestCase):
    """Landing a PR leaves the agent's local branch and worktree to retire (gh's --delete-branch removes both), and
    deletes only the remote branch. Real git; `gh` is stubbed."""

    def setUp(self):
        self.main, run = git_repo(self)
        write(self.main, "a.txt", "a\n")
        run("add", "a.txt")
        run("commit", "-q", "-m", "a")
        self.worktree = os.path.join(tempfile.mkdtemp(), "wt")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.worktree), ignore_errors=True)
        run("worktree", "add", "-q", "-b", "feat/x", self.worktree)
        self.gh_calls, self.delete_fails, self.fail_merge, self.sleeps = [], False, False, []
        self.deletes = []  # (returncode, stderr) per DELETE, in order; then delete_fails decides
        self.ref_check = (1, "gh: Unprocessable Entity (HTTP 422)")
        self.read_failures = 0  # reads after the merge that fail before one succeeds
        real_run = subprocess.run
        def fake_run(args, **kw):
            if args[0] != "gh":
                return real_run(args, **kw)
            self.gh_calls.append(args)
            if args[1:3] == ["pr", "view"] and ht.GitHubForge.FIELDS in args:
                out = json.dumps(pr(number=4, url="https://github.com/o/r/pull/4", headRefName="feat/x"))
                return subprocess.CompletedProcess(args, 0, out, "")
            if args[1:3] == ["pr", "view"] and self.read_failures:
                self.read_failures -= 1
                return subprocess.CompletedProcess(args, 1, "", "error connecting to api.github.com: TLS handshake timeout")
            if args[1:3] == ["pr", "view"]:
                out = json.dumps({"state": "MERGED", "url": "https://github.com/o/r/pull/4",
                                  "mergeCommit": {"oid": "m1"}, "headRefName": "feat/x"})
                return subprocess.CompletedProcess(args, 0, out, "")
            if args[1:3] == ["pr", "merge"] and self.fail_merge:
                return subprocess.CompletedProcess(args, 1, "", "Pull request is not mergeable")
            if args[1] == "api" and "DELETE" in args and self.deletes:
                code, err = self.deletes.pop(0)
                return subprocess.CompletedProcess(args, code, "", err)
            if args[1] == "api" and "DELETE" not in args:
                return subprocess.CompletedProcess(args, self.ref_check[0], "", self.ref_check[1])
            if args[1] == "api" and self.delete_fails:
                return subprocess.CompletedProcess(args, 1, "", "gh: Reference does not exist (HTTP 422)")
            return subprocess.CompletedProcess(args, 0, "", "")
        ht.subprocess.run = fake_run
        self.addCleanup(setattr, ht.subprocess, "run", real_run)

    def gh(self, *prefix):
        return [c for c in self.gh_calls if c[1:1 + len(prefix)] == list(prefix)]

    def forge(self, repo=None):
        return ht.GitHubForge(self.main, repo, sleep=self.sleeps.append)

    def test_leaves_the_worktree_and_local_branch_for_retire(self):
        result = ht.GitHubForge(self.main).merge(4, "abc", "feat/x")
        self.assertEqual(result, {"state": "merged", "commit": "m1", "url": "https://github.com/o/r/pull/4"})
        self.assertTrue(os.path.isdir(self.worktree))
        listed = [os.path.realpath(line[len("worktree "):]) for line in
                  ht.git(self.main, "worktree", "list", "--porcelain")[1].splitlines() if line.startswith("worktree ")]
        self.assertIn(os.path.realpath(self.worktree), listed)
        self.assertEqual(ht.git(self.main, "rev-parse", "--verify", "-q", "refs/heads/feat/x")[0], 0)
        merge = self.gh("pr", "merge")[0]
        self.assertNotIn("--delete-branch", merge)
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], "abc")

    def test_deletes_only_the_remote_branch(self):
        ht.GitHubForge(self.main).merge(4, "abc", "feat/x")
        self.assertEqual(self.gh("api"), [["gh", "api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/feat/x"]])

    def test_a_pr_in_another_repo_deletes_that_repos_branch(self):
        ht.GitHubForge(self.main, "o/r").merge(4, "abc", "feat/x")
        self.assertEqual(self.gh("api")[0][-1], "repos/o/r/git/refs/heads/feat/x")

    def test_a_failed_remote_delete_is_a_warning_after_the_merge(self):
        self.delete_fails = True
        self.ref_check = (0, "")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.forge().merge(4, "abc", "feat/x")
        self.assertEqual(result, {"state": "merged", "commit": "m1", "url": "https://github.com/o/r/pull/4"})
        self.assertIn("feat/x", err.getvalue())
        self.assertIn("Reference does not exist", err.getvalue())
        self.assertEqual(len(self.gh("api", "-X", "DELETE")), 2)
        self.assertEqual(self.sleeps, [ht.GITHUB_RETRY_SECONDS])

    def test_a_transient_422_is_retried_once_after_a_short_wait(self):
        self.deletes = [(1, "gh: Reference does not exist (HTTP 422)"), (0, "")]
        self.ref_check = (0, "")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.forge("o/r").merge(4, "abc", "feat/x")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.gh("api", "-X", "DELETE")), 2)
        self.assertEqual(self.sleeps, [ht.GITHUB_RETRY_SECONDS])
        self.assertIn(["gh", "api", "repos/o/r/git/ref/heads/feat/x"], self.gh("api"))

    def test_a_branch_already_gone_is_success_without_waiting(self):
        self.deletes = [(1, "gh: Reference does not exist (HTTP 422)")]
        self.ref_check = (1, "gh: Not Found (HTTP 404)")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.forge().merge(4, "abc", "feat/x")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.gh("api", "-X", "DELETE")), 1)
        self.assertEqual(self.sleeps, [])

    def test_a_failed_ref_check_counts_as_the_branch_may_still_exist(self):
        self.deletes = [(1, "gh: Server Error (HTTP 502)"), (0, "")]
        self.ref_check = (1, "gh: Server Error (HTTP 502)")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.forge().merge(4, "abc", "feat/x")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.gh("api", "-X", "DELETE")), 2)
        self.assertEqual(self.sleeps, [ht.GITHUB_RETRY_SECONDS])

    def test_a_failed_merge_raises_and_deletes_nothing(self):
        self.fail_merge = True
        with self.assertRaisesRegex(ht.HerdrError, "gh pr merge failed"):
            ht.GitHubForge(self.main).merge(4, "abc", "feat/x")
        self.assertEqual(self.gh("api"), [])

    def test_the_delete_uses_the_branch_read_before_the_merge(self):
        self.forge().merge(4, "abc", "feat/y")
        self.assertEqual(self.gh("api", "-X", "DELETE")[0][-1], "repos/{owner}/{repo}/git/refs/heads/feat/y")

    def test_a_failed_read_after_the_merge_is_retried_once(self):
        self.read_failures = 1
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.forge().merge(4, "abc", "feat/x")
        self.assertEqual(result, {"state": "merged", "commit": "m1", "url": "https://github.com/o/r/pull/4"})
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.gh("pr", "view")), 2)
        self.assertEqual(self.sleeps, [ht.GITHUB_RETRY_SECONDS])

    def test_a_read_that_keeps_failing_still_reports_the_merge(self):
        self.read_failures = 2
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.forge().merge(4, "abc", "feat/x")
        self.assertEqual(result, {"state": "merged", "commit": None, "url": None})
        self.assertIn("warning: merged", err.getvalue())
        self.assertIn("TLS handshake timeout", err.getvalue())
        self.assertEqual(len(self.gh("pr", "view")), 2)
        self.assertEqual(self.gh("api", "-X", "DELETE")[0][-1], "repos/{owner}/{repo}/git/refs/heads/feat/x")

    def land(self):
        return ht.land_loop(self.forge(), 4, timeout_seconds=60, poll_seconds=0, sleep=lambda s: None)

    def test_a_single_land_whose_read_fails_reports_merged_with_the_pr_url(self):
        self.read_failures = 2
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.land()
        self.assertEqual((result["state"], result["commit"], result["url"]),
                         ("merged", None, "https://github.com/o/r/pull/4"))
        self.assertEqual(self.gh("api", "-X", "DELETE")[0][-1], "repos/{owner}/{repo}/git/refs/heads/feat/x")

    def test_landing_several_goes_on_after_a_failed_read(self):
        def land_fn(agent):
            self.read_failures = 2 if agent == "a" else 0
            return self.land()
        with contextlib.redirect_stderr(io.StringIO()):
            results = ht.land_queue(["a", "b", "c"], land_fn, lambda agent, message: None, max_seconds=10)
        self.assertEqual([(r["agent"], r["result"], r["detail"]) for r in results],
                         [("a", "merged", "!4 merged"), ("b", "merged", "!4 merged as m1"),
                          ("c", "merged", "!4 merged as m1")])


class ReportFollowups(unittest.TestCase):
    def test_lists_follow_ups_and_unverified_changes(self):
        report = ht.parse_status_report(REPORT.replace("## Follow-ups\n- File a ticket for the docs gap",
            "## Follow-ups\n- File a ticket for the docs gap\n- Check the banner\n  on a real device")
            .replace("- The widget (Verified)", "- The widget (Verified)\n- Offline reads on a phone (Unverified)"))
        items = ht.report_followups(report)
        self.assertEqual([i["text"] for i in items if i["kind"] == "follow-up"],
                         ["File a ticket for the docs gap", "Check the banner on a real device"])
        self.assertEqual([i["text"] for i in items if i["kind"] == "unverified"], ["Offline reads on a phone (Unverified)"])

    def test_none_and_missing_sections_give_nothing(self):
        report = ht.parse_status_report(REPORT.replace("- File a ticket for the docs gap", "None."))
        self.assertEqual(ht.report_followups(report), [])
        self.assertEqual(ht.report_followups(None), [])

    def test_a_profile_may_name_its_planning_page(self):
        resolved = ht.resolve_profile({"name": "x", "planning": {"artifact": "https://claude.ai/artifact/abc"}}, "")
        self.assertEqual(resolved["planning"]["artifact"], "https://claude.ai/artifact/abc")
        self.assertIsNone(ht.resolve_profile(None, "")["planning"])

    def test_a_profile_may_list_mirrored_docs_and_defaults_to_none(self):
        mirrors = [{"source": "docs/guide.md", "copy": "~/docs/guide.md", "from": "## Contents"}]
        self.assertEqual(ht.resolve_profile({"name": "x", "mirrors": mirrors}, "")["mirrors"], mirrors)
        self.assertEqual(ht.resolve_profile(None, "")["mirrors"], [])


class FakeForge:
    def __init__(self, views):
        self.views, self.merged, self.rebased = list(views), None, 0

    def view(self, iid):
        item = self.views.pop(0) if len(self.views) > 1 else self.views[0]
        if isinstance(item, Exception):
            raise item
        return item, False

    def rebase(self, iid):
        self.rebased += 1

    def merge(self, iid, sha, branch):
        self.merged, self.branch = sha, branch
        return {"state": "merged", "commit": "c0ffee", "url": "u"}


class LandLoop(unittest.TestCase):
    def run_loop(self, forge, **kw):
        return ht.land_loop(forge, 7, timeout_seconds=60, poll_seconds=0, sleep=lambda s: None, **kw)

    def test_a_transient_forge_error_while_waiting_is_retried(self):
        forge = FakeForge([ht.HerdrError("TLS handshake timeout"), mr(head_pipeline={"status": "running", "sha": "abc"}),
                           ht.HerdrError("timeout"), mr()])
        self.assertEqual(self.run_loop(forge)["state"], "merged")
        self.assertEqual(forge.merged, "abc")

    def test_persistent_errors_give_up_with_the_last_error(self):
        forge = FakeForge([ht.HerdrError("TLS handshake timeout")])
        with self.assertRaisesRegex(ht.HerdrError, "TLS handshake timeout"):
            self.run_loop(forge, max_errors=3)

    def test_rebases_once_per_head_then_merges(self):
        forge = FakeForge([mr(detailed_merge_status="need_rebase"), mr(detailed_merge_status="need_rebase"), mr(sha="def", head_pipeline={"status": "success", "sha": "def"})])
        result = self.run_loop(forge)
        self.assertEqual((forge.rebased, forge.merged, result["rebased"]), (1, "def", True))

    def test_merges_with_the_branch_it_read_before_the_merge(self):
        forge = FakeForge([mr(source_branch="feat/x")])
        self.run_loop(forge)
        self.assertEqual(forge.branch, "feat/x")


class HeadLanded(unittest.TestCase):
    """land rebases before merging, so the merged commit is rarely the worktree's HEAD: the same patch counts too."""

    def test_says_how_head_matched_the_merge(self):
        self.assertEqual(ht.head_landed_how("abc", "abc", False, None, None), "same commit")
        self.assertEqual(ht.head_landed_how("abc", "def", True, None, None), "on the default branch")
        self.assertEqual(ht.head_landed_how("abc", "def", False, "p1", "p1"), "same patch")

    def test_none_when_nothing_matches(self):
        self.assertIsNone(ht.head_landed_how("abc", "def", False, "p1", "p2"))
        self.assertIsNone(ht.head_landed_how("abc", None, False, None, None))
        self.assertIsNone(ht.head_landed_how("abc", "def", False, None, None))

    def test_the_head_check_reports_how_it_matched(self):
        check = checks_by_name(ht.retire_checks("idle", "", "merged", "same patch", ht.parse_status_report(REPORT)))["head"]
        self.assertTrue(check["ok"])
        self.assertIn("same patch", check["detail"])

    def test_the_merged_commit_comes_from_either_forge(self):
        self.assertEqual(ht.merged_commit({"squash_commit_sha": "sq", "merge_commit_sha": None}), "sq")
        self.assertEqual(ht.merged_commit({"squash_commit_sha": None, "merge_commit_sha": "mc"}), "mc")
        self.assertEqual(ht.merged_commit(ht.github_pr_to_mr(pr(state="MERGED", mergeCommit={"oid": "gh"}))), "gh")
        self.assertIsNone(ht.merged_commit({}))


def git_repo(test):
    """A temporary repo with an `origin/main` ref, deleted after the test."""
    path = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    def run(*args):
        code, out = ht.git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args)
        test.assertEqual(code, 0, (args, out))
        return out
    run("init", "-q", "-b", "main")
    return path, run


def write(path, name, text):
    with open(os.path.join(path, name), "w") as handle:
        handle.write(text)


class PatchIds(unittest.TestCase):
    def test_a_rebased_squash_has_the_same_patch_as_the_branch(self):
        path, run = git_repo(self)
        write(path, "a.txt", "one\ntwo\nthree\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("checkout", "-qb", "feature")
        write(path, "a.txt", "one\ntwo\nthree\nfour\n")
        run("commit", "-qam", "add four")
        write(path, "b.txt", "new\n")
        run("add", "-A")
        run("commit", "-qm", "add b")
        run("checkout", "-q", "main")
        write(path, "c.txt", "someone else's work\n")
        run("add", "-A")
        run("commit", "-qm", "other merge")
        run("merge", "--squash", "-q", "feature")
        run("commit", "-qm", "feat: squashed")
        squash = run("rev-parse", "HEAD")
        run("update-ref", "refs/remotes/origin/main", "HEAD~1")  # the default branch before the squash landed
        run("checkout", "-q", "feature")
        branch = ht.branch_patch_id(path, "origin/main")
        self.assertTrue(branch)
        self.assertEqual(branch, ht.commit_patch_id(path, squash))
        self.assertNotEqual(branch, ht.commit_patch_id(path, "main~1"))

    def test_a_merge_commit_is_compared_against_its_first_parent(self):
        path, run = git_repo(self)
        write(path, "a.txt", "one\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("checkout", "-qb", "feature")
        write(path, "a.txt", "one\ntwo\n")
        run("commit", "-qam", "two")
        run("checkout", "-q", "main")
        write(path, "z.txt", "z\n")
        run("add", "-A")
        run("commit", "-qm", "other")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        run("merge", "--no-ff", "-q", "-m", "merge feature", "feature")
        merge = run("rev-parse", "HEAD")
        run("checkout", "-q", "feature")
        self.assertEqual(ht.branch_patch_id(path, "origin/main"), ht.commit_patch_id(path, merge))

    def test_an_unknown_commit_has_no_patch_id(self):
        path, _ = git_repo(self)
        self.assertIsNone(ht.commit_patch_id(path, "0" * 40))


if __name__ == "__main__":
    unittest.main()


# ---- a repository that runs its own entry and merge process ------------------------------------------------------

REPO_PROCESS = {
    "name": "acme-platform",
    "forge": "github",
    "spawn": {"command": "./scripts/start-session {key} --no-focus", "agent": "session-{key}", "brief": False},
    "retire": {"land": "repo", "branch_cleanup": "repo"},
    "worktree": {"remove": "./scripts/finish-branch --branch {branch}"},
}


class RepoProcessProfile(unittest.TestCase):
    def test_without_the_new_fields_the_helper_runs_the_default_flow(self):
        resolved = ht.resolve_profile(None, "git@github.com:acme/acme-app.git")
        self.assertEqual(resolved["spawn"], {"command": None, "agent": None, "brief": True})
        self.assertEqual(resolved["retire"], {"land": "helper", "branch_cleanup": "helper"})
        self.assertIn("spawn", resolved["defaulted"])
        self.assertIn("retire", resolved["defaulted"])

    def test_a_repo_can_hand_spawn_and_landing_to_its_own_process(self):
        resolved = ht.resolve_profile(REPO_PROCESS, "")
        self.assertEqual(resolved["spawn"]["command"], "./scripts/start-session {key} --no-focus")
        self.assertFalse(resolved["spawn"]["brief"])
        self.assertEqual(resolved["retire"], {"land": "repo", "branch_cleanup": "repo"})

    def test_a_partial_retire_field_keeps_the_other_default(self):
        resolved = ht.resolve_profile({"retire": {"land": "repo"}}, "")
        self.assertEqual(resolved["retire"], {"land": "repo", "branch_cleanup": "helper"})

    def test_refuses_an_unknown_mode_or_subkey_by_name(self):
        for bad, needle in (({"retire": {"land": "manual"}}, "retire.land"),
                            ({"retire": {"branch_cleanup": "never"}}, "retire.branch_cleanup"),
                            ({"retire": {"lands": "repo"}}, "lands"),
                            ({"spawn": {"comand": "x"}}, "comand"),
                            ({"spawn": {"command": "x", "agent": "a", "brief": "no"}}, "spawn.brief")):
            with self.assertRaisesRegex(ht.HerdrError, re.escape(needle)):
                ht.resolve_profile(bad, "")

    def test_a_launcher_needs_the_name_of_the_agent_it_starts_and_vice_versa(self):
        with self.assertRaisesRegex(ht.HerdrError, "spawn.agent"):
            ht.resolve_profile({"spawn": {"command": "./start {key}"}}, "")
        with self.assertRaisesRegex(ht.HerdrError, "spawn.command"):
            ht.resolve_profile({"spawn": {"agent": "session-{key}"}}, "")


class FillCommand(unittest.TestCase):
    def test_fills_each_placeholder_inside_its_own_argument(self):
        argv = ht.fill_command("./start {key} --branch {branch} --no-focus", {"key": "42", "branch": "feat/x y"})
        self.assertEqual(argv, ["./start", "42", "--branch", "feat/x y", "--no-focus"])

    def test_a_value_is_never_split_or_read_as_shell(self):
        argv = ht.fill_command("./start {key}", {"key": "1; rm -rf ~"})
        self.assertEqual(argv, ["./start", "1; rm -rf ~"])

    def test_refuses_a_placeholder_it_has_no_value_for(self):
        with self.assertRaisesRegex(ht.HerdrError, r"\{branch\}"):
            ht.fill_command("./start {key} {branch}", {"key": "42"})


class FakeLauncherHerdr:
    """Stands in for herdr while a repo's launcher starts an agent: agent list is empty for the first reads."""

    def __init__(self, empty_lists=1, screen="> ", name="session-42"):
        self.empty_lists, self.screen, self.name, self.calls = empty_lists, screen, name, []

    def __call__(self, *args, parse=True):
        self.calls.append(args)
        if args[:2] == ("agent", "list"):
            if self.empty_lists > 0:
                self.empty_lists -= 1
                return {"result": {"agents": []}}
            return {"result": {"agents": [{"name": self.name, "agent": "claude", "agent_status": "idle",
                                           "workspace_id": "w9", "pane_id": "w9:p1"}]}}
        if args[:2] == ("agent", "read"):
            return self.screen
        raise AssertionError("unexpected herdr call: %r" % (args,))


class RepoLaunch(AgentHerdrTest):
    def setUp(self):
        super().setUp()
        self.ran = []

    def runner(self, returncode=0, stdout="placed session-42\n", stderr=""):
        def run(argv, **kwargs):
            self.ran.append((argv, kwargs.get("cwd")))
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        return run

    def launch(self, runner=None, timeout=30, **values):
        profile = ht.resolve_profile(REPO_PROCESS, "")
        return ht.repo_launch(REPO, profile, dict({"key": "42"}, **values), runner=runner or self.runner(),
                              sleep=lambda s: None, timeout_seconds=timeout)

    def test_runs_the_repos_launcher_from_the_main_checkout_and_waits_for_its_agent(self):
        self.use(FakeLauncherHerdr(empty_lists=2))
        summary = self.launch()
        self.assertEqual(self.ran, [(["./scripts/start-session", "42", "--no-focus"], REPO)])
        self.assertEqual((summary["agent"], summary["workspace_id"], summary["pane_id"]), ("session-42", "w9", "w9:p1"))
        self.assertEqual((summary["brief"], summary["trust_prompt"]), (False, False))
        self.assertIn("placed session-42", summary["output"])

    def test_never_creates_a_worktree_or_starts_the_agent_itself(self):
        fake = self.use(FakeLauncherHerdr())
        self.launch()
        self.assertFalse([c for c in fake.calls if c[:2] in (("worktree", "open"), ("agent", "start"),
                                                             ("agent", "prompt"))])

    def test_a_launcher_refusal_is_shown_as_it_said_and_nothing_else_runs(self):
        fake = self.use(FakeLauncherHerdr())
        with self.assertRaisesRegex(ht.HerdrError, "refused: workspace already open"):
            self.launch(runner=self.runner(returncode=3, stdout="", stderr="refused: workspace already open"))
        self.assertEqual(fake.calls, [])

    def test_gives_up_naming_the_agent_when_it_never_appears(self):
        self.use(FakeLauncherHerdr(empty_lists=10 ** 6))
        with self.assertRaisesRegex(ht.HerdrError, "session-42"):
            self.launch(timeout=3)

    def test_reports_an_agent_waiting_at_the_folder_trust_prompt(self):
        self.use(FakeLauncherHerdr(screen=TRUST_SCREEN))
        self.assertTrue(self.launch()["trust_prompt"])

    def test_a_repo_without_a_launcher_is_told_to_use_launch(self):
        self.use(FakeLauncherHerdr())
        with self.assertRaisesRegex(ht.HerdrError, "spawn.command"):
            ht.repo_launch(REPO, ht.resolve_profile(None, ""), {"key": "42"}, runner=self.runner())


class LandRefusedWhenTheRepoMerges(unittest.TestCase):
    """retire.land: repo means the repository's own process merges; the helper never does, by any route."""

    def setUp(self):
        saved = ht.agent_profile, ht.cwd_profile, ht.land_loop, ht.retire_info
        def restore():
            ht.agent_profile, ht.cwd_profile, ht.land_loop, ht.retire_info = saved
        self.addCleanup(restore)
        repo = dict(ht.resolve_profile(REPO_PROCESS, ""), main_checkout=REPO, sources={"repo": REPO + "/x.json"})
        ht.agent_profile = lambda target: repo
        ht.cwd_profile = lambda: repo
        self.looped = []
        ht.land_loop = lambda *args, **kwargs: self.looped.append(args) or {"state": "merged"}

        def no_retire_info(target):
            raise AssertionError("retire_info must not run once landing is refused")
        ht.retire_info = no_retire_info

    def test_land_refuses_an_agent_in_such_a_repo(self):
        with self.assertRaisesRegex(ht.HerdrError, "own process"):
            ht.land("session-42")
        self.assertEqual(self.looped, [])

    def test_land_refuses_an_mr_named_from_inside_such_a_repo(self):
        with self.assertRaisesRegex(ht.HerdrError, "own process"):
            ht.land(None, mr="o/r!5")
        self.assertEqual(self.looped, [])

    def test_watch_refuses_before_it_starts_watching(self):
        with self.assertRaisesRegex(ht.HerdrError, "session-42.*own process"):
            ht.watch(["session-42"], max_hours=0.001, poll_seconds=0)

    def test_landing_several_refuses_them_all_before_landing_any(self):
        with self.assertRaisesRegex(ht.HerdrError, "own process"):
            ht.land_several(["session-42", "session-43"])
        self.assertEqual(self.looped, [])

    def test_a_default_profile_still_lands(self):
        default = dict(ht.resolve_profile(None, ""), main_checkout=REPO, sources={})
        ht.cwd_profile = lambda: default
        ht.land(None, mr="o/r!5")
        self.assertEqual(len(self.looped), 1)

    def test_an_mr_named_from_outside_any_repo_still_lands(self):
        ht.cwd_profile = lambda: None
        ht.land(None, mr="o/r!5")
        self.assertEqual(len(self.looped), 1)


class RetireChecksWhenTheRepoMerges(unittest.TestCase):
    def test_an_open_mr_waits_on_the_repos_own_process_and_approval_is_its_business(self):
        checks = checks_by_name(ht.retire_checks("idle", "", "opened", False, None, land_mode="repo",
                                                 report_expected=False, approval={"approved": False}))
        self.assertFalse(checks["merged"]["ok"])
        self.assertIn("own process", checks["merged"]["detail"])
        self.assertTrue(checks["approved"]["ok"])
        self.assertIn("own process", checks["approved"]["detail"])

    def test_once_merged_an_agent_without_a_brief_needs_no_status_report(self):
        checks = ht.retire_checks("idle", "", "merged", True, None, land_mode="repo", report_expected=False)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertIn("spawn.brief", checks_by_name(checks)["report"]["detail"])

    def test_a_report_the_agent_wrote_anyway_still_gates(self):
        report = ht.parse_status_report(REPORT.replace("safe_to_retire: yes", "safe_to_retire: no"))
        checks = checks_by_name(ht.retire_checks("idle", "", "merged", True, report, report_expected=False))
        self.assertFalse(checks["safe_to_retire"]["ok"])

    def test_defaults_are_todays_checks(self):
        self.assertEqual(ht.retire_checks("idle", "", "opened", True, None),
                         ht.retire_checks("idle", "", "opened", True, None, land_mode="helper", report_expected=True))


class RepoProcessExample(unittest.TestCase):
    def test_the_repo_process_example_hands_spawn_and_landing_to_the_repo(self):
        with open(os.path.join(EXAMPLES, "repo-process", ".claude", "herdr-profile.json")) as handle:
            resolved = ht.resolve_profile(json.load(handle), "")
        self.assertIn("{key}", resolved["spawn"]["command"])
        self.assertEqual(resolved["retire"]["land"], "repo")
        self.assertIn("{branch}", resolved["worktree"]["remove"])


class CwdProfileFailsClosed(unittest.TestCase):
    """Outside a git repo there is no profile to honour; inside one, a profile that can't be read must stop land --mr."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
        saved_cwd, saved_loop = os.getcwd(), ht.land_loop
        self.looped = []
        ht.land_loop = lambda *args, **kwargs: self.looped.append(args) or {"state": "merged"}

        def restore():
            os.chdir(saved_cwd)
            ht.land_loop = saved_loop
        self.addCleanup(restore)
        os.chdir(self.repo)

    def write(self, text):
        os.makedirs(os.path.join(self.repo, ".claude"), exist_ok=True)
        with open(os.path.join(self.repo, ".claude", "herdr-profile.json"), "w") as handle:
            handle.write(text)

    def test_a_malformed_or_mistyped_profile_stops_land_mr_before_any_forge_call(self):
        for text in ('{"retire":{"land":"Repo"}}', '{"retire":{"land":"repo"},}',
                     '{"retire":{"land":"repo","branch_clean":"repo"}}'):
            self.write(text)
            with self.assertRaises(ht.HerdrError, msg=text):
                ht.land(None, mr="o/r!5")
        self.assertEqual(self.looped, [])

    def test_a_valid_repo_mode_profile_refuses_and_a_default_one_lands(self):
        self.write('{"retire":{"land":"repo"}}')
        with self.assertRaisesRegex(ht.HerdrError, "own process"):
            ht.land(None, mr="o/r!5")
        self.write('{"name":"r"}')
        ht.land(None, mr="o/r!5")
        self.assertEqual(len(self.looped), 1)

    def test_outside_any_git_repo_there_is_no_profile(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        self.addCleanup(os.chdir, os.getcwd())  # runs first: Windows can't remove the folder it is standing in
        os.chdir(outside)
        self.assertIsNone(ht.cwd_profile())


class OverlayCannotLoosenTheRepo(unittest.TestCase):
    def setUp(self):
        self.main, self.overlays = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.main)
        self.addCleanup(shutil.rmtree, self.overlays)
        os.makedirs(os.path.join(self.main, ".claude"))
        with open(os.path.join(self.main, ".claude", "herdr-profile.json"), "w") as handle:
            json.dump({"retire": {"land": "repo", "branch_cleanup": "repo"}}, handle)

    def overlay(self, data):
        with open(os.path.join(self.overlays, "mine.json"), "w") as handle:
            json.dump(dict({"match": {"path": self.main}}, **data), handle)

    def test_an_overlay_may_not_hand_merging_or_branch_cleanup_back_to_the_helper(self):
        for retire in ({"land": "helper"}, {"branch_cleanup": "helper"}):
            self.overlay({"retire": retire})
            with self.assertRaisesRegex(ht.HerdrError, "overlay"):
                ht.load_profile(self.main, "", self.overlays)

    def test_a_retire_that_is_not_an_object_is_a_clean_refusal(self):
        self.overlay({"retire": "helper"})
        with self.assertRaisesRegex(ht.HerdrError, "retire must be an object"):
            ht.load_profile(self.main, "", self.overlays)

    def test_an_overlay_may_still_add_personal_fields(self):
        self.overlay({"worktree": {"remove": "./finish --lane mine {branch}"}})
        profile, _ = ht.load_profile(self.main, "", self.overlays)
        self.assertEqual(profile["retire"]["land"], "repo")
        self.assertIn("--lane mine", profile["worktree"]["remove"])


class RepoLaunchRobustness(AgentHerdrTest):
    def launch(self, key="42", runner=None):
        return ht.repo_launch(REPO, ht.resolve_profile(REPO_PROCESS, ""), {"key": key},
                              runner=runner or (lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")),
                              sleep=lambda s: None, timeout_seconds=1)

    def test_a_key_that_would_read_as_an_option_is_refused(self):
        self.use(FakeLauncherHerdr())
        for key in ("-rf", "--relaunch", "", "a b"):
            with self.assertRaisesRegex(ht.HerdrError, "key"):
                self.launch(key=key)

    def test_a_launcher_that_cannot_start_is_a_clean_refusal(self):
        self.use(FakeLauncherHerdr())
        def missing(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        with self.assertRaisesRegex(ht.HerdrError, "Could not run"):
            self.launch(runner=missing)

    def test_a_launcher_that_hangs_is_stopped_and_reported(self):
        self.use(FakeLauncherHerdr())
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        with self.assertRaisesRegex(ht.HerdrError, "did not finish"):
            self.launch(runner=hang)


class WorkToolsPath(unittest.TestCase):
    """retire-info names the installed work plugin's launcher, so retire reaches it from herdr's own plugin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plugins = os.path.join(self.tmp, "plugins")
        os.makedirs(self.plugins)

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def install(self, marketplace_source, install_path):
        self.write_json(os.path.join(self.plugins, "installed_plugins.json"),
                        {"version": 2, "plugins": {"work@work-skills": [{"scope": "user", "installPath": install_path}]}})
        self.write_json(os.path.join(self.plugins, "known_marketplaces.json"),
                        {"work-skills": {"source": marketplace_source}})

    def test_work_tools_from_a_directory_marketplace(self):
        clone = os.path.join(self.tmp, "work-skills")
        self.write_json(os.path.join(clone, ".claude-plugin", "marketplace.json"),
                        {"name": "work-skills", "plugins": [{"name": "work", "source": "./work"}]})
        self.touch(os.path.join(clone, "work", "scripts", "work-tools"))
        self.install({"source": "directory", "path": clone}, os.path.join(self.tmp, "cache", "work"))
        self.assertEqual(ht.work_tools_path(self.plugins), os.path.join(clone, "work", "scripts", "work-tools"))

    def test_work_tools_from_an_installed_copy(self):
        cache = os.path.join(self.tmp, "cache", "work-skills", "work", "abc123")
        self.touch(os.path.join(cache, "scripts", "work-tools"))
        self.install({"source": "github", "repo": "longpshorn/work-skills"}, cache)
        self.assertEqual(ht.work_tools_path(self.plugins), os.path.join(cache, "scripts", "work-tools"))

    def test_work_tools_none_when_not_installed(self):
        self.write_json(os.path.join(self.plugins, "installed_plugins.json"), {"version": 2, "plugins": {}})
        self.assertIsNone(ht.work_tools_path(self.plugins))
        self.assertIsNone(ht.work_tools_path(os.path.join(self.tmp, "nowhere")))

    def test_work_tools_none_when_file_missing(self):
        self.install({"source": "github", "repo": "longpshorn/work-skills"}, os.path.join(self.tmp, "cache", "gone"))
        self.assertIsNone(ht.work_tools_path(self.plugins))

    def test_work_tools_skips_a_work_plugin_without_a_launcher(self):
        cache = os.path.join(self.tmp, "cache", "work-skills", "work", "abc123")
        self.touch(os.path.join(cache, "scripts", "work-tools"))
        self.write_json(os.path.join(self.plugins, "installed_plugins.json"), {"version": 2, "plugins": {
            "work@work-store": [{"scope": "user", "installPath": os.path.join(self.tmp, "old")}],
            "work@work-skills": [{"scope": "user", "installPath": cache}]}})
        self.write_json(os.path.join(self.plugins, "known_marketplaces.json"),
                        {"work-skills": {"source": {"source": "github", "repo": "longpshorn/work-skills"}}})
        self.assertEqual(ht.work_tools_path(self.plugins), os.path.join(cache, "scripts", "work-tools"))

    def test_work_tools_skips_a_disabled_work_plugin(self):
        cache = os.path.join(self.tmp, "cache", "work")
        self.touch(os.path.join(cache, "scripts", "work-tools"))
        self.install({"source": "github", "repo": "longpshorn/work-skills"}, cache)
        self.write_json(os.path.join(self.tmp, "settings.json"), {"enabledPlugins": {"work@work-skills": False}})
        self.assertIsNone(ht.work_tools_path(self.plugins))

    def test_work_tools_none_on_unexpected_file_shapes(self):
        shapes = [
            ({"version": 1, "plugins": {"work@work-skills": {"installPath": "/x"}}}, {}),
            ({"plugins": {"work@work-skills": [{"installPath": "/x"}]}}, {"work-skills": "not an object"}),
            ({"plugins": {"work@work-skills": [{"installPath": "/x"}]}}, {"work-skills": {"source": ["list"]}}),
            ({"plugins": ["not", "a", "dict"]}, {}),
            ([1, 2], []),
        ]
        for installed, known in shapes:
            self.write_json(os.path.join(self.plugins, "installed_plugins.json"), installed)
            self.write_json(os.path.join(self.plugins, "known_marketplaces.json"), known)
            self.assertIsNone(ht.work_tools_path(self.plugins), (installed, known))


    def test_the_default_reads_the_suites_empty_home_not_the_real_one(self):
        self.assertEqual(os.path.expanduser("~"), SUITE_HOME)
        self.assertIsNone(ht.work_tools_path())


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKILL_TEXT = """---
name: spawn
description: Start an agent. Use for "/herdr:spawn <ticket>".
---

# Spawn — one ticket

Helper: `sh "${CLAUDE_PLUGIN_ROOT}/scripts/herdr-tools"`. See [PROFILES.md](${CLAUDE_PLUGIN_ROOT}/PROFILES.md).
"""


class RenderSkill(unittest.TestCase):
    """Other agents don't set ${CLAUDE_PLUGIN_ROOT}: install-skills writes the plugin's absolute root in its place."""

    def render(self, root="/home/u/herdr-skills/herdr"):
        return ht.render_skill(SKILL_TEXT, "herdr", "spawn", root)

    def test_replaces_the_plugin_root(self):
        text = self.render()
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)
        self.assertIn('`sh "/home/u/herdr-skills/herdr/scripts/herdr-tools"`', text)
        self.assertIn("(/home/u/herdr-skills/herdr/PROFILES.md)", text)

    def test_a_windows_root_uses_forward_slashes(self):
        self.assertIn('sh "C:/Users/u/herdr-skills/herdr/scripts/herdr-tools"',
                      self.render("C:\\Users\\u\\herdr-skills\\herdr"))

    def test_prefixes_the_name_with_the_plugin(self):
        text = self.render()
        self.assertIn("\nname: herdr-spawn\n", text)
        self.assertNotIn("\nname: spawn\n", text)

    def test_says_how_the_claude_names_map_and_how_to_update(self):
        text = self.render()
        note = text.split("# Spawn — one ticket\n", 1)[1].lstrip("\n").split("\n", 1)[0]
        self.assertTrue(note.startswith("> "), note)
        self.assertIn("`/herdr:<name>`", note)
        self.assertIn("`herdr-<name>`", note)
        self.assertIn('sh "/home/u/herdr-skills/herdr/scripts/herdr-tools" install-skills --update', note)

    def test_a_reference_file_only_gets_the_root(self):
        text = ht.render_file("Read `${CLAUDE_PLUGIN_ROOT}/skills/spawn/references/brief-template.md`.", "/r/herdr")
        self.assertEqual(text, "Read `/r/herdr/skills/spawn/references/brief-template.md`.")


class SkillDirs(unittest.TestCase):
    """One table names each tool's user-scope skills folder."""

    def test_every_tool_has_a_folder_and_agents_is_the_default(self):
        dirs = ht.skill_dirs(env={})
        home = os.path.expanduser("~")
        self.assertEqual(dirs["agents"], os.path.join(home, ".agents", "skills"))
        self.assertEqual(dirs["codex"], dirs["agents"])
        self.assertEqual(dirs["gemini"], os.path.join(home, ".gemini", "skills"))
        self.assertEqual(dirs["opencode"], os.path.join(home, ".config", "opencode", "skills"))
        self.assertEqual(dirs["omp"], os.path.join(home, ".omp", "agent", "skills"))
        self.assertEqual(ht.DEFAULT_SKILLS_TOOL, "agents")

    def test_opencode_honours_xdg_config_home(self):
        self.assertEqual(ht.skill_dirs(env={"XDG_CONFIG_HOME": "/x/cfg"})["opencode"],
                         os.path.join("/x/cfg", "opencode", "skills"))


class InstallSkills(unittest.TestCase):
    """install-skills writes rendered copies it can find and replace again, and never touches a folder it didn't write."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dest = os.path.join(self.tmp, "skills")

    def install(self, **options):
        return ht.install_skills(PLUGIN_ROOT, self.dest, tool="agents", commit="abc123", **options)

    def read(self, *parts):
        with open(os.path.join(self.dest, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_writes_every_skill_rendered_with_its_references_and_a_marker(self):
        result = self.install()
        skills = sorted(os.listdir(os.path.join(PLUGIN_ROOT, "skills")))
        self.assertEqual(sorted(result["installed"]), ["herdr-" + s for s in skills])
        self.assertEqual(sorted(os.listdir(self.dest)), ["herdr-" + s for s in skills])
        spawn = self.read("herdr-spawn", "SKILL.md")
        self.assertIn("name: herdr-spawn", spawn)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", spawn)
        self.assertIn(PLUGIN_ROOT.replace("\\", "/") + "/scripts/herdr-tools", spawn)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", self.read("herdr-spawn", "references", "brief-template.md"))
        marker = json.loads(self.read("herdr-spawn", ht.SKILLS_MARKER))
        self.assertEqual(marker, {"plugin": "herdr", "skill": "spawn", "root": PLUGIN_ROOT, "tool": "agents",
                                  "commit": "abc123"})

    def test_a_dry_run_writes_nothing(self):
        result = self.install(dry_run=True)
        self.assertTrue(result["installed"])
        self.assertFalse(os.path.exists(self.dest))

    def test_installing_again_replaces_its_own_copies(self):
        self.install()
        with open(os.path.join(self.dest, "herdr-spawn", "stale.md"), "w") as handle:
            handle.write("old")
        self.install()
        self.assertFalse(os.path.exists(os.path.join(self.dest, "herdr-spawn", "stale.md")))

    def test_refuses_a_folder_it_did_not_write_and_writes_nothing(self):
        os.makedirs(os.path.join(self.dest, "herdr-status"))
        with open(os.path.join(self.dest, "herdr-status", "SKILL.md"), "w") as handle:
            handle.write("mine")
        with self.assertRaises(ht.HerdrError) as caught:
            self.install()
        self.assertIn("herdr-status", str(caught.exception))
        self.assertEqual(os.listdir(self.dest), ["herdr-status"])
        self.assertEqual(self.read("herdr-status", "SKILL.md"), "mine")

    def test_removes_its_copy_of_a_skill_the_plugin_no_longer_has(self):
        self.install()
        gone = os.path.join(self.dest, "herdr-gone")
        os.makedirs(gone)
        with open(os.path.join(gone, ht.SKILLS_MARKER), "w") as handle:
            json.dump({"plugin": "herdr", "skill": "gone"}, handle)
        result = self.install()
        self.assertEqual(result["removed"], ["herdr-gone"])
        self.assertFalse(os.path.exists(gone))

    def test_leaves_other_plugins_skills_alone(self):
        other = os.path.join(self.dest, "work-checkpoint")
        os.makedirs(other)
        with open(os.path.join(other, ht.SKILLS_MARKER), "w") as handle:
            json.dump({"plugin": "work", "skill": "checkpoint"}, handle)
        self.install()
        ht.uninstall_skills(self.dest)
        self.assertEqual(os.listdir(self.dest), ["work-checkpoint"])

    def test_uninstall_removes_only_its_own_copies(self):
        self.install()
        os.makedirs(os.path.join(self.dest, "herdr-mine"))
        result = ht.uninstall_skills(self.dest)
        self.assertIn("herdr-spawn", result["removed"])
        self.assertEqual(os.listdir(self.dest), ["herdr-mine"])

    def test_uninstall_dry_run_and_a_missing_folder_remove_nothing(self):
        self.install()
        self.assertTrue(ht.uninstall_skills(self.dest, dry_run=True)["removed"])
        self.assertIn("herdr-spawn", os.listdir(self.dest))
        self.assertEqual(ht.uninstall_skills(os.path.join(self.tmp, "nowhere"))["removed"], [])

    def test_gemini_gets_a_note_about_agents_md_and_nothing_is_written_for_it(self):
        result = ht.install_skills(PLUGIN_ROOT, self.dest, tool="gemini", commit="abc123")
        self.assertTrue(any("AGENTS.md" in note and "settings.json" in note for note in result["notes"]))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["skills"])


class InstalledSkillDirs(unittest.TestCase):
    """--update finds every folder it installed into from the markers, and re-renders each for its tool."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_finds_the_folders_holding_a_plugins_copies_once_each(self):
        a, b, c = (os.path.join(self.tmp, n) for n in ("a", "b", "c"))
        ht.install_skills(PLUGIN_ROOT, a, tool="agents", commit="x")
        ht.install_skills(PLUGIN_ROOT, b, tool="omp", commit="x")
        os.makedirs(c)
        dirs = {"agents": a, "codex": a, "omp": b, "gemini": c, "opencode": os.path.join(self.tmp, "none")}
        self.assertEqual(ht.installed_skill_dirs("herdr", dirs), [(a, "agents"), (b, "omp")])
        self.assertEqual(ht.installed_skill_dirs("work", dirs), [])

    def test_refresh_re_renders_each_installed_folder(self):
        a = os.path.join(self.tmp, "a")
        ht.install_skills(PLUGIN_ROOT, a, tool="agents", commit="old")
        result = ht.refresh_skills(PLUGIN_ROOT, {"agents": a}, commit="new")
        self.assertEqual([r["dest"] for r in result], [a])
        with open(os.path.join(a, "herdr-spawn", ht.SKILLS_MARKER)) as handle:
            self.assertEqual(json.load(handle)["commit"], "new")


class InstallSkillsCommand(unittest.TestCase):
    """The command runs outside Herdr, installs from a git clone only, and --update pulls before re-rendering."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dest = os.path.join(self.tmp, "skills")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        saved = os.environ.pop("HERDR_ENV", None)
        self.addCleanup(lambda: saved is not None and os.environ.__setitem__("HERDR_ENV", saved))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ht.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_installs_into_dest_outside_herdr(self):
        code, out, err = self.run_main("install-skills", "--dest", self.dest)
        self.assertEqual(code, 0, err)
        self.assertIn("herdr-spawn", os.listdir(self.dest))
        self.assertEqual(json.loads(out)["dest"], self.dest)

    def test_refuses_a_root_outside_a_git_checkout(self):
        copy = os.path.join(self.tmp, "cache", "herdr")
        shutil.copytree(os.path.join(PLUGIN_ROOT, "skills"), os.path.join(copy, "skills"))
        with self.assertRaises(ht.HerdrError) as caught:
            ht.plugin_commit(copy)
        self.assertIn("clone", str(caught.exception))

    def test_update_refuses_a_clone_with_local_changes(self):
        clone = os.path.join(self.tmp, "clone")
        subprocess.run(["git", "init", "-q", clone], check=True)
        with open(os.path.join(clone, "dirty.txt"), "w") as handle:
            handle.write("x")
        with self.assertRaises(ht.HerdrError) as caught:
            ht.pull_plugin(clone)
        self.assertIn("local changes", str(caught.exception))


class WorkToolsFromInstalledSkills(unittest.TestCase):
    """Without the Claude Code plugin, retire finds work's launcher through the marker of a work skill install-skills
    wrote (work-skills uses the same marker)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.skills = os.path.join(self.tmp, "skills")
        self.root = os.path.join(self.tmp, "work-skills", "work")
        os.makedirs(os.path.join(self.root, "scripts"))
        open(os.path.join(self.root, "scripts", "work-tools"), "w").close()

    def mark(self, folder, data):
        os.makedirs(os.path.join(self.skills, folder))
        with open(os.path.join(self.skills, folder, ht.SKILLS_MARKER), "w") as handle:
            json.dump(data, handle)

    def test_finds_the_launcher_from_a_work_skills_marker(self):
        self.mark("work-checkpoint", {"plugin": "work", "skill": "checkpoint", "root": self.root})
        self.assertEqual(ht.work_tools_path(os.path.join(self.tmp, "no-plugins"), {"agents": self.skills}),
                         os.path.join(self.root, "scripts", "work-tools"))

    def test_none_when_the_marker_is_another_plugins_or_its_launcher_is_gone(self):
        self.mark("herdr-status", {"plugin": "herdr", "skill": "status", "root": self.root})
        self.mark("work-new", {"plugin": "work", "skill": "new", "root": os.path.join(self.tmp, "gone")})
        self.mark("work-bad", ["not", "a", "dict"])
        self.assertIsNone(ht.work_tools_path(os.path.join(self.tmp, "no-plugins"), {"agents": self.skills}))


class SkillTextIsToolNeutral(unittest.TestCase):
    """A Claude Code-only tool named in skill text always comes with what to do in any other agent."""

    CLAUDE_ONLY = ("run_in_background", "AskUserQuestion", "/permissions")

    def test_claude_code_only_tools_are_named_only_beside_an_alternative(self):
        offenders = []
        for folder, _, files in os.walk(os.path.join(PLUGIN_ROOT, "skills")):
            for name in (n for n in files if n.endswith(".md")):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        for tool in self.CLAUDE_ONLY:
                            if tool in line and not re.search(r"Claude Code: `%s`[^)]*elsewhere" % re.escape(tool),
                                                              line):
                                offenders.append("%s:%d: %s" % (name, number, tool))
        self.assertEqual(offenders, [])

    def test_the_trust_prompt_is_not_called_claudes(self):
        for folder, _, files in os.walk(os.path.join(PLUGIN_ROOT, "skills")):
            for name in (n for n in files if n.endswith(".md")):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    self.assertNotIn("Claude's folder-trust", handle.read(), name)


# ---- deliver and wait -------------------------------------------------------------------------------------------

NOW = datetime.datetime(2026, 11, 6, 23, 15, 7)


def prompt_entry(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_entry(name, **tool_input):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": tool_input}]}}


def result_entry():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


class MessageFiles(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home)

    def test_the_file_is_under_the_agents_message_folder_named_by_time_and_topic(self):
        path = ht.message_path("irt-102-1-dedupe", "Plan verdict!", NOW, home=self.home)
        self.assertEqual(path, os.path.join(self.home, ".cache", "herdr", "messages", "irt-102-1-dedupe",
                                            "20261106-231507-plan-verdict.md"))
        self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_a_second_message_in_the_same_second_never_overwrites_the_first(self):
        first = ht.message_path("a", "x", NOW, home=self.home)
        open(first, "w").close()
        self.assertTrue(ht.message_path("a", "x", NOW, home=self.home).endswith("20261106-231507-x-2.md"))

    def test_a_topic_with_nothing_usable_still_gets_a_name(self):
        self.assertTrue(ht.message_path("a", "!!!", NOW, home=self.home).endswith("-message.md"))

    def test_the_pointer_names_sender_path_and_short_hash(self):
        self.assertEqual(ht.pointer_line("irt-102", "/m/x.md", "0123456789abcdef"),
                         "Message from irt-102: read /m/x.md (sha256 0123456789ab) and act on it.")


class DeliveryState(unittest.TestCase):
    POINTER = "Message from p: read /m/x.md (sha256 0123456789ab) and act on it."

    def state(self, entries):
        return ht.delivery_state(entries, self.POINTER, "/m/x.md")

    def test_nothing_arrived(self):
        self.assertEqual(self.state([prompt_entry("earlier work")]),
                         {"delivered": False, "read": False, "newest_prompt": "earlier work"})

    def test_a_truncated_pointer_is_not_delivered(self):
        self.assertFalse(self.state([prompt_entry(self.POINTER[40:])])["delivered"])

    def test_arrived_but_not_yet_read(self):
        state = self.state([prompt_entry(self.POINTER), result_entry()])
        self.assertEqual((state["delivered"], state["read"]), (True, False))

    def test_read_with_the_read_tool(self):
        self.assertTrue(self.state([prompt_entry(self.POINTER), tool_entry("Read", file_path="/m/x.md")])["read"])

    def test_read_with_a_shell_command(self):
        entries = [prompt_entry(self.POINTER), tool_entry("Bash", command="cat '/m/x.md'")]
        self.assertTrue(self.state(entries)["read"])

    def test_a_read_before_the_pointer_does_not_count(self):
        entries = [tool_entry("Read", file_path="/m/x.md"), prompt_entry(self.POINTER)]
        self.assertFalse(self.state(entries)["read"])

    def test_tool_results_are_not_prompts(self):
        self.assertEqual(self.state([prompt_entry("asked"), result_entry()])["newest_prompt"], "asked")

    def test_a_prompt_made_of_text_blocks_counts(self):
        entry = {"type": "user", "message": {"content": [{"type": "text", "text": self.POINTER}]}}
        self.assertTrue(self.state([entry])["delivered"])


class FindTranscript(unittest.TestCase):
    def test_finds_the_session_in_any_project_folder(self):
        home = tempfile.mkdtemp()
        try:
            folder = os.path.join(home, ".claude", "projects", "-some-mangled-cwd")
            os.makedirs(folder)
            path = os.path.join(folder, "abc-123.jsonl")
            open(path, "w").close()
            self.assertEqual(ht.find_transcript("abc-123", home=home), path)
            self.assertIsNone(ht.find_transcript("missing", home=home))
        finally:
            shutil.rmtree(home)

    def test_reads_entries_and_skips_broken_lines(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.write(json.dumps(prompt_entry("a")) + "\n{not json\n\n" + json.dumps(prompt_entry("b")) + "\n")
        handle.close()
        try:
            self.assertEqual([e["message"]["content"] for e in ht.transcript_entries(handle.name)], ["a", "b"])
        finally:
            os.remove(handle.name)


class FakeDeliverHerdr:
    """One agent with a Claude transcript. on_prompt(fake, text) decides what the transcript records for each send."""

    def __init__(self, transcript, kind="claude", session="sess-1", statuses=("idle",), on_prompt=None, reads=None):
        self.transcript, self.kind, self.session = transcript, kind, session
        self.statuses, self.calls, self.prompts = list(statuses), [], []
        self.on_prompt = on_prompt or (lambda fake, text: fake.record(prompt_entry(text)))
        self.reads = reads  # (path appended to the transcript as a Read after this many `agent get` polls), or None

    def record(self, *entries):
        with open(self.transcript, "a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def __call__(self, *args, parse=True):
        self.calls.append(args)
        if args[:2] == ("agent", "prompt"):
            self.prompts.append(args[3])
            self.on_prompt(self, args[3])
            return {"result": {}}
        if args[:2] == ("agent", "get"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if self.reads is not None:
                self.reads[1] -= 1
                if self.reads[1] == 0:
                    self.record(tool_entry("Read", file_path=self.reads[0]))
            return {"result": {"agent": {"agent_status": status, "agent": self.kind,
                                         "agent_session": {"value": self.session} if self.session else None}}}
        raise AssertionError("unexpected herdr call: %r" % (args,))


class Deliver(AgentHerdrTest):
    def setUp(self):
        super().setUp()
        folder = os.path.join(self.home, ".claude", "projects", "-x")
        os.makedirs(folder)
        self.transcript = os.path.join(folder, "sess-1.jsonl")
        open(self.transcript, "w").close()

    def deliver(self, fake, **kwargs):
        self.use(fake)
        options = dict(text="Approved, with two restrictions.", topic="verdict", sender="parent", now=NOW,
                       timeout_seconds=30, poll_seconds=1, resend_after_seconds=5, sleep=lambda s: None)
        options.update(kwargs)
        return ht.deliver("child", **options)

    def message_path(self):
        return os.path.join(self.home, ".cache", "herdr", "messages", "child", "20261106-231507-verdict.md")

    def test_writes_the_whole_message_to_a_file_and_sends_only_the_pointer(self):
        fake = FakeDeliverHerdr(self.transcript, reads=None)
        fake.reads = [self.message_path(), 2]
        result = self.deliver(fake)
        with open(self.message_path()) as handle:
            self.assertEqual(handle.read(), "Approved, with two restrictions.")
        digest = hashlib.sha256(b"Approved, with two restrictions.").hexdigest()
        self.assertEqual(fake.prompts, ["Message from parent: read %s (sha256 %s) and act on it."
                                        % (self.message_path(), digest[:12])])
        self.assertEqual((result["verified"], result["delivered"], result["read"], result["attempts"]),
                         ("yes", True, True, 1))
        self.assertEqual(result["sha256"], digest)

    def test_a_message_file_is_sent_as_written(self):
        source = os.path.join(self.home, "verdict.md")
        with open(source, "w") as handle:
            handle.write("long verdict " * 200)
        fake = FakeDeliverHerdr(self.transcript)
        fake.reads = [self.message_path(), 1]
        result = self.deliver(fake, text=None, file=source)
        with open(result["path"]) as handle:
            self.assertEqual(handle.read(), "long verdict " * 200)

    def test_a_pointer_that_never_arrived_is_resent_once_to_an_idle_agent(self):
        sent = []
        def lose_the_first(fake, text):
            sent.append(text)
            if len(sent) == 2:
                fake.record(prompt_entry(text))
        fake = FakeDeliverHerdr(self.transcript, on_prompt=lose_the_first)
        fake.reads = [self.message_path(), 9]
        result = self.deliver(fake)
        self.assertEqual(len(fake.prompts), 2)
        self.assertEqual((result["verified"], result["attempts"]), ("yes", 2))

    def test_a_pointer_that_never_arrives_is_reported_after_one_resend(self):
        fake = FakeDeliverHerdr(self.transcript, on_prompt=lambda fake, text: None)
        result = self.deliver(fake)
        self.assertEqual(len(fake.prompts), 2)
        self.assertEqual((result["verified"], result["delivered"]), ("no", False))

    def test_a_working_agent_is_never_sent_a_second_pointer(self):
        fake = FakeDeliverHerdr(self.transcript, statuses=["working"], on_prompt=lambda fake, text: None)
        result = self.deliver(fake)
        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual(result["verified"], "no")

    def test_arrived_but_unread_is_reported_without_a_resend(self):
        fake = FakeDeliverHerdr(self.transcript)
        result = self.deliver(fake)
        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual((result["verified"], result["delivered"], result["read"]), ("no", True, False))
        self.assertIn("not read", result["detail"])

    def test_a_changed_file_fails_the_hash_check(self):
        def tamper(fake, text):
            fake.record(prompt_entry(text), tool_entry("Read", file_path=self.message_path()))
            with open(self.message_path(), "w") as handle:
                handle.write("something else")
        result = self.deliver(FakeDeliverHerdr(self.transcript, on_prompt=tamper))
        self.assertEqual(result["verified"], "no")
        self.assertIn("changed", result["detail"])

    def test_other_agent_kinds_are_unverified(self):
        fake = FakeDeliverHerdr(self.transcript, kind="codex", on_prompt=lambda fake, text: None)
        result = self.deliver(fake)
        self.assertEqual((result["verified"], len(fake.prompts)), ("unverified", 1))

    def test_a_claude_agent_without_a_session_id_is_unverified(self):
        result = self.deliver(FakeDeliverHerdr(self.transcript, session=None, on_prompt=lambda fake, text: None))
        self.assertEqual(result["verified"], "unverified")

    def test_needs_exactly_one_of_text_or_file(self):
        with self.assertRaisesRegex(ht.HerdrError, "exactly one"):
            self.deliver(FakeDeliverHerdr(self.transcript), text=None)


class FakeWaitHerdr:
    def __init__(self, statuses):
        self.statuses = list(statuses)

    def __call__(self, *args, parse=True):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status == "gone":
            raise ht.HerdrError('{"error":{"code":"agent_not_found","message":"agent target a not found"}}')
        return {"result": {"agent": {} if status is None else {"agent_status": status}}}


class Wait(AgentHerdrTest):
    def wait(self, statuses, until=("idle",), **kwargs):
        self.use(FakeWaitHerdr(statuses))
        options = dict(timeout_seconds=20, poll_seconds=1, gone_seconds=5, sleep=lambda s: None)
        options.update(kwargs)
        return ht.wait_for("a", until, **options)

    def test_returns_the_first_wanted_state(self):
        self.assertEqual(self.wait(["working", "working", "blocked"], until=("idle", "blocked"))["state"], "blocked")

    def test_blank_missing_and_unknown_statuses_mean_keep_waiting(self):
        self.assertEqual(self.wait(["", None, "unknown", "idle"], until=("idle", "unknown"))["state"], "idle")

    def test_a_short_absence_is_waited_through(self):
        self.assertEqual(self.wait(["gone", "gone", "idle"])["state"], "idle")

    def test_an_agent_gone_past_the_grace_period_is_reported_gone(self):
        self.assertEqual(self.wait(["gone"])["state"], "gone")

    def test_times_out_with_the_last_status_seen(self):
        self.assertEqual(self.wait(["working"]), {"agent": "a", "state": "timeout", "last": "working", "waited": 20})

    def test_other_herdr_errors_are_raised(self):
        class Broken:
            def __call__(self, *args, parse=True):
                raise ht.HerdrError("herdr server not running")
        self.use(Broken())
        with self.assertRaisesRegex(ht.HerdrError, "not running"):
            ht.wait_for("a", ("idle",), timeout_seconds=5, poll_seconds=1, sleep=lambda s: None)


class RebaseRequestIsDelivered(unittest.TestCase):
    def test_land_and_watch_send_the_rebase_request_as_a_file(self):
        calls, saved = [], ht.deliver
        ht.deliver = lambda agent, **kwargs: calls.append((agent, kwargs)) or {"verified": "yes"}
        try:
            ht.ask_to_rebase("proj105", ht.REBASE_REQUEST)
        finally:
            ht.deliver = saved
        self.assertEqual(calls, [("proj105", {"text": ht.REBASE_REQUEST, "topic": "rebase request",
                                              "timeout_seconds": 120})])


# ---- Parents and children ----------------------------------------------------------------------------------------


class HomeTest(unittest.TestCase):
    """Points the home directory at a temporary one, so records and reports land there."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.saved_home = point_home_at(self.home)

    def tearDown(self):
        restore_home(self.saved_home)
        shutil.rmtree(self.home)


def child_record(child="irt-102-1-dedupe", parent="irt-102", n=1, **fields):
    record = {"child": child, "parent": parent, "n": n, "key": "PROJ-1", "label": "%d PROJ-1 dedupe" % n,
              "layout": "workspace", "workspace_id": "wP", "tab_id": "wP:t2", "pane_id": "wP:p2",
              "worktree": "/code/repo-PROJ-1", "branch": "fix/PROJ-1-dedupe", "main_checkout": REPO,
              "spawned_at": "2026-11-11T13:00:00", "state": "live", "history": []}
    record.update(fields)
    return record


class ChildRecords(HomeTest):
    def test_a_record_lives_in_the_herdr_agents_cache_named_after_the_child(self):
        self.assertEqual(ht.agent_record_path("irt-102-1-dedupe"),
                         os.path.join(self.home, ".cache", "herdr", "agents", "irt-102-1-dedupe.json"))

    def test_written_records_read_back_and_a_missing_one_is_none(self):
        ht.write_record(child_record())
        self.assertEqual(ht.read_record("irt-102-1-dedupe"), child_record())
        self.assertIsNone(ht.read_record("nobody"))

    def test_an_update_changes_fields_and_appends_what_happened_to_the_history(self):
        ht.write_record(child_record())
        ht.update_record("irt-102-1-dedupe", "pop-out", now="2026-11-11T14:00:00", workspace_id="wX", pane_id="wX:p1")
        record = ht.read_record("irt-102-1-dedupe")
        self.assertEqual((record["workspace_id"], record["pane_id"]), ("wX", "wX:p1"))
        self.assertEqual(record["history"], [{"at": "2026-11-11T14:00:00", "event": "pop-out",
                                              "workspace_id": "wX", "pane_id": "wX:p1"}])

    def test_updating_a_child_without_a_record_says_so(self):
        with self.assertRaisesRegex(ht.HerdrError, "No record for nobody"):
            ht.update_record("nobody", "pop-out")

    def test_lists_every_record_and_skips_unreadable_files(self):
        ht.write_record(child_record("p-2-b", "p", 2))
        ht.write_record(child_record("p-1-a", "p", 1))
        with open(os.path.join(os.path.dirname(ht.agent_record_path("x")), "broken.json"), "w") as handle:
            handle.write("{")
        self.assertEqual([r["child"] for r in ht.list_records()], ["p-1-a", "p-2-b"])

    def test_no_records_folder_means_no_records(self):
        self.assertEqual(ht.list_records(), [])


class ChildNames(unittest.TestCase):
    def test_counts_up_per_parent(self):
        records = [child_record("p-1-a", "p", 1), child_record("p-3-c", "p", 3), child_record("q-7-x", "q", 7)]
        self.assertEqual(ht.next_child_name("p", "Dedupe rows", records), "p-4-dedupe-rows")
        self.assertEqual(ht.next_child_name("r", "x", records), "r-1-x")

    def test_cuts_the_slug_so_the_name_is_a_herdr_agent_name(self):
        name = ht.next_child_name("ipt-core801", "a very long slug that goes on and on", [])
        self.assertEqual(len(name), 32)
        self.assertTrue(ht.AGENT_NAME.match(name))
        self.assertTrue(name.startswith("ipt-core801-1-a-very-long-slug"))
        self.assertFalse(name.endswith("-"))

    def test_refuses_a_parent_that_is_not_an_agent_name(self):
        with self.assertRaisesRegex(ht.HerdrError, "named agent"):
            ht.next_child_name("w8Y:p1", "x", [])

    def test_refuses_a_parent_name_that_leaves_no_room_for_a_slug(self):
        with self.assertRaisesRegex(ht.HerdrError, "too long"):
            ht.next_child_name("p" * 29, "x", [])

    def test_refuses_a_slug_with_nothing_usable(self):
        with self.assertRaisesRegex(ht.HerdrError, "slug"):
            ht.next_child_name("p", "!!!", [])

    def test_the_tab_label_carries_number_key_and_topic(self):
        self.assertEqual(ht.child_label(3, "PROJ-9", "dedupe rows"), "3 PROJ-9 dedupe rows")


def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


class FakeLayoutHerdr:
    """A small in-memory Herdr: workspaces, tabs, panes and named agents, with the creates, moves and closes the
    layout commands use. Ids carry their workspace id, as Herdr's do, so moving a pane to another workspace renumbers
    it, and Herdr removes a tab or workspace a move or close leaves empty (both checked against Herdr 0.9.5)."""

    def __init__(self):
        self.calls, self.workspaces, self.tabs, self.panes, self.agents = [], {}, {}, {}, {}
        self.busy, self.counters, self.next_ws, self.screen = {}, {}, 0, "> "

    # -- building the starting state
    def new_id(self, ws, kind):
        self.counters[(ws, kind)] = self.counters.get((ws, kind), 0) + 1
        return "%s:%s%d" % (ws, kind, self.counters[(ws, kind)])

    def add_workspace(self, label, cwd="/", worktree=None, tab_label="1"):
        self.next_ws += 1
        ws = "w%d" % self.next_ws
        self.workspaces[ws] = {"workspace_id": ws, "label": label}
        if worktree:
            self.workspaces[ws]["worktree"] = worktree
        tab = self.add_tab(ws, tab_label)
        return ws, tab, self.add_pane(tab, cwd)

    def add_tab(self, ws, label):
        tab = self.new_id(ws, "t")
        self.tabs[tab] = {"tab_id": tab, "workspace_id": ws, "label": label}
        return tab

    def add_pane(self, tab, cwd="/"):
        ws = self.tabs[tab]["workspace_id"]
        pane = self.new_id(ws, "p")
        self.panes[pane] = {"pane_id": pane, "tab_id": tab, "workspace_id": ws, "cwd": cwd}
        return pane

    def add_agent(self, name, pane, status="idle", kind="claude"):
        self.agents[name] = {"name": name, "pane_id": pane, "agent_status": status, "agent": kind}

    # -- reading it back
    def agent_rows(self):
        rows = []
        for agent in self.agents.values():
            pane = self.panes[agent["pane_id"]]
            rows.append(dict(agent, tab_id=pane["tab_id"], workspace_id=pane["workspace_id"], cwd=pane["cwd"]))
        return rows

    def agent_named(self, name):
        return next(a for a in self.agent_rows() if a["name"] == name)

    def ws_row(self, ws):
        return dict(self.workspaces[ws], pane_count=len([p for p in self.panes.values() if p["workspace_id"] == ws]),
                    tab_count=len([t for t in self.tabs.values() if t["workspace_id"] == ws]))

    def labels(self):
        return sorted(w["label"] for w in self.workspaces.values())

    def called(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]

    # -- changes
    def remove_pane(self, pane):
        tab = self.panes.pop(pane)["tab_id"]
        for name in [n for n, a in self.agents.items() if a["pane_id"] == pane]:
            del self.agents[name]
        self.busy.pop(pane, None)
        if not any(p["tab_id"] == tab for p in self.panes.values()):
            ws = self.tabs.pop(tab)["workspace_id"]
            if not any(t["workspace_id"] == ws for t in self.tabs.values()):
                del self.workspaces[ws]

    def move_pane(self, pane, tab):
        new = self.add_pane(tab, self.panes[pane]["cwd"])
        for agent in self.agents.values():
            if agent["pane_id"] == pane:
                agent["pane_id"] = new
        if pane in self.busy:
            self.busy[new] = self.busy.pop(pane)
        self.remove_pane(pane)
        return self.panes[new]

    def __call__(self, *args, parse=True):
        self.calls.append(args)
        head = args[:2]
        if head == ("agent", "list"):
            return {"result": {"agents": self.agent_rows()}}
        if head == ("agent", "get"):
            if args[2] not in self.agents:
                raise ht.HerdrError('{"error":{"code":"agent_not_found"}}')
            return {"result": {"agent": self.agent_named(args[2])}}
        if head == ("agent", "start"):
            self.add_agent(args[2], opt(args, "--pane"), "idle", opt(args, "--kind"))
            return {"result": {}}
        if head == ("agent", "read"):
            return self.screen
        if head == ("agent", "prompt"):
            self.agents[args[2]]["agent_status"] = "working"
            return {"result": {}}
        if head == ("workspace", "list"):
            return {"result": {"workspaces": [self.ws_row(ws) for ws in self.workspaces]}}
        if head == ("workspace", "create"):
            ws, tab, pane = self.add_workspace(opt(args, "--label"), opt(args, "--cwd"))
            return {"result": {"workspace": self.ws_row(ws), "tab": self.tabs[tab], "root_pane": self.panes[pane]}}
        if head == ("workspace", "close"):
            for pane in [p for p, row in self.panes.items() if row["workspace_id"] == args[2]]:
                self.remove_pane(pane)
            return ""
        if head == ("tab", "list"):
            ws = opt(args, "--workspace")
            return {"result": {"tabs": [t for t in self.tabs.values() if ws in (None, t["workspace_id"])]}}
        if head == ("tab", "create"):
            tab = self.add_tab(opt(args, "--workspace"), opt(args, "--label"))
            pane = self.add_pane(tab, opt(args, "--cwd"))
            return {"result": {"tab": self.tabs[tab], "root_pane": self.panes[pane]}}
        if head == ("tab", "rename"):
            self.tabs[args[2]]["label"] = args[3]
            return ""
        if head == ("tab", "close"):
            for pane in [p for p, row in self.panes.items() if row["tab_id"] == args[2]]:
                self.remove_pane(pane)
            return ""
        if head == ("pane", "list"):
            ws = opt(args, "--workspace")
            return {"result": {"panes": [p for p in self.panes.values() if ws in (None, p["workspace_id"])]}}
        if head == ("pane", "split"):
            pane = self.add_pane(self.panes[args[2]]["tab_id"], opt(args, "--cwd"))
            return {"result": {"pane": self.panes[pane]}}
        if head == ("pane", "close"):
            self.remove_pane(args[2])
            return ""
        if head == ("pane", "process-info"):
            pane = opt(args, "--pane")
            name = self.busy.get(pane, "zsh")
            return {"result": {"process_info": {"pane_id": pane, "shell_pid": 1, "foreground_processes": [
                {"name": name, "pid": 1 if name == "zsh" else 2, "cmdline": name}]}}}
        if head == ("pane", "move"):
            pane = args[2]
            if "--new-workspace" in args:
                self.next_ws += 1
                ws = "w%d" % self.next_ws
                self.workspaces[ws] = {"workspace_id": ws, "label": opt(args, "--label")}
                tab = self.add_tab(ws, opt(args, "--tab-label") or "1")
            elif "--new-tab" in args:
                tab = self.add_tab(opt(args, "--workspace"), opt(args, "--label"))
            else:
                tab = opt(args, "--tab")
                target = opt(args, "--target-pane")
                assert target is None or self.panes[target]["tab_id"] == tab, "target pane is not in --tab"
            return {"result": {"move_result": {"pane": self.move_pane(pane, tab)}}}
        raise AssertionError("unexpected herdr call: %r" % (args,))


class LayoutTest(HomeTest):
    """A parent agent irt-102 in its own workspace, against FakeLayoutHerdr, with git and repo_root stubbed."""

    def setUp(self):
        super().setUp()
        self.saved = ht.herdr, ht.repo_root, ht.git
        self.fake = FakeLayoutHerdr()
        self.parent_ws, self.parent_tab, self.parent_pane = self.fake.add_workspace("IRT-102 parent", "/code/repo")
        self.fake.add_agent("irt-102", self.parent_pane)
        ht.herdr, ht.repo_root = self.fake, lambda path: REPO
        ht.git = lambda path, *args: (0, "fix/PROJ-1-dedupe")

    def tearDown(self):
        ht.herdr, ht.repo_root, ht.git = self.saved
        super().tearDown()

    def launch_child(self, name="irt-102-1-dedupe", layout="workspace", **kw):
        return ht.launch_child("/code/repo-PROJ-1", name, "claude", "Read the brief", "irt-102", "PROJ-1", "dedupe",
                               layout=layout, sleep=lambda s: None, **kw)


class LocateRecord(LayoutTest):
    def test_finds_a_moved_child_by_name_and_saves_its_new_ids(self):
        _, tab, pane = self.fake.add_workspace("elsewhere")
        self.fake.add_agent("irt-102-1-dedupe", pane)
        ht.write_record(child_record())
        agent = ht.locate_record(ht.read_record("irt-102-1-dedupe"))
        self.assertEqual(agent["pane_id"], pane)
        record = ht.read_record("irt-102-1-dedupe")
        self.assertEqual((record["workspace_id"], record["tab_id"], record["pane_id"]), ("w2", tab, pane))
        self.assertEqual(record["history"][-1]["event"], "located")

    def test_a_child_not_listed_is_gone_and_its_record_is_left_alone(self):
        ht.write_record(child_record())
        self.assertIsNone(ht.locate_record(ht.read_record("irt-102-1-dedupe")))
        self.assertEqual(ht.read_record("irt-102-1-dedupe"), child_record())


class LaunchChild(LayoutTest):
    def test_workspace_layout_opens_the_child_as_a_labelled_tab_in_the_parents_workspace(self):
        summary = self.launch_child()
        self.assertEqual(self.fake.called("tab", "create"), [(
            "tab", "create", "--workspace", self.parent_ws, "--cwd", os.path.abspath("/code/repo-PROJ-1"),
            "--label", "1 PROJ-1 dedupe", "--no-focus")])
        child = self.fake.agent_named("irt-102-1-dedupe")
        self.assertEqual(child["workspace_id"], self.parent_ws)
        self.assertNotEqual(child["tab_id"], self.parent_tab)
        self.assertEqual((summary["status"], summary["parent"], summary["tab_id"]), ("working", "irt-102", child["tab_id"]))
        self.assertEqual(summary["record"], ht.agent_record_path("irt-102-1-dedupe"))
        self.assertEqual(self.fake.called("worktree"), [])

    def test_the_record_says_where_the_child_is_and_what_it_works_on(self):
        self.launch_child()
        record = ht.read_record("irt-102-1-dedupe")
        child = self.fake.agent_named("irt-102-1-dedupe")
        self.assertEqual({k: record[k] for k in ("child", "parent", "n", "key", "label", "layout", "workspace_id",
                                                 "tab_id", "pane_id", "worktree", "branch", "main_checkout", "state")},
                         {"child": "irt-102-1-dedupe", "parent": "irt-102", "n": 1, "key": "PROJ-1",
                          "label": "1 PROJ-1 dedupe", "layout": "workspace", "workspace_id": self.parent_ws,
                          "tab_id": child["tab_id"], "pane_id": child["pane_id"],
                          "worktree": os.path.abspath("/code/repo-PROJ-1"), "branch": "fix/PROJ-1-dedupe",
                          "main_checkout": REPO, "state": "live"})
        self.assertTrue(record["spawned_at"])
        self.assertEqual([h["event"] for h in record["history"]], ["spawned"])

    def test_shared_layout_splits_the_parents_pane(self):
        self.launch_child(layout="shared")
        self.assertEqual(self.fake.called("pane", "split"), [(
            "pane", "split", self.parent_pane, "--direction", "right", "--cwd", os.path.abspath("/code/repo-PROJ-1"),
            "--no-focus")])
        child = self.fake.agent_named("irt-102-1-dedupe")
        self.assertEqual(child["tab_id"], self.parent_tab)
        self.assertEqual(ht.read_record("irt-102-1-dedupe")["layout"], "shared")

    def test_the_number_comes_from_the_childs_name_else_counts_up(self):
        self.launch_child(name="irt-102-4-dedupe")
        self.assertEqual(ht.read_record("irt-102-4-dedupe")["n"], 4)
        self.launch_child(name="dedupe-helper")
        self.assertEqual(ht.read_record("dedupe-helper")["label"], "5 PROJ-1 dedupe")

    def test_at_a_trust_prompt_it_still_records_the_child_and_saves_the_prompt(self):
        self.fake.screen = TRUST_SCREEN
        summary = self.launch_child()
        self.assertEqual((summary["trust_prompt"], summary["prompted"]), (True, False))
        self.assertEqual(self.fake.called("agent", "prompt"), [])
        self.assertIsNotNone(ht.read_record("irt-102-1-dedupe"))

    def test_refuses_a_parent_that_is_not_a_running_named_agent(self):
        with self.assertRaisesRegex(ht.HerdrError, "No agent named 'nobody'"):
            ht.launch_child("/code/repo-PROJ-1", "nobody-1-x", "claude", "p", "nobody", "PROJ-1", "x",
                            sleep=lambda s: None)
        self.assertEqual(self.fake.called("tab"), [])

    def test_refuses_a_name_that_already_has_a_live_record(self):
        ht.write_record(child_record())
        with self.assertRaisesRegex(ht.HerdrError, "already has a live record"):
            self.launch_child()
        self.assertEqual(self.fake.called("tab"), [])

    def test_refuses_an_unknown_layout(self):
        with self.assertRaisesRegex(ht.HerdrError, "workspace or shared"):
            self.launch_child(layout="grid")


class OpenParent(LayoutTest):
    def open(self):
        return ht.open_parent("irt-200", "IRT-200 outage", "/code/repo", "IRT", "claude", "Read the brief",
                              sleep=lambda s: None)

    def test_creates_the_shared_workspace_when_missing_and_uses_its_first_tab(self):
        summary = self.open()
        ws = next(w for w in self.fake.workspaces.values() if w["label"] == "IRT")
        tabs = [t for t in self.fake.tabs.values() if t["workspace_id"] == ws["workspace_id"]]
        self.assertEqual([t["label"] for t in tabs], ["IRT-200 outage"])
        self.assertEqual(self.fake.agent_named("irt-200")["workspace_id"], ws["workspace_id"])
        self.assertEqual((summary["workspace_id"], summary["status"]), (ws["workspace_id"], "working"))

    def test_adds_a_tab_to_an_existing_shared_workspace(self):
        ws, _, _ = self.fake.add_workspace("IRT")
        self.open()
        self.assertEqual(self.fake.called("workspace", "create"), [])
        self.assertEqual(self.fake.called("tab", "create"), [(
            "tab", "create", "--workspace", ws, "--cwd", os.path.abspath("/code/repo"), "--label", "IRT-200 outage",
            "--no-focus")])
        self.assertEqual(self.fake.agent_named("irt-200")["workspace_id"], ws)


class CliTest(LayoutTest):
    """Runs the CLI through main() inside a pretend Herdr pane."""

    def setUp(self):
        super().setUp()
        saved = os.environ.get("HERDR_ENV")
        os.environ["HERDR_ENV"] = "1"
        self.addCleanup(lambda: os.environ.pop("HERDR_ENV") if saved is None else os.environ.__setitem__(
            "HERDR_ENV", saved))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ht.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class LayoutCli(CliTest):
    def test_next_child_name_counts_from_the_parents_records(self):
        ht.write_record(child_record())
        self.assertEqual(self.run_main("next-child-name", "--parent", "irt-102", "--slug", "Retry jobs")[:2],
                         (0, "irt-102-2-retry-jobs\n"))

    def test_next_child_name_refuses_a_parent_that_is_not_running(self):
        code, _, err = self.run_main("next-child-name", "--parent", "irt-999", "--slug", "x")
        self.assertEqual(code, 1)
        self.assertIn("No agent named 'irt-999'", err)

    def test_launch_with_a_parent_needs_a_key_and_topic_but_no_label(self):
        code, out, _ = self.run_main("launch", "--path", "/code/repo-PROJ-1", "--name", "irt-102-1-dedupe",
                                     "--parent", "irt-102", "--key", "PROJ-1", "--topic", "dedupe",
                                     "--layout", "shared", "--prompt", "go")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["layout"], "shared")
        code, _, err = self.run_main("launch", "--path", "/x", "--name", "irt-102-2-x", "--parent", "irt-102",
                                     "--prompt", "go")
        self.assertEqual(code, 1)
        self.assertIn("--key and --topic", err)

    def test_launch_without_a_parent_still_needs_a_label(self):
        code, _, err = self.run_main("launch", "--path", "/x", "--name", "x", "--prompt", "go")
        self.assertEqual(code, 1)
        self.assertIn("--label", err)

    def test_open_parent_starts_the_parent_in_the_shared_workspace(self):
        code, out, _ = self.run_main("open-parent", "--name", "irt-200", "--label", "IRT-200 outage", "--cwd",
                                     "/code/repo", "--shared-label", "IRT", "--prompt", "go")
        self.assertEqual((code, json.loads(out)["shared_label"]), (0, "IRT"))


def write_report(agent, **fields):
    header = dict({"agent": agent, "state": "in-progress", "safe_to_retire": "no"}, **fields)
    path = ht.status_report_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("---\n%s\n---\n## Summary\nx\n" % "\n".join("%s: %s" % kv for kv in header.items()))
    return path


class ChildMoves(LayoutTest):
    def test_children_lists_each_child_with_where_it_is_and_its_reports_state_and_age(self):
        self.launch_child()
        self.launch_child(name="irt-102-2-retry", layout="shared")
        os.utime(write_report("irt-102-1-dedupe", state="mr-open"), (0, time_now() - 600))
        rows = ht.children("irt-102", now=time_now())
        self.assertEqual([(r["child"], r["where"], r["pane"], r["report"], r["report_age_minutes"]) for r in rows],
                         [("irt-102-1-dedupe", "tab 1 PROJ-1 dedupe", "working", "mr-open", 10),
                          ("irt-102-2-retry", "pane in the parent's tab", "working", None, None)])
        self.assertEqual(rows[0]["worktree"], os.path.abspath("/code/repo-PROJ-1"))

    def test_children_shows_a_popped_out_or_gone_child(self):
        self.launch_child()
        ht.write_record(child_record("irt-102-2-old", n=2))
        ht.pop_out("irt-102-1-dedupe")
        self.assertEqual([r["where"] for r in ht.children("irt-102")], ["own workspace", "gone"])

    def test_pop_out_moves_the_child_to_its_own_workspace_and_updates_its_record(self):
        self.launch_child()
        ht.pop_out("irt-102-1-dedupe")
        pane = self.fake.called("pane", "move")[0][2]
        self.assertEqual(self.fake.called("pane", "move"), [
            ("pane", "move", pane, "--new-workspace", "--label", "1 PROJ-1 dedupe", "--no-focus")])
        child = self.fake.agent_named("irt-102-1-dedupe")
        self.assertNotEqual(child["workspace_id"], self.parent_ws)
        record = ht.read_record("irt-102-1-dedupe")
        self.assertEqual((record["workspace_id"], record["pane_id"], record["history"][-1]["event"]),
                         (child["workspace_id"], child["pane_id"], "pop-out"))

    def test_pop_in_puts_a_workspace_layout_child_back_as_a_tab(self):
        self.launch_child()
        ht.pop_out("irt-102-1-dedupe")
        popped = self.fake.agent_named("irt-102-1-dedupe")
        ht.pop_in("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("pane", "move")[-1], (
            "pane", "move", popped["pane_id"], "--new-tab", "--workspace", self.parent_ws, "--label",
            "1 PROJ-1 dedupe", "--no-focus"))
        self.assertEqual(self.fake.agent_named("irt-102-1-dedupe")["workspace_id"], self.parent_ws)
        self.assertNotIn(popped["workspace_id"], self.fake.workspaces)
        self.assertEqual(ht.read_record("irt-102-1-dedupe")["history"][-1]["event"], "pop-in")

    def test_pop_in_puts_a_shared_layout_child_back_beside_its_parent(self):
        self.launch_child(layout="shared")
        ht.pop_out("irt-102-1-dedupe")
        popped = self.fake.agent_named("irt-102-1-dedupe")
        ht.pop_in("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("pane", "move")[-1], (
            "pane", "move", popped["pane_id"], "--tab", self.parent_tab, "--split", "right", "--target-pane",
            self.parent_pane, "--no-focus"))
        self.assertEqual(self.fake.agent_named("irt-102-1-dedupe")["tab_id"], self.parent_tab)

    def test_pop_out_and_pop_in_refuse_a_child_already_there_or_gone(self):
        self.launch_child()
        with self.assertRaisesRegex(ht.HerdrError, "already beside its parent"):
            ht.pop_in("irt-102-1-dedupe")
        ht.pop_out("irt-102-1-dedupe")
        with self.assertRaisesRegex(ht.HerdrError, "already in its own workspace"):
            ht.pop_out("irt-102-1-dedupe")
        ht.write_record(child_record("irt-102-2-old", n=2))
        with self.assertRaisesRegex(ht.HerdrError, "irt-102-2-old is not running"):
            ht.pop_out("irt-102-2-old")
        with self.assertRaisesRegex(ht.HerdrError, "No record for nobody"):
            ht.pop_in("nobody")


class Collapse(LayoutTest):
    def test_refuses_a_live_child_whose_report_is_not_merged_and_safe(self):
        self.launch_child()
        write_report("irt-102-1-dedupe", state="merged", safe_to_retire="no")
        with self.assertRaisesRegex(ht.HerdrError, "not finished"):
            ht.collapse("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("tab", "close") + self.fake.called("pane", "close"), [])

    def test_closes_a_merged_and_safe_childs_tab_and_keeps_its_record_as_retired(self):
        self.launch_child()
        tab = self.fake.agent_named("irt-102-1-dedupe")["tab_id"]
        write_report("irt-102-1-dedupe", state="merged", safe_to_retire="yes")
        ht.collapse("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("tab", "close"), [("tab", "close", tab)])
        self.assertIn("irt-102", self.fake.agents)
        record = ht.read_record("irt-102-1-dedupe")
        self.assertEqual((record["state"], record["history"][-1]["event"]), ("retired", "collapse"))

    def test_closes_only_the_pane_of_a_shared_child_never_the_parents_tab(self):
        self.launch_child(layout="shared")
        pane = self.fake.agent_named("irt-102-1-dedupe")["pane_id"]
        ht.update_record("irt-102-1-dedupe", "retire", state="retired")
        ht.collapse("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("pane", "close"), [("pane", "close", pane)])
        self.assertEqual(self.fake.called("tab", "close"), [])
        self.assertIn(self.parent_tab, self.fake.tabs)

    def test_a_retired_child_whose_agent_exited_closes_its_recorded_pane_only_if_it_sits_in_the_worktree(self):
        self.launch_child()
        pane = self.fake.agent_named("irt-102-1-dedupe")["pane_id"]
        del self.fake.agents["irt-102-1-dedupe"]
        ht.update_record("irt-102-1-dedupe", "retire", state="retired")
        ht.collapse("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("tab", "close"), [("tab", "close", self.fake.called("tab", "close")[0][2])])
        self.assertNotIn(pane, self.fake.panes)

    def test_a_recorded_pane_now_holding_something_else_is_left_alone(self):
        ht.write_record(child_record(state="retired", pane_id=self.parent_pane))
        result = ht.collapse("irt-102-1-dedupe")
        self.assertIsNone(result["closed"])
        self.assertEqual(self.fake.called("tab", "close") + self.fake.called("pane", "close"), [])
        self.assertIn(self.parent_pane, self.fake.panes)


def time_now():
    import time
    return time.time()


class ChildMovesCli(CliTest):
    def test_children_prints_a_table_or_json(self):
        self.launch_child()
        code, out, _ = self.run_main("children", "irt-102")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("CHILD"))
        self.assertIn("irt-102-1-dedupe", out)
        self.assertEqual(json.loads(self.run_main("children", "irt-102", "--json")[1])[0]["child"], "irt-102-1-dedupe")

    def test_pop_out_pop_in_and_collapse_run_by_name(self):
        self.launch_child()
        self.assertEqual(self.run_main("pop-out", "irt-102-1-dedupe")[0], 0)
        self.assertEqual(self.run_main("pop-in", "irt-102-1-dedupe")[0], 0)
        code, _, err = self.run_main("collapse", "irt-102-1-dedupe")
        self.assertEqual(code, 1)
        self.assertIn("not finished", err)


class CloseBlockers(unittest.TestCase):
    def test_lists_every_other_agent_and_every_pane_running_more_than_its_shell(self):
        panes = [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}, {"pane_id": "w1:p3"}, {"pane_id": "w1:p4"}]
        agents = [{"name": "me", "pane_id": "w1:p1", "agent_status": "idle"},
                  {"name": "sibling", "pane_id": "w1:p2", "agent_status": "working"}]
        shell = {"shell_pid": 1, "foreground_processes": [{"pid": 1, "cmdline": "-zsh"}]}
        server = {"shell_pid": 1, "foreground_processes": [{"pid": 7, "cmdline": "npm run dev"}]}
        procs = {"w1:p1": shell, "w1:p2": shell, "w1:p3": server, "w1:p4": shell}
        self.assertEqual(ht.close_blockers(panes, agents, procs, keep={"w1:p1"}),
                         ["agent sibling (working) in w1:p2", "process `npm run dev` in w1:p3"])


def linked(path):
    return {"repo_root": REPO, "checkout_path": path, "is_linked_worktree": True}


class RetireTarget(LayoutTest):
    def setUp(self):
        super().setUp()
        self.worktree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.worktree)
        self.toplevel = self.worktree
        ht.git = lambda path, *args: (0, self.toplevel) if args == ("rev-parse", "--show-toplevel") else (0, "")

    def child(self, **fields):
        tab = self.fake.add_tab(self.parent_ws, "1 PROJ-1 dedupe")
        pane = self.fake.add_pane(tab, self.worktree)
        self.fake.add_agent("irt-102-1-dedupe", pane)
        ht.write_record(child_record(worktree=self.worktree, **fields))
        return pane

    def target(self, name="irt-102-1-dedupe", own_pane="w9:p9"):
        return ht.retire_target(name, self.fake.agent_rows(), [self.fake.ws_row(w) for w in self.fake.workspaces],
                                own_workspace=self.parent_ws, own_pane=own_pane)

    def test_a_recorded_childs_worktree_comes_from_its_record_checked_with_git(self):
        self.child()
        ws, record = self.target()
        self.assertEqual(record["child"], "irt-102-1-dedupe")
        self.assertEqual((ws["workspace_id"], ws["label"]), (self.parent_ws, "1 PROJ-1 dedupe"))
        self.assertEqual(ws["worktree"], {"checkout_path": self.worktree, "repo_root": REPO, "is_linked_worktree": True})

    def test_refuses_when_git_disagrees_with_the_record(self):
        self.child()
        self.toplevel = "/somewhere/else"
        with self.assertRaisesRegex(ht.HerdrError, "git says"):
            self.target()

    def test_refuses_a_record_pointing_at_the_main_checkout(self):
        self.child(main_checkout=self.worktree)
        with self.assertRaisesRegex(ht.HerdrError, "main checkout"):
            self.target()

    def test_refuses_the_childs_own_pane_but_not_the_workspace_it_shares_with_this_session(self):
        pane = self.child()
        self.assertEqual(self.target(own_pane="w9:p9")[1]["child"], "irt-102-1-dedupe")
        with self.assertRaisesRegex(ht.HerdrError, "this session"):
            self.target(own_pane=pane)

    def test_a_retired_record_still_serves_a_child_that_is_gone(self):
        ht.write_record(child_record(worktree=self.worktree, state="retired"))
        self.assertEqual(self.target()[1]["child"], "irt-102-1-dedupe")

    def test_a_retired_record_is_ignored_once_its_name_runs_again_as_a_one_workspace_agent(self):
        ht.write_record(child_record("proj9", state="retired", worktree=self.worktree))
        self.fake.add_workspace("PROJ-9 x", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", [p for p, row in self.fake.panes.items() if row["workspace_id"] == "w2"][0])
        ws, record = self.target("proj9")
        self.assertEqual((ws["workspace_id"], record), ("w2", None))

    def test_an_agent_without_a_record_takes_the_one_workspace_path_unchanged(self):
        self.fake.add_workspace("PROJ-9 x", worktree=linked("/code/repo-PROJ-9"))
        pane = [p for p, row in self.fake.panes.items() if row["workspace_id"] == "w2"][0]
        self.fake.add_agent("proj9", pane)
        ws, record = self.target("proj9")
        self.assertEqual((ws["workspace_id"], record), ("w2", None))


class RetireClose(LayoutTest):
    def test_a_child_closes_only_its_tab_and_is_marked_retired(self):
        self.launch_child()
        tab = self.fake.agent_named("irt-102-1-dedupe")["tab_id"]
        ht.retire_close("irt-102-1-dedupe")
        self.assertEqual(self.fake.called("tab", "close"), [("tab", "close", tab)])
        self.assertEqual(self.fake.called("workspace", "close"), [])
        self.assertIn("irt-102", self.fake.agents)
        self.assertEqual(ht.read_record("irt-102-1-dedupe")["state"], "retired")

    def agent_workspace(self):
        ws, tab, pane = self.fake.add_workspace("PROJ-9 x", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", pane)
        return ws, tab

    def test_refuses_a_workspace_holding_another_agent_and_lists_it(self):
        ws, tab = self.agent_workspace()
        self.fake.add_agent("helper", self.fake.add_pane(tab))
        with self.assertRaisesRegex(ht.HerdrError, r"(?s)other live agents or processes.*agent helper \(idle\)"):
            ht.retire_close("proj9")
        self.assertEqual(self.fake.called("workspace", "close"), [])

    def test_refuses_a_workspace_with_a_pane_running_a_process(self):
        ws, _ = self.agent_workspace()
        busy = self.fake.add_pane(self.fake.add_tab(ws, "server"))
        self.fake.busy[busy] = "npm run dev"
        with self.assertRaisesRegex(ht.HerdrError, "process `npm run dev`"):
            ht.retire_close("proj9")
        self.assertIn(ws, self.fake.workspaces)

    def test_closes_a_workspace_holding_only_the_agent_and_idle_shells(self):
        ws, tab = self.agent_workspace()
        self.fake.add_pane(tab)
        ht.retire_close("proj9")
        self.assertEqual(self.fake.called("workspace", "close"), [("workspace", "close", ws)])

    def test_the_close_plan_shows_the_action_and_its_blockers_before_anything_closes(self):
        ws, tab = self.agent_workspace()
        self.fake.add_agent("helper", self.fake.add_pane(tab))
        plan = ht.close_plan("proj9")
        self.assertEqual((plan["action"], plan["target"]), ("workspace close", ws))
        self.assertEqual(plan["blockers"], ["agent helper (idle) in %s:p2" % ws])
        self.launch_child()
        plan = ht.close_plan("irt-102-1-dedupe")
        self.assertEqual((plan["action"], plan["blockers"]), ("tab close", []))


class RetireInfoForAChild(LayoutTest):
    def setUp(self):
        super().setUp()
        self.worktree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.worktree)
        answers = {("rev-parse", "--show-toplevel"): self.worktree, ("branch", "--show-current"): "fix/PROJ-1-dedupe",
                   ("rev-parse", "HEAD"): "abc", ("status", "--porcelain"): ""}
        ht.git = lambda path, *args: (0, answers.get(args, ""))
        saved = ht.project_profile, ht.forge_for, ht.work_tools_path
        self.addCleanup(lambda: setattr(ht, "project_profile", saved[0]) or setattr(ht, "forge_for", saved[1])
                        or setattr(ht, "work_tools_path", saved[2]))
        ht.project_profile = lambda main: dict(ht.resolve_profile(None, "git@github.com:acme/repo.git"),
                                               main_checkout=main, remote="", sources={})
        ht.forge_for = lambda profile: FakeViewForge()
        ht.work_tools_path = lambda: None

    def test_a_childs_worktree_agent_and_close_come_from_its_record_never_the_shared_workspace(self):
        tab = self.fake.add_tab(self.parent_ws, "1 PROJ-1 dedupe")
        self.fake.add_agent("irt-102-1-dedupe", self.fake.add_pane(tab, self.worktree))
        ht.write_record(child_record(worktree=self.worktree))
        info = ht.retire_info("irt-102-1-dedupe")
        self.assertEqual((info["agent"], info["worktree"], info["main_checkout"], info["parent"]),
                         ("irt-102-1-dedupe", self.worktree, REPO, "irt-102"))
        self.assertEqual((info["close"]["action"], info["close"]["target"]), ("tab close", tab))
        self.assertEqual(info["close"]["record"], ht.agent_record_path("irt-102-1-dedupe"))
        self.assertTrue(checks_by_name(info["checks"])["close"]["ok"])


class RetireCloseCli(CliTest):
    def test_retire_close_closes_a_childs_tab_and_refuses_a_shared_workspace_with_the_blockers(self):
        self.launch_child()
        self.assertEqual(self.run_main("retire-close", "irt-102-1-dedupe")[0], 0)
        ws, tab, pane = self.fake.add_workspace("PROJ-9 x", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", pane)
        self.fake.add_agent("helper", self.fake.add_pane(tab))
        code, _, err = self.run_main("retire-close", "proj9")
        self.assertEqual(code, 1)
        self.assertIn("agent helper (idle)", err)


class CloseParent(LayoutTest):
    def test_refuses_while_children_are_live_and_lists_them(self):
        self.launch_child()
        with self.assertRaisesRegex(ht.HerdrError, r"(?s)live children.*irt-102-1-dedupe.*--rehome"):
            ht.close_parent("irt-102")
        self.assertEqual(self.fake.called("workspace", "close") + self.fake.called("tab", "close"), [])

    def test_closes_the_parents_workspace_once_its_children_are_retired(self):
        self.launch_child()
        ht.retire_close("irt-102-1-dedupe")
        result = ht.close_parent("irt-102")
        self.assertEqual(self.fake.called("workspace", "close"), [("workspace", "close", self.parent_ws)])
        self.assertEqual((result["closed"], result["notice"]), ("workspace close " + self.parent_ws, None))

    def test_in_a_shared_workspace_only_the_parents_tab_closes(self):
        other_tab = self.fake.add_tab(self.parent_ws, "IRT-300")
        self.fake.add_agent("irt-300", self.fake.add_pane(other_tab))
        ht.close_parent("irt-102")
        self.assertEqual(self.fake.called("tab", "close"), [("tab", "close", self.parent_tab)])
        self.assertEqual(self.fake.called("workspace", "close"), [])
        self.assertIn("irt-300", self.fake.agents)

    def test_refuses_when_the_parents_own_tab_holds_another_agent(self):
        self.fake.add_agent("stranger", self.fake.add_pane(self.parent_tab))
        other_tab = self.fake.add_tab(self.parent_ws, "IRT-300")
        self.fake.add_agent("irt-300", self.fake.add_pane(other_tab))
        with self.assertRaisesRegex(ht.HerdrError, "agent stranger"):
            ht.close_parent("irt-102")
        self.assertEqual(self.fake.called("tab", "close") + self.fake.called("workspace", "close"), [])

    def test_rehome_moves_live_children_to_the_orphaned_agents_workspace_and_prints_a_notice(self):
        self.launch_child()
        self.launch_child(name="irt-102-2-retry", layout="shared")
        result = ht.close_parent("irt-102", rehome=True)
        first, second = self.fake.agent_named("irt-102-1-dedupe"), self.fake.agent_named("irt-102-2-retry")
        orphans = [w for w, row in self.fake.workspaces.items() if row["label"] == ht.ORPHAN_WORKSPACE_LABEL]
        self.assertEqual(len(orphans), 1)
        self.assertEqual({first["workspace_id"], second["workspace_id"]}, set(orphans))
        self.assertNotEqual(first["tab_id"], second["tab_id"])
        self.assertNotIn("irt-102", self.fake.agents)
        self.assertEqual([ht.read_record(c)["state"] for c in ("irt-102-1-dedupe", "irt-102-2-retry")],
                         ["rehomed", "rehomed"])
        self.assertIn("irt-102-1-dedupe, irt-102-2-retry", result["notice"])
        self.assertIn(ht.ORPHAN_WORKSPACE_LABEL, result["notice"])

    def test_rehome_reuses_an_existing_orphaned_agents_workspace(self):
        orphans, _, _ = self.fake.add_workspace(ht.ORPHAN_WORKSPACE_LABEL)
        self.launch_child()
        ht.close_parent("irt-102", rehome=True)
        self.assertEqual(self.fake.agent_named("irt-102-1-dedupe")["workspace_id"], orphans)

    def test_refuses_to_close_the_pane_this_session_runs_in(self):
        os.environ["HERDR_PANE_ID"], saved = self.parent_pane, os.environ.get("HERDR_PANE_ID")
        self.addCleanup(lambda: os.environ.__setitem__("HERDR_PANE_ID", saved) if saved else os.environ.pop(
            "HERDR_PANE_ID"))
        with self.assertRaisesRegex(ht.HerdrError, "this session"):
            ht.close_parent("irt-102")


class Adopt(LayoutTest):
    def test_records_an_existing_agent_in_its_own_worktree_workspace_as_a_child(self):
        ws, tab, pane = self.fake.add_workspace("PROJ-9 cache", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", pane)
        record = ht.adopt("irt-102", "proj9", key="PROJ-9", topic="cache")
        self.assertEqual({k: record[k] for k in ("child", "parent", "n", "label", "layout", "workspace_id", "tab_id",
                                                 "pane_id", "worktree", "main_checkout", "state")},
                         {"child": "proj9", "parent": "irt-102", "n": 1, "label": "1 PROJ-9 cache",
                          "layout": "workspace", "workspace_id": ws, "tab_id": tab, "pane_id": pane,
                          "worktree": "/code/repo-PROJ-9", "main_checkout": REPO, "state": "live"})
        self.assertEqual(record["history"][0]["event"], "adopted")
        self.assertEqual(ht.read_record("proj9"), record)

    def test_without_a_key_the_label_is_the_number_and_its_workspace_label(self):
        _, _, pane = self.fake.add_workspace("PROJ-9 cache", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", pane)
        self.assertEqual(ht.adopt("irt-102", "proj9", layout="shared")["label"], "1 PROJ-9 cache")

    def test_an_agent_without_worktree_info_takes_its_worktree_from_git(self):
        _, _, pane = self.fake.add_workspace("loose", cwd="/code/repo-PROJ-9/src")
        self.fake.add_agent("proj9", pane)
        ht.git = lambda path, *args: (0, "/code/repo-PROJ-9") if args == ("rev-parse", "--show-toplevel") else (0, "b")
        self.assertEqual(ht.adopt("irt-102", "proj9")["worktree"], "/code/repo-PROJ-9")

    def test_refuses_an_unknown_agent_or_parent_or_one_already_recorded(self):
        with self.assertRaisesRegex(ht.HerdrError, "No agent named 'ghost'"):
            ht.adopt("irt-102", "ghost")
        _, _, pane = self.fake.add_workspace("PROJ-9 cache", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", pane)
        with self.assertRaisesRegex(ht.HerdrError, "No agent named 'irt-999'"):
            ht.adopt("irt-999", "proj9")
        ht.adopt("irt-102", "proj9")
        with self.assertRaisesRegex(ht.HerdrError, "already has a live record"):
            ht.adopt("irt-102", "proj9")


class Consolidate(LayoutTest):
    def adopted(self, name, label, layout="workspace", extra_pane=False):
        ws, tab, pane = self.fake.add_workspace(label, worktree=linked("/code/repo-" + name))
        self.fake.add_agent(name, pane)
        if extra_pane:
            self.fake.busy[self.fake.add_pane(tab)] = "npm run dev"
        ht.adopt("irt-102", name, layout=layout)
        return ws

    def test_a_dry_run_lists_the_moves_and_changes_nothing(self):
        ws = self.adopted("proj9", "PROJ-9 cache")
        result = ht.consolidate(dry_run=True)
        pane = self.fake.agent_named("proj9")["pane_id"]
        self.assertEqual(result["moves"], [{"child": "proj9", "parent": "irt-102", "from": "PROJ-9 cache",
                                            "command": ["pane", "move", pane, "--new-tab", "--workspace",
                                                        self.parent_ws, "--label", "1 PROJ-9 cache", "--no-focus"]}])
        self.assertEqual(self.fake.called("pane", "move"), [])
        self.assertIn(ws, self.fake.workspaces)

    def test_moves_children_beside_their_parent_by_layout_and_reports_emptied_workspaces(self):
        first = self.adopted("proj9", "PROJ-9 cache")
        second = self.adopted("proj10", "PROJ-10 retry", layout="shared", extra_pane=True)
        result = ht.consolidate()
        self.assertEqual(self.fake.agent_named("proj9")["workspace_id"], self.parent_ws)
        self.assertNotEqual(self.fake.agent_named("proj9")["tab_id"], self.parent_tab)
        self.assertEqual(self.fake.agent_named("proj10")["tab_id"], self.parent_tab)
        self.assertNotIn(first, self.fake.workspaces)
        self.assertIn(second, self.fake.workspaces)
        self.assertEqual(result["workspaces"], [{"workspace": "PROJ-9 cache", "result": "removed by Herdr (empty)"},
                                                {"workspace": "PROJ-10 retry", "result": "kept: it still holds 1 pane"}])
        self.assertEqual(self.fake.called("workspace", "close"), [])
        self.assertEqual(ht.read_record("proj9")["history"][-1]["event"], "consolidate")

    def test_skips_children_already_beside_their_parent_gone_or_orphaned(self):
        self.launch_child()
        ht.write_record(child_record("irt-102-2-old", n=2))
        self.adopted("proj9", "PROJ-9 cache")
        del self.fake.agents["irt-102"]
        result = ht.consolidate()
        self.assertEqual(result["moves"], [])
        self.assertEqual(sorted(s["child"] for s in result["skipped"]), ["irt-102-1-dedupe", "irt-102-2-old", "proj9"])


class ParentCli(CliTest):
    def test_close_parent_refuses_live_children_and_rehome_prints_the_notice(self):
        self.launch_child()
        code, _, err = self.run_main("close-parent", "irt-102")
        self.assertEqual(code, 1)
        self.assertIn("live children", err)
        code, out, _ = self.run_main("close-parent", "irt-102", "--rehome")
        self.assertEqual(code, 0)
        self.assertIn("Orphaned agents", json.loads(out)["notice"])

    def test_adopt_then_consolidate_dry_run_and_for_real(self):
        _, _, pane = self.fake.add_workspace("PROJ-9 cache", worktree=linked("/code/repo-PROJ-9"))
        self.fake.add_agent("proj9", pane)
        code, out, _ = self.run_main("adopt", "--parent", "irt-102", "proj9", "--key", "PROJ-9", "--topic", "cache")
        self.assertEqual((code, json.loads(out)["label"]), (0, "1 PROJ-9 cache"))
        code, out, _ = self.run_main("consolidate", "--dry-run")
        self.assertEqual((code, len(json.loads(out)["moves"])), (0, 1))
        self.assertEqual(self.fake.called("pane", "move"), [])
        self.assertEqual(self.run_main("consolidate")[0], 0)
        self.assertEqual(self.fake.agent_named("proj9")["workspace_id"], self.parent_ws)


class StatusGroupsChildren(unittest.TestCase):
    AGENTS = AGENTS + [
        {"name": "a-1-x", "agent": "claude", "agent_status": "blocked", "workspace_id": "w1", "pane_id": "w1:p2",
         "terminal_title_stripped": "Plan gate"},
        {"name": "a-2-y", "agent": "claude", "agent_status": "working", "workspace_id": "w1", "pane_id": "w1:p3",
         "terminal_title_stripped": "Tests"},
        {"name": "gone-1-z", "agent": "claude", "agent_status": "idle", "workspace_id": "w1", "pane_id": "w1:p4",
         "terminal_title_stripped": "Waiting"},
    ]
    RECORDS = [child_record("a-1-x", "a", 1), child_record("a-2-y", "a", 2),
               child_record("gone-1-z", "gone", 1, state="rehomed"), child_record("a-3-old", "a", 3, state="retired")]

    def test_children_follow_their_parent_and_a_child_whose_parent_is_gone_is_an_orphan(self):
        rows = ht.status_rows(WORKSPACES, self.AGENTS, self.RECORDS)
        self.assertEqual([(r["target"], r.get("parent"), r.get("orphan", False)) for r in rows], [
            ("b", None, False), ("d", None, False), ("w2:p1", None, False), ("a", None, False),
            ("a-1-x", "a", False), ("a-2-y", "a", False), ("gone-1-z", "gone", True)])

    def test_the_table_indents_children_and_flags_orphans(self):
        lines = ht.format_status(ht.status_rows(WORKSPACES, self.AGENTS, self.RECORDS)).splitlines()
        child = next(line for line in lines if "a-1-x" in line)
        self.assertRegex(child, r"^blocked\s+  a-1-x ")
        orphan = next(line for line in lines if "gone-1-z" in line)
        self.assertTrue(orphan.endswith("orphan of gone: Waiting"))

    def test_a_childs_own_children_follow_it_one_level_deeper(self):
        agents = self.AGENTS + [{"name": "a-1-x-1-q", "agent": "claude", "agent_status": "idle", "workspace_id": "w1",
                                 "pane_id": "w1:p5", "terminal_title_stripped": "Grandchild"}]
        rows = ht.status_rows(WORKSPACES, agents, self.RECORDS + [child_record("a-1-x-1-q", "a-1-x", 1)])
        self.assertEqual([(r["target"], r.get("depth", 0)) for r in rows][3:7],
                         [("a", 0), ("a-1-x", 1), ("a-1-x-1-q", 2), ("a-2-y", 1)])
        self.assertIn("    a-1-x-1-q", ht.format_status(rows))

    def test_without_records_the_output_is_unchanged(self):
        self.assertEqual(ht.format_status(ht.status_rows(WORKSPACES, AGENTS, [])),
                         ht.format_status(ht.status_rows(WORKSPACES, AGENTS)))
        self.assertEqual(ht.status_rows(WORKSPACES, AGENTS, [child_record("ghost-1-x", "ghost", 1)]),
                         ht.status_rows(WORKSPACES, AGENTS))


class StatusReportParentLine(unittest.TestCase):
    def test_a_report_with_a_parent_line_parses_and_one_without_still_does(self):
        with_parent = ht.parse_status_report("---\nagent: a-1-x\nparent: a\nstate: in-progress\n---\n## Summary\nx\n")
        without = ht.parse_status_report("---\nagent: b\nstate: in-progress\n---\n## Summary\nx\n")
        self.assertEqual(with_parent["fields"]["parent"], "a")
        self.assertNotIn("parent", without["fields"])
        self.assertEqual(without["fields"]["state"], "in-progress")


class StatusCli(CliTest):
    def test_status_reads_the_records_and_groups_children(self):
        self.launch_child()
        out = self.run_main("status")[1].splitlines()
        self.assertEqual([line.split()[1] for line in out[1:]], ["irt-102", "irt-102-1-dedupe"])
        self.assertRegex(out[2], r"^\S+\s+  irt-102-1-dedupe ")


# ---- Health ------------------------------------------------------------------------------------------------------

T0 = 1793000000.0  # any fixed instant; health stamps it in local time and reads it back


def minutes(n):
    return T0 + n * 60


class ObservedState(unittest.TestCase):
    def test_blank_missing_and_unknown_are_no_observation(self):
        for status in ("", "  ", None, "unknown"):
            self.assertIsNone(ht.observed_state(status))

    def test_a_real_status_is_itself(self):
        self.assertEqual(ht.observed_state(" blocked "), "blocked")


class Observe(unittest.TestCase):
    def agent(self, status, seq=1):
        return {"name": "irt-102-1-dedupe", "agent_status": status, "state_change_seq": seq}

    def test_the_first_observation_starts_the_state_and_its_history(self):
        record, changed = ht.observe(child_record(), self.agent("idle", 4), T0)
        self.assertTrue(changed)
        self.assertEqual(record["observed"], {"state": "idle", "seq": 4, "since": ht.stamp(T0),
                                              "checked_at": ht.stamp(T0)})
        self.assertEqual(record["history"], [{"at": ht.stamp(T0), "event": "observed", "state": "idle", "seq": 4}])

    def test_the_same_state_keeps_its_start_and_adds_no_history(self):
        record, _ = ht.observe(child_record(), self.agent("blocked", 4), T0)
        record, changed = ht.observe(record, self.agent("blocked", 4), minutes(10))
        self.assertFalse(changed)
        self.assertEqual(record["observed"]["since"], ht.stamp(T0))
        self.assertEqual(record["observed"]["checked_at"], ht.stamp(minutes(10)))
        self.assertEqual(len(record["history"]), 1)

    def test_a_new_state_restarts_the_clock(self):
        record, _ = ht.observe(child_record(), self.agent("working", 4), T0)
        record, changed = ht.observe(record, self.agent("blocked", 5), minutes(10))
        self.assertTrue(changed)
        self.assertEqual(record["observed"]["since"], ht.stamp(minutes(10)))
        self.assertEqual([h["state"] for h in record["history"]], ["working", "blocked"])

    def test_the_same_state_with_a_new_sequence_number_was_left_and_reentered(self):
        record, _ = ht.observe(child_record(), self.agent("idle", 4), T0)
        record, changed = ht.observe(record, self.agent("idle", 6), minutes(10))
        self.assertTrue(changed)
        self.assertEqual(record["observed"]["since"], ht.stamp(minutes(10)))

    def test_blank_and_unknown_change_nothing(self):
        start, _ = ht.observe(child_record(), self.agent("blocked", 4), T0)
        for status in ("", None, "unknown"):
            record, changed = ht.observe(start, self.agent(status, 9), minutes(10))
            self.assertFalse(changed)
            self.assertEqual(record, start)

    def test_working_is_remembered_as_the_last_time_it_was_seen_working(self):
        record, _ = ht.observe(child_record(), self.agent("working", 4), T0)
        self.assertEqual(record["observed"]["last_working_at"], ht.stamp(T0))
        record, _ = ht.observe(record, self.agent("working", 4), minutes(10))
        self.assertEqual(record["observed"]["last_working_at"], ht.stamp(minutes(10)))
        record, _ = ht.observe(record, self.agent("idle", 5), minutes(20))
        self.assertEqual(record["observed"]["last_working_at"], ht.stamp(minutes(10)))

    def test_a_stop_reentered_unseen_counts_as_work_after_the_previous_check(self):
        record, _ = ht.observe(child_record(), self.agent("idle", 4), T0)
        record, _ = ht.observe(record, self.agent("idle", 6), minutes(10))
        self.assertEqual(record["observed"]["last_working_at"], ht.stamp(T0))

    def test_the_record_passed_in_is_not_changed(self):
        record = child_record()
        ht.observe(record, self.agent("idle"), T0)
        self.assertEqual(record, child_record())


class WaitRecordsObservations(AgentHerdrTest):
    def wait(self, statuses, name="irt-102-1-dedupe"):
        self.use(FakeWaitHerdr(statuses))
        return ht.wait_for(name, ("idle",), timeout_seconds=20, poll_seconds=1, sleep=lambda s: None)

    def test_a_recorded_childs_state_changes_go_to_its_history_once_each(self):
        ht.write_record(child_record())
        self.wait(["working", "", "working", "unknown", "idle"])
        record = ht.read_record("irt-102-1-dedupe")
        self.assertEqual([h["state"] for h in record["history"]], ["working", "idle"])
        self.assertEqual(record["observed"]["state"], "idle")

    def test_an_agent_without_a_record_gets_none(self):
        self.wait(["working", "idle"], name="proj105")
        self.assertIsNone(ht.read_record("proj105"))

    def test_a_retired_record_is_left_alone(self):
        ht.write_record(child_record(state="retired"))
        self.wait(["idle"])
        self.assertEqual(ht.read_record("irt-102-1-dedupe"), child_record(state="retired"))


class ReadTranscriptFromACursor(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        self.path = os.path.join(folder, "s.jsonl")

    def write(self, text, mode="w"):
        with open(self.path, mode + "b") as handle:
            handle.write(text.encode("utf-8"))

    def test_reads_only_what_follows_the_cursor_and_returns_the_new_one(self):
        first = json.dumps(prompt_entry("a")) + "\n"
        self.write(first + json.dumps(prompt_entry("b")) + "\n")
        entries, end = ht.read_transcript(self.path, len(first.encode("utf-8")))
        self.assertEqual([e["message"]["content"] for e in entries], ["b"])
        self.assertEqual(end, os.path.getsize(self.path))

    def test_a_half_written_last_line_waits_for_the_next_read(self):
        first = json.dumps(prompt_entry("a")) + "\n"
        self.write(first + json.dumps(prompt_entry("b"))[:10])
        entries, end = ht.read_transcript(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(end, len(first.encode("utf-8")))

    def test_a_cursor_past_the_end_reads_from_the_start(self):
        self.write(json.dumps(prompt_entry("a")) + "\n")
        entries, _ = ht.read_transcript(self.path, 10 ** 6)
        self.assertEqual(len(entries), 1)

    def test_offsets_count_bytes_not_characters(self):
        first = json.dumps(prompt_entry("caf\u00e9 \u2014 ok"), ensure_ascii=False) + "\n"
        self.write(first + json.dumps(prompt_entry("b")) + "\n")
        _, end = ht.read_transcript(self.path)
        self.assertEqual(end, os.path.getsize(self.path))
        entries, _ = ht.read_transcript(self.path, len(first.encode("utf-8")))
        self.assertEqual([e["message"]["content"] for e in entries], ["b"])

    def test_transcript_entries_still_reads_everything(self):
        self.write(json.dumps(prompt_entry("a")) + "\n" + json.dumps(prompt_entry("b")) + "\n")
        self.assertEqual(len(ht.transcript_entries(self.path)), 2)


class ForbiddenCommands(unittest.TestCase):
    ENTRIES = [prompt_entry("go"), tool_entry("Bash", command="git push --force origin x", description="push"),
               tool_entry("Read", file_path="/x/git push --force"), result_entry(),
               tool_entry("PowerShell", command="Remove-Item -Recurse C:\\x"), tool_entry("Bash", command="ls")]

    def test_shell_commands_are_tool_calls_with_a_command(self):
        self.assertEqual(ht.shell_commands(self.ENTRIES),
                         ["git push --force origin x", "Remove-Item -Recurse C:\\x", "ls"])

    def test_commands_matching_any_pattern(self):
        patterns = [re.compile(r"push\s+--force"), re.compile(r"(?i)remove-item")]
        self.assertEqual(ht.forbidden_commands(self.ENTRIES, patterns),
                         ["git push --force origin x", "Remove-Item -Recurse C:\\x"])

    def test_no_patterns_match_nothing(self):
        self.assertEqual(ht.forbidden_commands(self.ENTRIES, []), [])


def health_report(state):
    return ht.parse_status_report("---\nagent: x\nstate: %s\n---\n## Summary\nx\n" % state)


class ChildProblems(unittest.TestCase):
    def problems(self, pane="idle", since=0, last_working=None, report="in-progress", report_age=60, now=11,
                 forbidden=(), agent=True):
        observed = {"state": pane, "seq": 1, "since": ht.stamp(minutes(since)), "checked_at": ht.stamp(minutes(now))}
        if last_working is not None:
            observed["last_working_at"] = ht.stamp(minutes(last_working))
        record = child_record(observed=observed)
        found = ht.child_problems(record, {"agent_status": pane} if agent else None,
                                  health_report(report) if report else None, report_age, list(forbidden),
                                  minutes(now), interval_minutes=10, blocked_minutes=20)
        for problem in found:
            self.assertEqual(problem["child"], "irt-102-1-dedupe")
            self.assertTrue(problem["evidence"])
        return [p["kind"] for p in found]

    def test_a_healthy_working_child_has_none(self):
        self.assertEqual(self.problems(pane="working", last_working=11, report_age=1), [])

    def test_a_child_no_agent_answers_to_is_gone(self):
        self.assertEqual(self.problems(agent=False), ["gone"])

    def test_blocked_past_the_limit(self):
        self.assertEqual(self.problems(pane="blocked", now=21, report="waiting-on-owner", report_age=1), ["blocked"])
        self.assertEqual(self.problems(pane="blocked", now=19, report="waiting-on-owner", report_age=1), [])

    def test_stopped_without_rewriting_its_report_since_it_was_last_seen_working(self):
        # last seen working at -5, so its report (written at -15) predates that work
        self.assertIn("stale", self.problems(last_working=-5, report="waiting-on-owner", report_age=26))

    def test_a_report_rewritten_after_the_work_is_not_stale(self):
        self.assertEqual(self.problems(last_working=-5, report="waiting-on-owner", report_age=12), [])

    def test_a_stop_younger_than_one_interval_is_not_judged_yet(self):
        self.assertEqual(self.problems(last_working=-5, report="waiting-on-owner", report_age=26, now=9), [])

    def test_a_stopped_child_with_no_report_is_stale(self):
        self.assertEqual(self.problems(report=None, report_age=None), ["stale"])

    def test_never_seen_working_is_not_stale_with_a_report(self):
        self.assertEqual(self.problems(report="waiting-on-owner", report_age=500), [])

    def test_done_counts_as_stopped(self):
        self.assertEqual(self.problems(pane="done", report=None, report_age=None), ["stale"])

    def test_in_progress_while_stopped_for_more_than_an_interval(self):
        self.assertEqual(self.problems(report="in-progress", report_age=1), ["mismatch"])
        self.assertEqual(self.problems(report="in-progress", report_age=1, now=9), [])

    def test_waiting_on_owner_while_working_for_more_than_an_interval(self):
        self.assertEqual(self.problems(pane="working", last_working=11, report="waiting-on-owner"), ["mismatch"])
        self.assertEqual(self.problems(pane="working", last_working=9, now=9, report="waiting-on-owner"), [])

    def test_each_forbidden_command_is_a_problem(self):
        kinds = self.problems(pane="working", last_working=11, report_age=1, forbidden=["rm -rf /", "git push -f"])
        self.assertEqual(kinds, ["forbidden", "forbidden"])

    def test_a_child_never_observed_is_checked_for_gone_and_forbidden_only(self):
        record = child_record()
        found = ht.child_problems(record, {"agent_status": "unknown"}, None, None, ["rm -rf /"], minutes(60),
                                  interval_minutes=10, blocked_minutes=20)
        self.assertEqual([p["kind"] for p in found], ["forbidden"])


class HealthTest(LayoutTest):
    """A parent irt-102 with one recorded Claude child, its status report and its transcript in the test home."""

    CHILD = "irt-102-1-dedupe"

    def setUp(self):
        super().setUp()
        _, tab, pane = self.fake.add_workspace("child")
        self.fake.add_agent(self.CHILD, pane, "working")
        self.fake.agents[self.CHILD].update(state_change_seq=1, agent_session={"value": "sess-9"})
        ht.write_record(child_record())
        folder = os.path.join(self.home, ".claude", "projects", "-code-repo")
        os.makedirs(folder)
        self.transcript = os.path.join(folder, "sess-9.jsonl")
        self.add_entries(prompt_entry("Read the brief"))

    def add_entries(self, *entries):
        with open(self.transcript, "a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def set_pane(self, status, seq):
        self.fake.agents[self.CHILD].update(agent_status=status, state_change_seq=seq)

    def write_report(self, state, at):
        path = ht.status_report_path(self.CHILD)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("---\nagent: %s\nstate: %s\n---\n## Summary\nx\n" % (self.CHILD, state))
        os.utime(path, (at, at))

    def check(self, at, forbid=(r"push\s+--force",)):
        return ht.health_check("irt-102", [re.compile(p) for p in forbid], interval_minutes=10, blocked_minutes=20,
                               now=minutes(at))


class HealthCheck(HealthTest):
    def test_a_healthy_child_has_no_problems_and_its_observation_is_recorded(self):
        self.write_report("in-progress", minutes(0))
        result = self.check(0)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["parent"], "irt-102")
        row = result["children"][0]
        self.assertEqual((row["child"], row["pane"], row["report"], row["forbid"]),
                         (self.CHILD, "working", "in-progress", "checked"))
        record = ht.read_record(self.CHILD)
        self.assertEqual(record["observed"]["state"], "working")
        self.assertEqual(record["transcript_cursor"],
                         {"session": "sess-9", "offset": os.path.getsize(self.transcript)})

    def test_a_forbidden_command_is_reported_once(self):
        self.write_report("in-progress", minutes(0))
        self.add_entries(tool_entry("Bash", command="git push --force origin main"))
        first = self.check(0)
        self.assertEqual([(p["kind"], p["child"]) for p in first["problems"]], [("forbidden", self.CHILD)])
        self.assertIn("git push --force origin main", first["problems"][0]["evidence"])
        self.assertEqual(self.check(5)["problems"], [])
        self.add_entries(tool_entry("Bash", command="ls"), tool_entry("Bash", command="git push  --force"))
        self.assertEqual(len(self.check(10)["problems"]), 1)

    def test_a_child_that_stops_without_rewriting_its_report_is_stale_and_mismatched(self):
        self.write_report("in-progress", minutes(-30))
        self.check(0)
        self.set_pane("idle", 2)
        self.check(10)
        kinds = sorted(p["kind"] for p in self.check(21)["problems"])
        self.assertEqual(kinds, ["mismatch", "stale"])

    def test_one_history_entry_per_state_change(self):
        self.write_report("in-progress", minutes(0))
        for at, status, seq in ((0, "working", 1), (10, "working", 1), (20, "", 1), (30, "blocked", 2)):
            self.set_pane(status, seq)
            self.check(at)
        history = [h for h in ht.read_record(self.CHILD)["history"] if h["event"] == "observed"]
        self.assertEqual([h["state"] for h in history], ["working", "blocked"])

    def test_a_child_that_is_gone_is_a_problem_not_a_crash(self):
        del self.fake.agents[self.CHILD]
        result = self.check(0)
        self.assertEqual([p["kind"] for p in result["problems"]], ["gone"])
        self.assertEqual(result["children"][0]["pane"], "gone")

    def test_only_this_parents_live_children_are_checked(self):
        ht.write_record(child_record("irt-102-2-old", n=2, state="retired"))
        ht.write_record(child_record("irt-9-1-other", parent="irt-9"))
        self.write_report("in-progress", minutes(0))
        self.assertEqual([r["child"] for r in self.check(0)["children"]], [self.CHILD])

    def test_without_patterns_the_transcript_is_not_read(self):
        self.write_report("in-progress", minutes(0))
        result = self.check(0, forbid=())
        self.assertEqual(result["children"][0]["forbid"], "off")
        self.assertNotIn("transcript_cursor", ht.read_record(self.CHILD))

    def test_a_child_with_no_transcript_is_unchecked_not_a_problem(self):
        self.fake.agents[self.CHILD].update(agent="codex", agent_session=None)
        self.write_report("in-progress", minutes(0))
        result = self.check(0)
        self.assertEqual((result["children"][0]["forbid"], result["problems"]), ("unchecked", []))

    def test_a_new_session_starts_its_cursor_again(self):
        self.write_report("in-progress", minutes(0))
        self.check(0)
        self.fake.agents[self.CHILD]["agent_session"] = {"value": "sess-10"}
        with open(os.path.join(os.path.dirname(self.transcript), "sess-10.jsonl"), "w") as handle:
            handle.write(json.dumps(tool_entry("Bash", command="git push --force")) + "\n")
        self.assertEqual([p["kind"] for p in self.check(5)["problems"]], ["forbidden"])


class HealthWatch(HealthTest):
    def watch(self, on_sleep=lambda: None):
        clock, sleeps = [minutes(0)], []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            on_sleep()

        result = ht.health_watch("irt-102", [], interval_minutes=10, blocked_minutes=20, sleep=sleep,
                                 now=lambda: clock[0])
        return result, sleeps

    def test_checks_every_interval_and_returns_the_first_problem(self):
        self.write_report("waiting-on-owner", minutes(0))
        self.set_pane("blocked", 2)
        result, sleeps = self.watch()
        self.assertEqual([p["kind"] for p in result["problems"]], ["blocked"])
        self.assertEqual(sleeps, [600, 600, 600])
        self.assertEqual(result["checked_at"], ht.stamp(minutes(30)))

    def test_returns_with_no_problems_once_no_live_child_is_left(self):
        self.write_report("in-progress", minutes(0))

        def retire():
            ht.update_record(self.CHILD, "retired", state="retired")

        result, sleeps = self.watch(retire)
        self.assertEqual((result["children"], result["problems"], sleeps), ([], [], [600]))


class HealthCli(HealthTest, CliTest):
    def test_no_problems_exits_0_with_json(self):
        self.write_report("in-progress", time.time())
        code, out, _ = self.run_main("health", "--parent", "irt-102")
        self.assertEqual((code, json.loads(out)["problems"]), (0, []))

    def test_problems_exit_4_with_json(self):
        self.write_report("in-progress", time.time())
        self.add_entries(tool_entry("Bash", command="git push --force"))
        code, out, _ = self.run_main("health", "--parent", "irt-102", "--forbid", r"push\s+--force",
                                     "--forbid", "rm -rf")
        self.assertEqual((code, [p["kind"] for p in json.loads(out)["problems"]]), (4, ["forbidden"]))

    def test_a_bad_pattern_is_a_helper_error(self):
        code, out, err = self.run_main("health", "--parent", "irt-102", "--forbid", "(")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--forbid", err)

    def test_watch_passes_its_limits_through(self):
        calls, saved = [], ht.health_watch
        ht.health_watch = lambda *args, **kwargs: calls.append((args, kwargs)) or {"problems": [], "children": []}
        try:
            code = self.run_main("health", "--parent", "irt-102", "--watch", "--interval-minutes", "5",
                                 "--blocked-minutes", "15")[0]
        finally:
            ht.health_watch = saved
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(("irt-102", []), {"interval_minutes": 5.0, "blocked_minutes": 15.0})])
