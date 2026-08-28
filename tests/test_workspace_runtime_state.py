"""
Regression coverage for harness runtime-state OWNERSHIP.

Ledger Full #2 failed four consecutive spec attempts in 0.07 seconds
each with:

    ERROR: Workspace is not clean.
    ?? .agent/

SPEC ATTEMPT 1 ran for 3352s, failed, and the repository was restored --
but the restore could not remove `.agent/`, because `git restore .` only
touches tracked files and the harness had created that directory inside
the target repository to hold its own state.json and history.jsonl. Every
later attempt then died on the clean-baseline check before doing any
work.

The fix is ownership, not a .gitignore entry: harness runtime state lives
under <harness>/runtime/projects/<project-key>/, and rollback removes
untracked artifacts an attempt left behind.

These tests use real git repositories in temporary directories. No
models are involved.
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent  # noqa: E402
from core.project_runtime import (  # noqa: E402
    LEGACY_RUNTIME_DIRNAME,
    harness_project_runtime_dir,
    assert_runtime_outside_workspace,
    configure_project_runtime,
    is_inside,
    project_runtime_key,
    reclaim_legacy_runtime_state,
    runtime_paths,
)
from core.repository import (  # noqa: E402
    git_status,
    rollback_repository,
)
from core.spec_memory import record_spec_failure  # noqa: E402
from core.state import (  # noqa: E402
    append_history,
    default_state,
    save_state,
)


def git(workspace, *args):
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True
    )


def make_repository(root):
    """
    A minimal committed repository with one tracked production file, one
    tracked test file, one spec, and a .gitignore that ignores build
    output the way a real project does.
    """

    root = Path(root)

    (root / "src").mkdir()
    (root / "specs").mkdir()

    (root / "src" / "service.py").write_text(
        "def deposit():\n    return False\n"
    )

    (root / "src" / "service_test.py").write_text(
        "def test_deposit():\n    assert deposit()\n"
    )

    (root / "specs" / "001-deposit.md").write_text(
        "# Deposit\n\nDeposit funds into an account.\n"
    )

    (root / ".gitignore").write_text(
        "build/\n"
    )

    git(root, "init", "-q")
    git(root, "config", "user.email", "harness@example.invalid")
    git(root, "config", "user.name", "Harness Test")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")

    return root



class RuntimeDirCleanup(unittest.TestCase):
    """
    Base class: a test that configures runtime storage for a temporary
    workspace must not leave harness runtime directories behind for
    workspaces that no longer exist.
    """

    def forget_runtime(self, workspace):
        self.addCleanup(
            shutil.rmtree,
            harness_project_runtime_dir(
                workspace,
                create=False
            ),
            True
        )


class RuntimeLocationTests(RuntimeDirCleanup):
    def test_no_runtime_path_is_inside_the_workspace(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            for key, path in runtime_paths(
                workspace
            ).items():
                self.assertFalse(
                    is_inside(
                        path,
                        workspace
                    ),
                    f"{key} resolves inside the workspace: {path}"
                )

    def test_configure_creates_nothing_in_the_workspace(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            self.forget_runtime(workspace)

            before = sorted(
                p.name
                for p in workspace.iterdir()
            )

            config = configure_project_runtime(
                {},
                workspace
            )

            after = sorted(
                p.name
                for p in workspace.iterdir()
            )

            self.assertEqual(before, after)

            self.assertFalse(
                (
                    workspace
                    / LEGACY_RUNTIME_DIRNAME
                ).exists()
            )

            for key in (
                "state_file",
                "history_file",
                "spec_memory_file",
            ):
                self.assertFalse(
                    is_inside(
                        config[key],
                        workspace
                    ),
                    f"{key} points inside the workspace"
                )

    def test_writing_all_runtime_state_leaves_workspace_untouched(self):
        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            self.forget_runtime(workspace)

            config = configure_project_runtime(
                {},
                workspace
            )

            save_state(
                config,
                default_state("task")
            )

            append_history(
                config,
                "run_started",
                {"version": "test"}
            )

            memory = agent.SpecFailureMemory.load(
                config["spec_memory_file"],
                "scope"
            )

            config["spec_memory"] = memory

            record_spec_failure(
                config,
                "contract/structural",
                "a finding"
            )

            self.assertEqual(
                git_status(workspace),
                ""
            )

            self.assertTrue(
                Path(
                    config["state_file"]
                ).exists()
            )

            self.assertTrue(
                Path(
                    config["history_file"]
                ).exists()
            )

            self.assertTrue(
                Path(
                    config["spec_memory_file"]
                ).exists()
            )

            Path(
                config["state_file"]
            ).unlink()

            Path(
                config["history_file"]
            ).unlink()

            Path(
                config["spec_memory_file"]
            ).unlink()

    def test_distinct_workspaces_get_distinct_runtime_keys(self):
        with TemporaryDirectory() as one, TemporaryDirectory() as two:
            self.assertNotEqual(
                project_runtime_key(one),
                project_runtime_key(two)
            )

    def test_runtime_pointed_into_the_workspace_fails_closed(self):
        """
        The invariant is enforced, not merely intended: any future code
        path that aims runtime state back at the repository must fail
        loudly here.
        """

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            with self.assertRaises(ValueError):
                assert_runtime_outside_workspace(
                    {
                        "state_file": str(
                            workspace
                            / ".agent"
                            / "state.json"
                        )
                    },
                    workspace
                )


class LegacyRuntimeDirectoryTests(RuntimeDirCleanup):
    """
    A workspace already dirtied by an older harness version must heal
    on the next run instead of failing every attempt forever.
    """

    def test_harness_owned_legacy_directory_is_reclaimed(self):
        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            self.forget_runtime(workspace)

            legacy = (
                workspace
                / LEGACY_RUNTIME_DIRNAME
            )

            legacy.mkdir()

            legacy.joinpath(
                "state.json"
            ).write_text('{"phase": "failed"}')

            legacy.joinpath(
                "history.jsonl"
            ).write_text('{"event": "run_started"}\n')

            self.assertIn(
                ".agent",
                git_status(workspace)
            )

            report = reclaim_legacy_runtime_state(
                workspace
            )

            self.assertTrue(report["found"])
            self.assertTrue(report["removed"])
            self.assertFalse(legacy.exists())

            self.assertEqual(
                git_status(workspace),
                ""
            )

            preserved = Path(
                report["preserved_to"]
            )

            self.assertTrue(
                (
                    preserved / "state.json"
                ).exists()
            )

    def test_reclaimed_state_stays_resumable(self):
        """
        Archiving the old state is not enough: a run interrupted under
        the previous layout must still be resumable after the upgrade,
        so the reclaimed state.json/history.jsonl become the ACTIVE
        runtime files when the new location has none.
        """

        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            self.forget_runtime(workspace)

            legacy = (
                workspace
                / LEGACY_RUNTIME_DIRNAME
            )

            legacy.mkdir()

            legacy.joinpath(
                "state.json"
            ).write_text(
                json.dumps(
                    {
                        "phase": "tests_frozen",
                        "workspace": str(workspace)
                    }
                )
            )

            legacy.joinpath(
                "history.jsonl"
            ).write_text(
                '{"event": "run_started"}\n'
            )

            config = configure_project_runtime(
                {},
                workspace
            )

            report = config[
                "legacy_runtime_report"
            ]

            self.assertTrue(report["removed"])
            self.assertIn(
                "state.json",
                report["migrated"]
            )

            state = json.loads(
                Path(
                    config["state_file"]
                ).read_text()
            )

            self.assertEqual(
                state["phase"],
                "tests_frozen"
            )

    def test_reclaim_never_overwrites_newer_active_state(self):
        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            self.forget_runtime(workspace)

            current = configure_project_runtime(
                {},
                workspace
            )

            Path(
                current["state_file"]
            ).write_text(
                '{"phase": "current"}'
            )

            legacy = (
                workspace
                / LEGACY_RUNTIME_DIRNAME
            )

            legacy.mkdir()

            legacy.joinpath(
                "state.json"
            ).write_text(
                '{"phase": "stale"}'
            )

            configure_project_runtime(
                {},
                workspace
            )

            self.assertEqual(
                json.loads(
                    Path(
                        current["state_file"]
                    ).read_text()
                )["phase"],
                "current"
            )

    def test_directory_with_unowned_files_is_left_alone(self):
        """
        Fails closed: `.agent/` might legitimately belong to the user's
        project. The harness only removes a directory it can prove is
        entirely its own.
        """

        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            legacy = (
                workspace
                / LEGACY_RUNTIME_DIRNAME
            )

            legacy.mkdir()

            legacy.joinpath(
                "state.json"
            ).write_text("{}")

            legacy.joinpath(
                "user-notes.md"
            ).write_text("mine")

            report = reclaim_legacy_runtime_state(
                workspace
            )

            self.assertTrue(report["found"])
            self.assertFalse(report["removed"])
            self.assertEqual(
                report["unexpected"],
                ["user-notes.md"]
            )
            self.assertTrue(legacy.exists())

    def test_absent_directory_is_a_no_op(self):
        with TemporaryDirectory() as tmp:
            report = reclaim_legacy_runtime_state(
                tmp
            )

            self.assertFalse(report["found"])
            self.assertFalse(report["removed"])


class RollbackCleanlinessTests(unittest.TestCase):
    def test_rollback_removes_untracked_artifacts_and_restores_edits(self):
        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            original = (
                workspace / "src" / "service.py"
            ).read_text()

            (
                workspace / "src" / "service.py"
            ).write_text("broken\n")

            (
                workspace / "src" / "extra.py"
            ).write_text("new production file\n")

            (
                workspace / LEGACY_RUNTIME_DIRNAME
            ).mkdir()

            (
                workspace
                / LEGACY_RUNTIME_DIRNAME
                / "state.json"
            ).write_text("{}")

            self.assertNotEqual(
                git_status(workspace),
                ""
            )

            residual = rollback_repository(
                workspace
            )

            self.assertEqual(residual, "")

            self.assertEqual(
                (
                    workspace / "src" / "service.py"
                ).read_text(),
                original
            )

            self.assertFalse(
                (
                    workspace / "src" / "extra.py"
                ).exists()
            )

    def test_rollback_keeps_project_ignored_build_output(self):
        """
        `git clean -fd` deliberately, not `-fdx`: build output the
        project itself ignores is the project's business, never shows up
        in `git status --short`, and is expensive to regenerate.
        """

        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            build = workspace / "build"
            build.mkdir()
            build.joinpath("artifact.bin").write_text("x")

            residual = rollback_repository(
                workspace
            )

            self.assertEqual(residual, "")
            self.assertTrue(
                build.joinpath(
                    "artifact.bin"
                ).exists()
            )


class UntrackedFileSafetyTests(RuntimeDirCleanup):
    """
    Removing untracked files is only provably safe when the harness
    knows every untracked file present was created by the attempt being
    discarded. When it does not know that -- a resumed run, or a
    repository that was already dirty before the attempt -- the user's
    files must survive.
    """

    def test_clean_can_be_declined(self):
        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            scratch = workspace / "my-notes.txt"
            scratch.write_text("work in progress")

            (
                workspace / "src" / "service.py"
            ).write_text("broken\n")

            residual = rollback_repository(
                workspace,
                clean_untracked=False
            )

            self.assertTrue(
                scratch.exists(),
                "a user's untracked file was deleted"
            )

            self.assertIn(
                "my-notes.txt",
                residual
            )

            self.assertEqual(
                (
                    workspace / "src" / "service.py"
                ).read_text(),
                "def deposit():\n    return False\n"
            )

    def test_already_dirty_repository_keeps_its_untracked_files(self):
        """
        The Ledger run's second attempt died on the clean-baseline check
        with `?? .agent/` still present. If that rollback had cleaned
        unconditionally, a repository dirtied by the USER would have had
        those files deleted instead.
        """

        with TemporaryDirectory() as tmp:
            workspace = make_repository(tmp)

            self.forget_runtime(workspace)

            scratch = workspace / "user-scratch.md"
            scratch.write_text("mine")

            calls = []

            def attempt(
                config,
                project,
                spec_path,
                isolation=None
            ):
                calls.append(spec_path)
                return False

            argv = [
                "agent.py",
                str(workspace),
                "--spec-dir",
                "specs",
            ]

            with mock.patch.object(
                agent,
                "load_json",
                return_value={
                    "max_spec_attempts": 2
                }
            ), mock.patch.object(
                agent,
                "run_single_spec",
                side_effect=attempt
            ), mock.patch.object(
                sys,
                "argv",
                argv
            ):
                agent.main()

            self.assertTrue(
                scratch.exists(),
                "a pre-existing untracked file was deleted by rollback"
            )


class FailedSpecAttemptRollbackTests(RuntimeDirCleanup):
    """
    THE regression this run demands, end to end through agent.main():

        clean repo
          -> failed spec attempt (harness runtime state written,
             production edited, new files created)
          -> rollback
          -> git status --short == empty

    The multi-spec attempt loop, the rollback and the runtime-state
    configuration are all real. Only the pipeline itself is replaced, by
    a stand-in that fails the way a real attempt fails and dirties
    everything a real attempt could dirty.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.workspace = make_repository(
            self._tmp.name
        )

        self.forget_runtime(self.workspace)

        self.runtime_files = []

        def cleanup_runtime():
            for path in self.runtime_files:
                Path(path).unlink(
                    missing_ok=True
                )

        self.addCleanup(cleanup_runtime)

    def _dirtying_attempt(self, calls):
        """
        Stands in for run_single_spec: writes every kind of state a real
        failed attempt writes, then reports failure.
        """

        def attempt(
            config,
            project,
            spec_path,
            isolation=None
        ):
            calls.append(spec_path)

            save_state(
                config,
                default_state("task")
            )

            append_history(
                config,
                "run_started",
                {"attempt": len(calls)}
            )

            record_spec_failure(
                config,
                "implementation",
                "never reached GREEN"
            )

            self.runtime_files.extend(
                [
                    config["state_file"],
                    config["history_file"],
                    config["spec_memory_file"],
                ]
            )

            # Production work from the failed attempt: an edit to a
            # tracked file plus a brand-new untracked file.
            (
                Path(project)
                / "src"
                / "service.py"
            ).write_text(
                "def deposit():\n    return None\n"
            )

            (
                Path(project)
                / "src"
                / "half_finished.py"
            ).write_text("partial\n")

            return False

        return attempt

    def _run_agent(self, calls, max_spec_attempts=3):
        config = {
            "max_spec_attempts": max_spec_attempts,
            "agentic_implementation_enabled": False,
        }

        argv = [
            "agent.py",
            str(self.workspace),
            "--spec-dir",
            "specs",
        ]

        with mock.patch.object(
            agent,
            "load_json",
            return_value=config
        ), mock.patch.object(
            agent,
            "run_single_spec",
            side_effect=self._dirtying_attempt(
                calls
            )
        ), mock.patch.object(
            sys,
            "argv",
            argv
        ):
            return agent.main()

    def test_repository_is_exactly_clean_after_every_failed_attempt(self):
        calls = []

        self.assertEqual(
            git_status(self.workspace),
            ""
        )

        exit_code = self._run_agent(calls)

        self.assertEqual(exit_code, 1)

        # Every attempt actually ran: no attempt was lost to a stale
        # dirty-workspace failure.
        self.assertEqual(len(calls), 3)

        self.assertEqual(
            git_status(self.workspace),
            "",
            "repository is not clean after rollback"
        )

        self.assertFalse(
            (
                self.workspace
                / LEGACY_RUNTIME_DIRNAME
            ).exists()
        )

        self.assertFalse(
            (
                self.workspace
                / "src"
                / "half_finished.py"
            ).exists()
        )

        self.assertEqual(
            (
                self.workspace
                / "src"
                / "service.py"
            ).read_text(),
            "def deposit():\n    return False\n"
        )

    def test_runtime_state_was_really_written_outside_the_repository(self):
        """
        Guards against the test passing for the wrong reason: the
        repository must be clean BECAUSE the state went elsewhere, not
        because nothing was written.
        """

        calls = []

        self._run_agent(
            calls,
            max_spec_attempts=1
        )

        self.assertTrue(self.runtime_files)

        for path in self.runtime_files:
            self.assertFalse(
                is_inside(
                    path,
                    self.workspace
                ),
                f"runtime file inside repository: {path}"
            )

        history = Path(
            [
                p for p in self.runtime_files
                if p.endswith("history.jsonl")
            ][0]
        )

        self.assertTrue(history.exists())

        events = [
            json.loads(line)["event"]
            for line in history.read_text().splitlines()
            if line.strip()
        ]

        self.assertIn("run_started", events)


if __name__ == "__main__":
    unittest.main()
