"""
Deterministic coverage for OPTIONAL unattended power-off.

This feature can turn a machine off, so every test here is built around
one question: could this code path power off a box that should have
stayed on?

Nothing in this file may ever reach a real power-off. The OS executor is
replaced everywhere by a recording fake, the delay by a recording fake
sleeper, and the one test that touches LinuxPowerOffExecutor injects a
fake runner instead of subprocess.

The agent orchestration itself is REAL: agent.main(), the multi-spec
attempt loop, rollback, the shutdown policy, the ordering of audit
events and the idempotency latch all run for real against temporary git
repositories. Only the pipeline and the OS calls are substituted.
"""

import io
import json
import shutil
import subprocess
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent  # noqa: E402
import core.power as power  # noqa: E402
from core.power import (  # noqa: E402
    BLOCK_FINALIZATION_FAILED,
    BLOCK_NOT_ENABLED,
    BLOCK_NOT_TERMINAL,
    BLOCK_PERSISTENCE_FAILED,
    BLOCK_REMAINING_WORK,
    DEFAULT_POWEROFF_ARGV,
    DEFAULT_SHUTDOWN_DELAY_SECONDS,
    LinuxPowerOffExecutor,
    RunFinalizer,
    SHUTDOWN_REASON,
    ShutdownController,
    ShutdownPolicy,
    ShutdownSettings,
    WorkloadResult,
    multi_spec_result,
    single_spec_result,
)
from core.project_runtime import (  # noqa: E402
    harness_project_runtime_dir,
    is_inside,
    runtime_paths,
)
from core.repository import git_status  # noqa: E402


BENCHMARK_REPOSITORY = Path(
    "/home/alex/ai-benchmarks/ledger-pipeline"
)


def git(workspace, *args):
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True
    )


def make_repository(root, spec_count=1):
    """
    A minimal committed repository with one tracked production file and
    `spec_count` queued specs.
    """

    root = Path(root)

    (root / "src").mkdir()
    (root / "specs").mkdir()

    (root / "src" / "service.py").write_text(
        "def deposit():\n    return False\n"
    )

    for index in range(1, spec_count + 1):
        (
            root
            / "specs"
            / f"{index:03d}-work.md"
        ).write_text(
            f"# Work {index}\n\nDo work {index}.\n"
        )

    git(root, "init", "-q")
    git(root, "config", "user.email", "harness@example.invalid")
    git(root, "config", "user.name", "Harness Test")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")

    return root


class FakePowerOffExecutor:
    """
    Stands in for the real Linux executor. Records every call and never
    touches the operating system.
    """

    def __init__(self, journal=None):
        self.calls = []
        self.journal = (
            journal
            if journal is not None
            else []
        )

    def describe(self):
        return "fake-poweroff"

    def poweroff(self):
        self.calls.append("poweroff")
        self.journal.append("poweroff")

        return {
            "argv": ["fake-poweroff"],
            "exit_code": 0,
            "output": ""
        }


class FakeSleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


