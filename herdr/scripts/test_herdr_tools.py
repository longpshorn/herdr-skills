"""Tests for herdr_tools: its pure logic, and its herdr flows against a stubbed `herdr`. Run: python3 -m unittest discover -s <this dir> -p 'test_*.py'"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import herdr_tools as ht

REPO = "/code/repo"
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
    """Stubs `herdr` and git, and points HOME at a temporary directory for the saved-prompt files."""

    def setUp(self):
        self.saved = ht.herdr, ht.repo_root, os.environ.get("HOME")
        ht.repo_root = lambda path: REPO
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home

    def tearDown(self):
        ht.herdr, ht.repo_root, os.environ["HOME"] = self.saved
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
        self.assertEqual(fake.called("worktree", "open"), [("worktree", "open", "--cwd", REPO, "--path", REPO + "-TICKET-2",
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
        self.assertEqual(ht.status_report_path("res1", home="/home/p"), "/home/p/.cache/herdr/status/res1.md")


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
            "CLAUDE.md": "# App\n\n## Development lifecycle (agents)\nNever merge.\n",
            ".claude/settings.json": {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}},
            "docs/shared-concepts.md": "## Registry\n",
            "package.json": {"scripts": {"docs:impact": "node scripts/docs-impact.mjs"}},
            ".github/pull_request_template.md": "## Docs reviewed\n",
            "scripts/bootstrap.mjs": "install('herdr@herdr-skills')",
        }))
        self.assertEqual([name for name, p in practices.items() if not p["present"]], ["worktree"])

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

    def merge(self, iid, sha):
        self.merged = sha
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
