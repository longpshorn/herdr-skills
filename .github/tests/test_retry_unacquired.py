"""Tests for the retry-unacquired workflow: runs its retry step's script, as written in the workflow, against recorded jobs JSON and a stubbed `gh`. Needs bash and jq. Run: python3 -m unittest discover -s <this dir> -p 'test_*.py'"""
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, "..", "workflows", "retry-unacquired.yml")
STEP = "- name: Re-run jobs that never got a runner"
RUN_ID = "39472275495"

FAKE_GH = """#!/bin/sh
echo "gh $*" >> "$GH_LOG"
if [ "$1" = api ]; then cat "$FIXTURE"; fi
"""


def step_script():
    """The `run: |` block of the retry step, dedented."""
    lines = open(WORKFLOW, encoding="utf-8").read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == STEP)
    run = start + 1
    assert lines[run].strip() == "run: |", lines[run]
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def run_step(fixture):
    """Run the step as GitHub does (bash -eo pipefail); return (exit code, output, gh calls)."""
    with tempfile.TemporaryDirectory() as tmp:
        gh = os.path.join(tmp, "gh")
        with open(gh, "w") as f:
            f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        log = os.path.join(tmp, "gh.log")
        open(log, "w").close()
        env = dict(
            os.environ,
            PATH=tmp + os.pathsep + os.environ["PATH"],
            GH_LOG=log,
            FIXTURE=os.path.join(HERE, "fixtures", fixture),
            GH_REPO="longpshorn/herdr-skills",
            RUN_ID=RUN_ID,
            ATTEMPT="1",
        )
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step_script()],
            env=env, capture_output=True, text=True,
        )
        with open(log) as f:
            calls = f.read().splitlines()
    return proc.returncode, proc.stdout + proc.stderr, calls


class RetryUnacquired(unittest.TestCase):
    def assert_reruns(self, fixture, rerun):
        code, output, calls = run_step(fixture)
        self.assertEqual(code, 0, output)
        api = [c for c in calls if c.startswith("gh api ")]
        self.assertEqual(len(api), 1, calls)
        self.assertIn(f"repos/longpshorn/herdr-skills/actions/runs/{RUN_ID}/attempts/1/jobs", api[0])
        self.assertIn("--paginate", api[0])
        self.assertIn("--slurp", api[0])
        self.assertEqual([c for c in calls if not c.startswith("gh api ")], rerun, output)
        return output

    def test_reruns_the_one_job_that_never_got_a_runner(self):
        # Recorded from run 39472275495, attempt 1: helper (macos-latest) was never acquired.
        output = self.assert_reruns("one-unacquired.json", [f"gh run rerun {RUN_ID} --failed"])
        self.assertIn("helper (macos-latest)", output)

    def test_reruns_every_unacquired_job_across_pages_in_one_attempt(self):
        # runner_name may be "" or null for a job that never started.
        output = self.assert_reruns("two-unacquired-paged.json", [f"gh run rerun {RUN_ID} --failed"])
        self.assertIn("helper (macos-latest)", output)
        self.assertIn("example (macos-latest)", output)

    def test_retries_nothing_when_another_job_failed_on_a_runner(self):
        output = self.assert_reruns("unacquired-and-failure.json", [])
        self.assertIn("helper (ubuntu-latest)", output)

    def test_retries_nothing_when_no_job_went_unacquired(self):
        self.assert_reruns("failure-only.json", [])


if __name__ == "__main__":
    unittest.main()