class ShutdownTestCase(unittest.TestCase):
    """
    Drives real agent.main() runs with a fake power-off executor.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.executor = None
        self.sleeper = None
        self.controller = None
        self.settings = None
        self.journal = []

    def make_workspace(self, spec_count=1):
        root = Path(
            self._tmp.name
        ) / f"repo-{spec_count}-{len(self.journal)}"

        root.mkdir()

        workspace = make_repository(
            root,
            spec_count=spec_count
        )

        self.addCleanup(
            shutil.rmtree,
            harness_project_runtime_dir(
                workspace,
                create=False
            ),
            True
        )

        return workspace

    # -- run helpers -------------------------------------------------

    def run_agent(
        self,
        workspace,
        extra_argv=(),
        config=None,
        spec_attempt=None,
        pipeline_result=True,
        history_writer=None
    ):
        """
        Run agent.main() for real, with a fake power-off executor
        injected through the same factory agent.py uses.

        `spec_attempt` replaces run_single_spec (multi-spec runs);
        `pipeline_result` replaces run_pipeline (single-spec runs).
        """

        config = dict(
            config
            if config is not None
            else {"max_spec_attempts": 2}
        )

        argv = [
            "agent.py",
            str(workspace),
            *extra_argv,
        ]

        journal = self.journal

        def controller_factory(settings, **kwargs):
            self.settings = settings
            self.executor = FakePowerOffExecutor(
                journal=journal
            )
            self.sleeper = FakeSleeper()

            self.controller = ShutdownController(
                settings,
                executor=self.executor,
                sleeper=self.sleeper
            )

            return self.controller

        def finalizer_factory(
            run_config,
            settings,
            controller
        ):
            return RunFinalizer(
                run_config,
                settings,
                controller=controller,
                history_writer=history_writer
            )

        if spec_attempt is None:
            def spec_attempt(
                run_config,
                project,
                spec_path,
                isolation=None
            ):
                journal.append(
                    f"spec:{spec_path}"
                )
                return True

        buffer = io.StringIO()

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    agent,
                    "load_json",
                    return_value=config
                )
            )

            stack.enter_context(
                mock.patch.object(
                    agent,
                    "run_single_spec",
                    side_effect=spec_attempt
                )
            )

            stack.enter_context(
                mock.patch.object(
                    agent,
                    "run_pipeline",
                    return_value=pipeline_result
                )
            )

            stack.enter_context(
                mock.patch.object(
                    agent,
                    "build_shutdown_controller",
                    side_effect=controller_factory
                )
            )

            if history_writer is not None:
                stack.enter_context(
                    mock.patch.object(
                        agent,
                        "build_run_finalizer",
                        side_effect=finalizer_factory
                    )
                )

            stack.enter_context(
                mock.patch.object(
                    sys,
                    "argv",
                    argv
                )
            )

            stack.enter_context(
                redirect_stdout(buffer)
            )

            try:
                exit_code = agent.main()

            finally:
                # Captured even when the run raises: the abnormal-end
                # tests assert on what was (and was not) printed.
                self.output = buffer.getvalue()

        return exit_code

    def history_events(self, workspace):
        path = runtime_paths(
            workspace
        )["history_file"]

        if not Path(path).exists():
            return []

        return [
            json.loads(line)
            for line in Path(
                path
            ).read_text().splitlines()
            if line.strip()
        ]

    def event_names(self, workspace):
        return [
            entry["event"]
            for entry in self.history_events(
                workspace
            )
        ]

    def poweroff_count(self):
        return (
            len(self.executor.calls)
            if self.executor
            else 0
        )


# ======================================================================
# 1. Default behaviour never requests shutdown
# ======================================================================


class DefaultBehaviourTests(ShutdownTestCase):
    def test_default_multi_spec_run_never_requests_shutdown(self):
        workspace = self.make_workspace(
            spec_count=2
        )

        exit_code = self.run_agent(
            workspace,
            extra_argv=["--spec-dir", "specs"]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertFalse(
            self.controller.requested
        )
        self.assertNotIn(
            "shutdown_requested",
            self.event_names(workspace)
        )

    def test_default_single_spec_run_never_requests_shutdown(self):
        workspace = self.make_workspace()

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md"
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.poweroff_count(),
            0
        )

    def test_default_failed_run_never_requests_shutdown(self):
        workspace = self.make_workspace()

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md"
            ],
            pipeline_result=False
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            self.poweroff_count(),
            0
        )

    def test_disabled_run_prints_no_shutdown_warnings(self):
        """
        19. The shutdown-disabled path stays behaviourally unchanged:
        same exit code, same repository outcome, and not one word about
        shutting anything down.
        """

        workspace = self.make_workspace(
            spec_count=2
        )

        exit_code = self.run_agent(
            workspace,
            extra_argv=["--spec-dir", "specs"]
        )

        self.assertEqual(exit_code, 0)

        lowered = self.output.lower()

        for phrase in (
            "shutdown",
            "power off",
            "poweroff",
            "run finished",
        ):
            self.assertNotIn(
                phrase,
                lowered,
                f"disabled run mentioned {phrase!r}"
            )

        self.assertEqual(
            git_status(workspace),
            ""
        )

    def test_explicit_no_flag_overrides_enabled_config(self):
        workspace = self.make_workspace()

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--no-shutdown-when-done",
            ],
            config={
                "shutdown_when_done": True
            }
        )

        self.assertEqual(exit_code, 0)
        self.assertFalse(
            self.settings.enabled
        )
        self.assertEqual(
            self.poweroff_count(),
            0
        )


# ======================================================================
# 2-3. Single-spec terminal results
# ======================================================================


class SingleSpecShutdownTests(ShutdownTestCase):
    def test_successful_single_spec_requests_exactly_one_shutdown(self):
        workspace = self.make_workspace()

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--shutdown-when-done",
                "--shutdown-delay",
                "7",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.poweroff_count(),
            1
        )
        self.assertEqual(
            self.sleeper.calls,
            [7]
        )

        events = self.event_names(workspace)

        self.assertIn("run_finished", events)
        self.assertIn(
            "shutdown_requested",
            events
        )

    def test_controlled_single_spec_failure_may_shutdown(self):
        """
        3. A FAILED but CONTROLLED terminal result still means there is
        nothing left to run, so an operator who opted in gets their
        machine powered off -- after the pipeline's own cleanup and
        after the audit events are on disk.
        """

        workspace = self.make_workspace()

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ],
            pipeline_result=False
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            self.poweroff_count(),
            1
        )

        finished = [
            entry
            for entry in self.history_events(
                workspace
            )
            if entry["event"] == "run_finished"
        ][-1]

        self.assertEqual(
            finished["data"]["result"],
            "failed"
        )
        self.assertEqual(
            finished["data"]["run_mode"],
            "single_spec"
        )
        self.assertTrue(
            finished["data"][
                "shutdown_when_done"
            ]
        )


# ======================================================================
# 4-6. Multi-spec orchestrator terminal results
# ======================================================================


class MultiSpecShutdownTests(ShutdownTestCase):
    def test_no_shutdown_between_successful_specs(self):
        """
        4. Power-off must happen after the ORCHESTRATOR finishes, never
        after an individual spec. The journal proves the ordering.
        """

        workspace = self.make_workspace(
            spec_count=3
        )

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ]
        )

        self.assertEqual(exit_code, 0)

        self.assertEqual(
            self.journal,
            [
                "spec:specs/001-work.md",
                "spec:specs/002-work.md",
                "spec:specs/003-work.md",
                "poweroff",
            ]
        )

    def test_complete_multi_spec_success_requests_one_shutdown(self):
        workspace = self.make_workspace(
            spec_count=3
        )

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.poweroff_count(),
            1
        )

        finished = [
            entry
            for entry in self.history_events(
                workspace
            )
            if entry["event"] == "run_finished"
        ][-1]

        self.assertEqual(
            finished["data"]["result"],
            "passed"
        )
        self.assertEqual(
            finished["data"][
                "completed_work"
            ],
            3
        )
        self.assertEqual(
            finished["data"]["total_work"],
            3
        )

    def test_terminal_multi_spec_failure_requests_one_shutdown(self):
        """
        6. The 4/8-shaped case: the queue stops early because a spec
        exhausted its retries. There is no more work this invocation
        will do, so an opted-in operator gets a power-off -- but only
        after the rollback came back clean.
        """

        workspace = self.make_workspace(
            spec_count=3
        )

        def attempt(
            run_config,
            project,
            spec_path,
            isolation=None
        ):
            self.journal.append(
                f"spec:{spec_path}"
            )

            return not spec_path.endswith(
                "002-work.md"
            )

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ],
            spec_attempt=attempt
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            self.poweroff_count(),
            1
        )

        # Spec 003 was never attempted: the orchestrator stopped.
        self.assertNotIn(
            "spec:specs/003-work.md",
            self.journal
        )

        self.assertEqual(
            self.journal[-1],
            "poweroff"
        )

        finished = [
            entry
            for entry in self.history_events(
                workspace
            )
            if entry["event"] == "run_finished"
        ][-1]

        self.assertEqual(
            finished["data"]["result"],
            "failed"
        )
        self.assertEqual(
            finished["data"][
                "completed_work"
            ],
            1
        )
        self.assertEqual(
            finished["data"]["total_work"],
            3
        )
        self.assertEqual(
            finished["data"][
                "remaining_work"
            ],
            0
        )
        self.assertIn(
            "002-work.md",
            finished["data"][
                "failure_reason"
            ]
        )

    def test_repository_is_clean_after_terminal_failure_shutdown(self):
        """
        17. Nothing about opting into shutdown may leave the target
        repository dirty.
        """

        workspace = self.make_workspace(
            spec_count=2
        )

        def attempt(
            run_config,
            project,
            spec_path,
            isolation=None
        ):
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

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ],
            spec_attempt=attempt
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            self.poweroff_count(),
            1
        )
        self.assertEqual(
            git_status(workspace),
            "",
            "repository was left dirty by a shutdown-enabled run"
        )


# ======================================================================
# 7-8. Dry run
# ======================================================================


class DryRunTests(ShutdownTestCase):
    def test_dry_run_never_executes_the_os_executor(self):
        workspace = self.make_workspace(
            spec_count=2
        )

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-dry-run",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.poweroff_count(),
            0
        )

        # The full decision path still ran, and still recorded itself.
        self.assertTrue(
            self.controller.requested
        )
        self.assertFalse(
            self.controller.executed
        )
        self.assertIn(
            "shutdown_requested",
            self.event_names(workspace)
        )
        self.assertIn(
            "DRY RUN: would power off machine.",
            self.output
        )

    def test_dry_run_does_not_wait_for_the_real_delay(self):
        workspace = self.make_workspace()

        self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--shutdown-when-done",
                "--shutdown-dry-run",
                "--shutdown-delay",
                "600",
            ]
        )

        self.assertEqual(
            self.sleeper.calls,
            [],
            "dry run slept for the configured delay"
        )

        self.assertIn(
            "Configured delay: 600 seconds.",
            self.output
        )

    def test_dry_run_records_dry_run_in_the_audit_event(self):
        workspace = self.make_workspace()

        self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--shutdown-when-done",
                "--shutdown-dry-run",
            ]
        )

        requested = [
            entry
            for entry in self.history_events(
                workspace
            )
            if entry["event"]
            == "shutdown_requested"
        ][-1]

        self.assertTrue(
            requested["data"]["dry_run"]
        )
        self.assertEqual(
            requested["data"]["reason"],
            SHUTDOWN_REASON
        )


# ======================================================================
# 9-10. Ordering: persist first, power off last
# ======================================================================


class OrderingTests(ShutdownTestCase):
    def test_run_finished_is_persisted_before_shutdown_requested(self):
        workspace = self.make_workspace(
            spec_count=2
        )

        self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ]
        )

        events = self.event_names(workspace)

        self.assertIn("run_finished", events)
        self.assertIn(
            "shutdown_requested",
            events
        )

        self.assertLess(
            events.index("run_finished"),
            events.index(
                "shutdown_requested"
            )
        )

    def test_shutdown_requested_is_persisted_before_the_executor(self):
        workspace = self.make_workspace()

        order = []

        def writer(config, event, data=None):
            order.append(
                f"history:{event}"
            )

            from core.state import append_history

            append_history(
                config,
                event,
                data
            )

        journal = self.journal

        original_poweroff = (
            FakePowerOffExecutor.poweroff
        )

        def recording_poweroff(fake):
            order.append("executor")
            return original_poweroff(fake)

        with mock.patch.object(
            FakePowerOffExecutor,
            "poweroff",
            recording_poweroff
        ):
            self.run_agent(
                workspace,
                extra_argv=[
                    "--spec",
                    "specs/001-work.md",
                    "--shutdown-when-done",
                    "--shutdown-delay",
                    "0",
                ],
                history_writer=writer
            )

        self.assertEqual(
            order[-2:],
            [
                "history:shutdown_requested",
                "executor",
            ]
        )

        self.assertLess(
            order.index(
                "history:run_finished"
            ),
            order.index(
                "history:shutdown_requested"
            )
        )

        self.assertEqual(journal, ["poweroff"])


# ======================================================================
# 11-12. Persistence and repository finalization failures
# ======================================================================


class FailureBlocksShutdownTests(ShutdownTestCase):
    def test_run_finished_persistence_failure_prevents_shutdown(self):
        workspace = self.make_workspace()

        def writer(config, event, data=None):
            raise OSError(
                "history volume is read-only"
            )

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ],
            history_writer=writer
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertFalse(
            self.controller.requested
        )
        self.assertIn(
            BLOCK_PERSISTENCE_FAILED,
            self.output
        )

    def test_shutdown_event_persistence_failure_prevents_shutdown(self):
        """
        The second audit write is just as load-bearing as the first: a
        power-off we could not record is a power-off we must not do.
        """

        workspace = self.make_workspace()

        def writer(config, event, data=None):
            if event == "shutdown_requested":
                raise OSError(
                    "history volume is read-only"
                )

            from core.state import append_history

            append_history(
                config,
                event,
                data
            )

        self.run_agent(
            workspace,
            extra_argv=[
                "--spec",
                "specs/001-work.md",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ],
            history_writer=writer
        )

        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertFalse(
            self.controller.requested
        )
        self.assertIn(
            "Automatic shutdown ABORTED",
            self.output
        )

    def test_commit_failure_prevents_shutdown(self):
        """
        12. Repository finalization failed, so the completed work is not
        safely committed. The machine stays on.
        """

        workspace = self.make_workspace(
            spec_count=2
        )

        with mock.patch.object(
            agent,
            "commit_spec_result",
            side_effect=
                subprocess.CalledProcessError(
                    1,
                    ["git", "commit"]
                )
        ):
            exit_code = self.run_agent(
                workspace,
                extra_argv=[
                    "--spec-dir",
                    "specs",
                    "--shutdown-when-done",
                    "--shutdown-delay",
                    "0",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertIn(
            BLOCK_FINALIZATION_FAILED,
            self.output
        )

    def test_unclean_rollback_prevents_shutdown(self):
        """
        12. The queue stopped terminally, but the repository would not
        come back clean, so the harness cannot prove the failed attempt
        was fully discarded. Fail toward staying on.
        """

        workspace = self.make_workspace(
            spec_count=2
        )

        # A file the USER left untracked before the run: rollback is
        # not allowed to remove it, so the repository is still dirty
        # when the queue stops.
        (
            workspace
            / "user-scratch.md"
        ).write_text("mine\n")

        def attempt(
            run_config,
            project,
            spec_path,
            isolation=None
        ):
            return False

        exit_code = self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ],
            spec_attempt=attempt
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertIn(
            BLOCK_FINALIZATION_FAILED,
            self.output
        )
        self.assertTrue(
            (
                workspace
                / "user-scratch.md"
            ).exists()
        )


# ======================================================================
# 13-14. Abnormal endings never power anything off
# ======================================================================


class AbnormalEndingTests(ShutdownTestCase):
    def test_keyboard_interrupt_prevents_automatic_shutdown(self):
        workspace = self.make_workspace(
            spec_count=2
        )

        def attempt(
            run_config,
            project,
            spec_path,
            isolation=None
        ):
            raise KeyboardInterrupt()

        with self.assertRaises(
            KeyboardInterrupt
        ):
            self.run_agent(
                workspace,
                extra_argv=[
                    "--spec-dir",
                    "specs",
                    "--shutdown-when-done",
                    "--shutdown-delay",
                    "0",
                ],
                spec_attempt=attempt
            )

        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertFalse(
            self.controller.requested
        )
        self.assertNotIn(
            "shutdown_requested",
            self.event_names(workspace)
        )

    def test_unexpected_exception_prevents_automatic_shutdown(self):
        workspace = self.make_workspace(
            spec_count=2
        )

        def attempt(
            run_config,
            project,
            spec_path,
            isolation=None
        ):
            raise RuntimeError(
                "model backend vanished"
            )

        with self.assertRaises(RuntimeError):
            self.run_agent(
                workspace,
                extra_argv=[
                    "--spec-dir",
                    "specs",
                    "--shutdown-when-done",
                    "--shutdown-delay",
                    "0",
                ],
                spec_attempt=attempt
            )

        self.assertEqual(
            self.poweroff_count(),
            0
        )
        self.assertFalse(
            self.controller.requested
        )

    def test_interrupt_with_shutdown_disabled_says_nothing(self):
        workspace = self.make_workspace()

        def attempt(
            run_config,
            project,
            spec_path,
            isolation=None
        ):
            raise KeyboardInterrupt()

        with self.assertRaises(
            KeyboardInterrupt
        ):
            self.run_agent(
                workspace,
                extra_argv=[
                    "--spec-dir",
                    "specs"
                ],
                spec_attempt=attempt
            )

        self.assertNotIn(
            "shutdown",
            self.output.lower()
        )


# ======================================================================
# 15-16. Controller idempotency and the real executor's shape
# ======================================================================


class ControllerUnitTests(unittest.TestCase):
    def make_controller(
        self,
        enabled=True,
        dry_run=False,
        delay=0
    ):
        settings = ShutdownSettings(
            enabled=enabled,
            delay_seconds=delay,
            dry_run=dry_run
        )

        executor = FakePowerOffExecutor()
        sleeper = FakeSleeper()

        controller = ShutdownController(
            settings,
            executor=executor,
            sleeper=sleeper,
            printer=lambda *a, **k: None
        )

        return settings, controller, executor, sleeper

    def test_shutdown_execution_is_idempotent(self):
        """
        15. Even if finalization is invoked repeatedly, the OS is asked
        exactly once.
        """

        settings, controller, executor, _ = (
            self.make_controller()
        )

        events = []

        finalizer = RunFinalizer(
            {},
            settings,
            controller=controller,
            history_writer=
                lambda config, event, data=None:
                    events.append(event),
            printer=lambda *a, **k: None
        )

        result = single_spec_result(
            passed=True
        )

        finalizer.finalize(result)
        finalizer.finalize(result)
        finalizer.finalize(result)

        self.assertEqual(
            len(executor.calls),
            1
        )
        self.assertEqual(
            events.count(
                "shutdown_requested"
            ),
            1
        )

    def test_dry_run_idempotency_also_holds(self):
        settings, controller, executor, sleeper = (
            self.make_controller(
                dry_run=True,
                delay=600
            )
        )

        finalizer = RunFinalizer(
            {},
            settings,
            controller=controller,
            history_writer=
                lambda config, event, data=None: True,
            printer=lambda *a, **k: None
        )

        finalizer.finalize(
            single_spec_result(passed=True)
        )
        finalizer.finalize(
            single_spec_result(passed=True)
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(sleeper.calls, [])
        self.assertTrue(controller.requested)
        self.assertFalse(controller.executed)


class LinuxExecutorTests(unittest.TestCase):
    def test_executor_uses_argv_and_never_a_shell(self):
        """
        16. The real executor passes an argv LIST to subprocess and
        never enables a shell.
        """

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

            class Completed:
                returncode = 0
                stdout = ""
                stderr = ""

            return Completed()

        executor = LinuxPowerOffExecutor()

        with mock.patch.object(
            power.subprocess,
            "run",
            fake_run
        ):
            result = executor.poweroff()

        self.assertIsInstance(
            captured["argv"],
            list
        )
        self.assertEqual(
            captured["argv"],
            list(DEFAULT_POWEROFF_ARGV)
        )
        self.assertNotIn(
            "shell",
            captured["kwargs"]
        )
        self.assertEqual(
            result["exit_code"],
            0
        )

    def test_shell_string_command_is_refused(self):
        with self.assertRaises(ValueError):
            LinuxPowerOffExecutor(
                argv="/usr/bin/systemctl poweroff"
            )

        with self.assertRaises(ValueError):
            LinuxPowerOffExecutor(argv=[])

    def test_power_module_contains_no_shell_execution(self):
        offenders = [
            line.strip()
            for line in (
                REPO_ROOT
                / "core"
                / "power.py"
            ).read_text().splitlines()
            if "shell=True" in line
        ]

        self.assertEqual(
            offenders,
            [],
            "core/power.py uses shell execution"
        )

    def test_agent_entrypoint_contains_no_shell_execution(self):
        offenders = [
            line.strip()
            for line in (
                REPO_ROOT / "agent.py"
            ).read_text().splitlines()
            if "shell=True" in line
        ]

        self.assertEqual(
            offenders,
            [],
            "agent.py uses shell execution"
        )

    def test_default_command_is_the_systemd_poweroff_binary(self):
        self.assertEqual(
            DEFAULT_POWEROFF_ARGV,
            (
                "/usr/bin/systemctl",
                "poweroff"
            )
        )


# ======================================================================
# Policy, settings precedence and remaining-work centralization
# ======================================================================


class PolicyTests(unittest.TestCase):
    def policy(self, enabled=True):
        return ShutdownPolicy(
            ShutdownSettings(enabled=enabled)
        )

    def test_disabled_policy_always_refuses(self):
        decision = self.policy(
            enabled=False
        ).decide(
            single_spec_result(passed=True)
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            BLOCK_NOT_ENABLED
        )

    def test_non_terminal_result_refuses(self):
        decision = self.policy().decide(
            WorkloadResult(
                result="passed",
                mode="multi_spec",
                terminal=False
            )
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            BLOCK_NOT_TERMINAL
        )

    def test_remaining_work_refuses(self):
        result = WorkloadResult(
            result="passed",
            mode="multi_spec",
            completed_work=4,
            total_work=8,
            remaining_work=4
        )

        self.assertTrue(
            result.has_remaining_work()
        )

        decision = self.policy().decide(
            result
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            BLOCK_REMAINING_WORK
        )

    def test_terminal_queue_stop_has_no_remaining_work(self):
        """
        A queue that stopped at 4/8 is terminal for THIS invocation:
        there is nothing further it will execute.
        """

        result = multi_spec_result(
            completed_work=4,
            total_work=8,
            failure_reason="spec 005 exhausted retries"
        )

        self.assertFalse(
            result.has_remaining_work()
        )

        decision = self.policy().decide(
            result
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(
            decision.reason,
            SHUTDOWN_REASON
        )

    def test_failed_finalization_refuses(self):
        decision = self.policy().decide(
            multi_spec_result(
                completed_work=4,
                total_work=8,
                failure_reason="stopped",
                finalization_ok=False
            )
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            BLOCK_FINALIZATION_FAILED
        )

    def test_failed_persistence_refuses(self):
        decision = self.policy().decide(
            single_spec_result(passed=True),
            persisted=False
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            BLOCK_PERSISTENCE_FAILED
        )


class SettingsPrecedenceTests(unittest.TestCase):
    def test_defaults_are_off(self):
        settings = ShutdownSettings.resolve({})

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.dry_run)
        self.assertEqual(
            settings.delay_seconds,
            DEFAULT_SHUTDOWN_DELAY_SECONDS
        )

    def test_config_can_enable_when_cli_is_silent(self):
        settings = ShutdownSettings.resolve(
            {
                "shutdown_when_done": True,
                "shutdown_delay_seconds": 120
            },
            enabled=None,
            delay_seconds=None
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(
            settings.delay_seconds,
            120
        )

    def test_explicit_cli_overrides_config_in_both_directions(self):
        disabled = ShutdownSettings.resolve(
            {"shutdown_when_done": True},
            enabled=False
        )

        self.assertFalse(disabled.enabled)

        enabled = ShutdownSettings.resolve(
            {"shutdown_when_done": False},
            enabled=True
        )

        self.assertTrue(enabled.enabled)

    def test_explicit_cli_delay_overrides_config(self):
        settings = ShutdownSettings.resolve(
            {"shutdown_delay_seconds": 900},
            delay_seconds=5
        )

        self.assertEqual(
            settings.delay_seconds,
            5
        )

    def test_invalid_config_delay_falls_back_to_default(self):
        settings = ShutdownSettings.resolve(
            {"shutdown_delay_seconds": "soon"}
        )

        self.assertEqual(
            settings.delay_seconds,
            DEFAULT_SHUTDOWN_DELAY_SECONDS
        )

    def test_negative_delay_is_clamped(self):
        settings = ShutdownSettings.resolve(
            {},
            delay_seconds=-30
        )

        self.assertEqual(
            settings.delay_seconds,
            0
        )

    def test_disabled_factory_builds_no_real_executor(self):
        """
        With the feature off, the real Linux executor is never even
        constructed.
        """

        controller = power.build_shutdown_controller(
            ShutdownSettings(enabled=False)
        )

        self.assertIsNone(
            controller.executor
        )


# ======================================================================
# 18. Runtime state stays outside the target repository
# ======================================================================


class RuntimeStateLocationTests(ShutdownTestCase):
    def test_shutdown_audit_events_are_written_outside_the_repo(self):
        workspace = self.make_workspace(
            spec_count=2
        )

        self.run_agent(
            workspace,
            extra_argv=[
                "--spec-dir",
                "specs",
                "--shutdown-when-done",
                "--shutdown-delay",
                "0",
            ]
        )

        history = Path(
            runtime_paths(
                workspace
            )["history_file"]
        )

        self.assertTrue(history.exists())

        self.assertFalse(
            is_inside(
                history,
                workspace
            ),
            "shutdown audit trail was written inside the target "
            "repository"
        )

        self.assertIn(
            "shutdown_requested",
            history.read_text()
        )

        self.assertEqual(
            git_status(workspace),
            ""
        )

    @unittest.skipUnless(
        (
            BENCHMARK_REPOSITORY / ".git"
        ).exists(),
        "benchmark repository is not present"
    )
    def test_benchmark_repository_is_untouched(self):
        """
        17/18. The benchmark repository is not modified by this feature,
        and none of the harness runtime paths for it -- including the
        shutdown audit trail -- live inside it.
        """

        self.assertEqual(
            git_status(
                BENCHMARK_REPOSITORY
            ),
            "",
            "benchmark working tree is not clean"
        )

        for path in runtime_paths(
            BENCHMARK_REPOSITORY
        ).values():
            self.assertFalse(
                is_inside(
                    path,
                    BENCHMARK_REPOSITORY
                ),
                f"runtime path inside benchmark: {path}"
            )


if __name__ == "__main__":
    unittest.main()
