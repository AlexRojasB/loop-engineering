"""
Controlled end-of-run finalization and OPTIONAL machine power-off.

This machine is used as a local AI server for long unattended runs, so
the harness may be asked to power it off once the entire requested
workload is finished. That is a genuinely dangerous capability, and it
is built here -- in one small, mockable module -- rather than scattered
as subprocess calls through agent.py and the pipeline.

Three rules shape everything below.

1. Shutdown is the LAST side effect of a SUCCESSFUL finalization path.
   Nothing is powered off until the final audit event has actually been
   written to harness-owned runtime state outside the target repository.

2. Fail toward NOT shutting down. Every uncertainty -- an unwritable
   history file, a repository that would not come clean, work still
   queued, an interrupt, an unexpected exception -- leaves the machine
   powered ON. There is no path here that powers off because something
   was merely assumed to be fine.

3. Opt-in only. Without an explicit --shutdown-when-done (or its config
   equivalent) the policy below can only ever return "not enabled", and
   no executor is ever reached.

The decomposition mirrors those rules:

    ShutdownSettings   what the user asked for (CLI over config)
    WorkloadResult     what the run actually ended as, plus whether
                       there is any remaining work at all
    ShutdownPolicy     may we power off? -- pure, no side effects
    PowerOffExecutor   how a Linux box is actually powered off
    ShutdownController one real power-off request per run, at most
    RunFinalizer       persist `run_finished`, then ask the controller

`WorkloadResult.has_remaining_work()` is the single place the notion of
"is there anything left for this invocation to do" lives. A future job
queue changes that one predicate and nothing else: job finished -> more
queued work? yes, continue; no, shutdown.
"""

import subprocess
import time
from datetime import datetime, timezone


DEFAULT_SHUTDOWN_DELAY_SECONDS = 60

# Ubuntu 24.04 is systemd-managed and /sbin/poweroff, /sbin/shutdown and
# /usr/sbin/poweroff are all symlinks to this same binary. Naming it
# directly and absolutely keeps the authorization surface as narrow as
# possible: exactly one argv, no PATH lookup, no shell.
DEFAULT_POWEROFF_ARGV = (
    "/usr/bin/systemctl",
    "poweroff",
)

RESULT_PASSED = "passed"
RESULT_FAILED = "failed"

MODE_SINGLE_SPEC = "single_spec"
MODE_MULTI_SPEC = "multi_spec"

RUN_FINISHED_EVENT = "run_finished"
SHUTDOWN_REQUESTED_EVENT = "shutdown_requested"

# The one reason a real power-off is ever requested.
SHUTDOWN_REASON = "run_terminal_and_idle"

BLOCK_NOT_ENABLED = "shutdown_not_enabled"
BLOCK_NOT_TERMINAL = "run_not_terminal"
BLOCK_REMAINING_WORK = "remaining_work"
BLOCK_FINALIZATION_FAILED = "finalization_failed"
BLOCK_PERSISTENCE_FAILED = "persistence_failed"
BLOCK_ALREADY_REQUESTED = "shutdown_already_requested"


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


# ------------------------------------------------------------------
# What the user asked for
# ------------------------------------------------------------------


class ShutdownSettings:
    """
    Resolved shutdown configuration for one invocation.

    An explicit CLI value always wins over config.json. `None` means the
    flag was not given at all, which is what makes an explicit
    --no-shutdown-when-done able to override `shutdown_when_done: true`
    in config rather than being indistinguishable from silence.
    """

    def __init__(
        self,
        enabled=False,
        delay_seconds=DEFAULT_SHUTDOWN_DELAY_SECONDS,
        dry_run=False,
        poweroff_argv=DEFAULT_POWEROFF_ARGV
    ):
        self.enabled = bool(enabled)
        self.delay_seconds = max(
            0,
            int(delay_seconds)
        )
        self.dry_run = bool(dry_run)
        self.poweroff_argv = tuple(
            poweroff_argv
        )

    @classmethod
    def resolve(
        cls,
        config=None,
        enabled=None,
        delay_seconds=None,
        dry_run=None
    ):
        config = config or {}

        if enabled is None:
            enabled = config.get(
                "shutdown_when_done",
                False
            )

        if delay_seconds is None:
            delay_seconds = config.get(
                "shutdown_delay_seconds",
                DEFAULT_SHUTDOWN_DELAY_SECONDS
            )

        if dry_run is None:
            dry_run = config.get(
                "shutdown_dry_run",
                False
            )

        try:
            delay_seconds = int(
                delay_seconds
            )

        except (TypeError, ValueError):
            delay_seconds = (
                DEFAULT_SHUTDOWN_DELAY_SECONDS
            )

        argv = (
            config.get(
                "shutdown_command"
            )
            or DEFAULT_POWEROFF_ARGV
        )

        return cls(
            enabled=enabled,
            delay_seconds=delay_seconds,
            dry_run=dry_run,
            poweroff_argv=argv
        )

    def to_dict(self):
        return {
            "shutdown_when_done":
                self.enabled,
            "shutdown_delay_seconds":
                self.delay_seconds,
            "shutdown_dry_run":
                self.dry_run,
        }


# ------------------------------------------------------------------
# What the run actually ended as
# ------------------------------------------------------------------


class WorkloadResult:
    """
    The terminal state of one whole invocation -- never of a single spec
    inside a multi-spec queue.

    `terminal` says the harness reached an explicitly handled end state
    and will do no further work. `finalization_ok` says the repository
    and runtime state were brought to a known-good condition on the way
    there (commits made, rollback verified clean). `remaining_work` is
    the centralized "is there anything left" count.
    """

    def __init__(
        self,
        result,
        mode,
        completed_work=0,
        total_work=0,
        failure_reason=None,
        terminal=True,
        finalization_ok=True,
        remaining_work=0
    ):
        self.result = result
        self.mode = mode
        self.completed_work = int(
            completed_work
        )
        self.total_work = int(
            total_work
        )
        self.failure_reason = failure_reason
        self.terminal = bool(terminal)
        self.finalization_ok = bool(
            finalization_ok
        )
        self.remaining_work = max(
            0,
            int(remaining_work)
        )

    def has_remaining_work(self):
        """
        The single definition of "this invocation still has something to
        execute".

        For --spec-dir today the answer is decided by the ORCHESTRATOR,
        not by any individual spec: a queue that stopped at 4/8 because
        spec 005 exhausted its retries has no remaining work, because
        this invocation will never run specs 6-8. A future job queue
        replaces exactly this predicate.
        """

        return self.remaining_work > 0

    def display_result(self):
        return (
            "PASSED"
            if self.result == RESULT_PASSED
            else "FAILED"
        )

    def audit_metadata(
        self,
        shutdown_when_done=False
    ):
        return {
            "result": self.result,
            "run_mode": self.mode,
            "completed_work":
                self.completed_work,
            "total_work":
                self.total_work,
            "failure_reason":
                self.failure_reason,
            "shutdown_when_done":
                bool(shutdown_when_done),
            "terminal": self.terminal,
            "finalization_ok":
                self.finalization_ok,
            "remaining_work":
                self.remaining_work,
            "timestamp": now_iso(),
        }


def multi_spec_result(
    completed_work,
    total_work,
    failure_reason=None,
    finalization_ok=True
):
    """
    Terminal result of the multi-spec ORCHESTRATOR itself.

    Called only once the orchestrator has stopped for good, so remaining
    work is zero by construction: either the queue completed, or it
    terminally stopped and this invocation will run nothing further.
    """

    return WorkloadResult(
        result=(
            RESULT_FAILED
            if failure_reason
            else RESULT_PASSED
        ),
        mode=MODE_MULTI_SPEC,
        completed_work=completed_work,
        total_work=total_work,
        failure_reason=failure_reason,
        terminal=True,
        finalization_ok=finalization_ok,
        remaining_work=0
    )


def single_spec_result(
    passed,
    failure_reason=None,
    finalization_ok=True
):
    return WorkloadResult(
        result=(
            RESULT_PASSED
            if passed
            else RESULT_FAILED
        ),
        mode=MODE_SINGLE_SPEC,
        completed_work=1 if passed else 0,
        total_work=1,
        failure_reason=(
            None
            if passed
            else failure_reason
        ),
        terminal=True,
        finalization_ok=finalization_ok,
        remaining_work=0
    )


# ------------------------------------------------------------------
# May we power off?
# ------------------------------------------------------------------


class ShutdownDecision:
    def __init__(
        self,
        allowed,
        reason
    ):
        self.allowed = bool(allowed)
        self.reason = reason

    def __repr__(self):
        return (
            "ShutdownDecision("
            f"allowed={self.allowed}, "
            f"reason={self.reason!r})"
        )


class ShutdownPolicy:
    """
    Pure decision function. No printing, no persistence, no subprocess.

    Every branch except the last one refuses.
    """

    def __init__(self, settings):
        self.settings = settings

    def decide(
        self,
        result,
        persisted=True
    ):
        if not self.settings.enabled:
            return ShutdownDecision(
                False,
                BLOCK_NOT_ENABLED
            )

        if not persisted:
            return ShutdownDecision(
                False,
                BLOCK_PERSISTENCE_FAILED
            )

        if result is None or not result.terminal:
            return ShutdownDecision(
                False,
                BLOCK_NOT_TERMINAL
            )

        if result.has_remaining_work():
            return ShutdownDecision(
                False,
                BLOCK_REMAINING_WORK
            )

        if not result.finalization_ok:
            return ShutdownDecision(
                False,
                BLOCK_FINALIZATION_FAILED
            )

        return ShutdownDecision(
            True,
            SHUTDOWN_REASON
        )


# ------------------------------------------------------------------
# How a Linux box is actually powered off
# ------------------------------------------------------------------


class PowerOffExecutor:
    """
    Interface. Tests substitute a recording fake for this; automated
    tests never construct the real one.
    """

    def describe(self):
        raise NotImplementedError

    def poweroff(self):
        raise NotImplementedError


class LinuxPowerOffExecutor(PowerOffExecutor):
    """
    Real power-off, via argv-based subprocess execution.

    Deliberately never shell-interpreted: handing a command that powers
    off a machine to a shell is indefensible, and there is nothing here
    that needs shell syntax. No sudo password is embedded, requested or
    prompted for -- authorizing this argv is a one-time OS configuration
    the operator performs deliberately, outside the harness.
    """

    def __init__(
        self,
        argv=DEFAULT_POWEROFF_ARGV,
        runner=None,
        timeout=30
    ):
        if isinstance(argv, str):
            # A bare string would be a shell-command mistake. Refuse it
            # here rather than let anything downstream be tempted to
            # interpret it.
            raise ValueError(
                "Power-off command must be an argv list, not a "
                "shell string."
            )

        argv = [
            str(part)
            for part in argv
        ]

        if not argv:
            raise ValueError(
                "Power-off argv must not be empty."
            )

        self.argv = argv
        self.timeout = timeout
        self._runner = (
            runner
            or self._run_subprocess
        )

    @staticmethod
    def _run_subprocess(
        argv,
        timeout
    ):
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    def describe(self):
        return " ".join(self.argv)

    def poweroff(self):
        completed = self._runner(
            self.argv,
            self.timeout
        )

        exit_code = getattr(
            completed,
            "returncode",
            0
        )

        return {
            "argv": list(self.argv),
            "exit_code": exit_code,
            "output": (
                getattr(
                    completed,
                    "stdout",
                    ""
                )
                or ""
            )
            + (
                getattr(
                    completed,
                    "stderr",
                    ""
                )
                or ""
            ),
        }


# ------------------------------------------------------------------
# One real power-off request per run, at most
# ------------------------------------------------------------------


class ShutdownController:
    """
    Owns the single power-off request for this process.

    Idempotent by construction: the `requested` latch is set BEFORE the
    executor is reached, so even if a finalization function is invoked
    twice -- by a retry, a nested code path, or a future bug -- the OS
    is asked exactly once.
    """

    def __init__(
        self,
        settings,
        executor=None,
        sleeper=None,
        printer=print
    ):
        self.settings = settings
        self.policy = ShutdownPolicy(
            settings
        )
        self.executor = executor
        self._sleep = (
            sleeper
            if sleeper is not None
            else time.sleep
        )
        self._print = printer

        self.requested = False
        self.executed = False
        self.last_decision = None

    def decide(
        self,
        result,
        persisted=True
    ):
        decision = self.policy.decide(
            result,
            persisted=persisted
        )

        self.last_decision = decision

        return decision

    def request(
        self,
        audit
    ):
        """
        Persist `shutdown_requested`, then -- and only then -- ask the
        OS to power off.

        `audit` is a zero-argument callable that must persist the
        shutdown_requested event and return truthy. If it returns falsey
        or raises, no power-off happens: a shutdown we could not record
        is a shutdown we must not perform.

        Returns True when a power-off was actually requested from the
        executor (or would have been, in dry-run).
        """

        if self.requested:
            self._print(
                "Automatic shutdown already requested; "
                "ignoring duplicate request."
            )
            return False

        try:
            recorded = audit()

        except Exception as exc:
            self._print(
                "Automatic shutdown ABORTED: the "
                "shutdown_requested audit event could not be "
                f"persisted ({exc})."
            )
            return False

        if not recorded:
            self._print(
                "Automatic shutdown ABORTED: the "
                "shutdown_requested audit event could not be "
                "persisted."
            )
            return False

        # Latch first. Everything after this point happens at most once
        # per run, whatever the caller does.
        self.requested = True

        if self.settings.dry_run:
            self._print(
                "Automatic shutdown requested."
            )
            self._print(
                "DRY RUN: would power off machine."
            )
            self._print(
                "Configured delay: "
                f"{self.settings.delay_seconds} seconds."
            )

            if self.executor is not None:
                self._print(
                    "DRY RUN: command that would run: "
                    + self.executor.describe()
                )

            # Deliberately no sleep: a dry run validates the decision
            # path, and must not cost the operator the real delay.
            return True

        if self.executor is None:
            self._print(
                "Automatic shutdown ABORTED: no power-off "
                "executor is configured."
            )
            return False

        if self.settings.delay_seconds > 0:
            self._sleep(
                self.settings.delay_seconds
            )

        result = self.executor.poweroff()

        self.executed = True

        return result if result is not None else True


# ------------------------------------------------------------------
# Persist first, power off last
# ------------------------------------------------------------------


class RunFinalizer:
    """
    The single end-of-run path.

    Order is the whole point and is enforced here, not by convention:

        1. everything the existing harness already does (commits,
           rollback, state.json, history, timing, memory) -- already
           finished before finalize() is called;
        2. `run_finished` written to history.jsonl;
        3. policy decision;
        4. `shutdown_requested` written to history.jsonl;
        5. delay;
        6. OS power-off.

    A failure at step 2 or 4 stops the sequence dead and the machine
    stays on.
    """

    def __init__(
        self,
        config,
        settings,
        controller=None,
        history_writer=None,
        printer=print
    ):
        self.config = config
        self.settings = settings
        self.controller = controller
        self._print = printer

        if history_writer is None:
            from core.state import append_history

            history_writer = append_history

        self._append_history = history_writer

    def _record(
        self,
        event,
        data
    ):
        self._append_history(
            self.config,
            event,
            data
        )

        return True

    def finalize(self, result):
        """
        Returns a report dict describing what was persisted and whether
        a power-off was requested. Never raises for a persistence
        problem: it reports it and refuses to shut down.
        """

        report = {
            "run_finished_persisted": False,
            "shutdown_requested": False,
            "decision": None,
            "reason": None,
        }

        try:
            self._record(
                RUN_FINISHED_EVENT,
                result.audit_metadata(
                    shutdown_when_done=
                        self.settings.enabled
                )
            )

            report[
                "run_finished_persisted"
            ] = True

        except Exception as exc:
            # Loud, because after a reboot this event is how the run is
            # understood at all.
            self._print(
                "WARNING: could not persist the run_finished audit "
                f"event ({exc})."
            )

        decision = ShutdownPolicy(
            self.settings
        ).decide(
            result,
            persisted=report[
                "run_finished_persisted"
            ]
        )

        report["decision"] = decision.allowed
        report["reason"] = decision.reason

        if not self.settings.enabled:
            # Default behaviour: say nothing about shutdown at all.
            return report

        self._print_banner(
            result,
            decision,
            report[
                "run_finished_persisted"
            ]
        )

        if not decision.allowed:
            return report

        if self.controller is None:
            self._print(
                "Automatic shutdown ABORTED: no shutdown "
                "controller is configured."
            )
            return report

        requested = self.controller.request(
            lambda: self._record(
                SHUTDOWN_REQUESTED_EVENT,
                {
                    "reason": SHUTDOWN_REASON,
                    "dry_run":
                        self.settings.dry_run,
                    "delay_seconds":
                        self.settings.delay_seconds,
                    "result": result.result,
                    "run_mode": result.mode,
                    "completed_work":
                        result.completed_work,
                    "total_work":
                        result.total_work,
                    "timestamp": now_iso(),
                }
            )
        )

        report["shutdown_requested"] = bool(
            requested
        )

        return report

    def _print_banner(
        self,
        result,
        decision,
        persisted
    ):
        self._print("")
        self._print("=" * 60)
        self._print("RUN FINISHED")
        self._print("=" * 60)
        self._print(
            f"Result: {result.display_result()}"
        )
        self._print("")

        if result.failure_reason:
            self._print(
                f"Reason: {result.failure_reason}"
            )

        self._print(
            "Runtime state saved."
            if persisted
            else "WARNING: runtime state was NOT fully saved."
        )

        if result.finalization_ok:
            self._print(
                "Repository finalized."
            )

        else:
            self._print(
                "WARNING: repository finalization did not "
                "complete cleanly."
            )

        if not decision.allowed:
            self._print(
                "Automatic shutdown NOT requested: "
                f"{decision.reason}"
            )
            self._print(
                "Machine will stay powered on."
            )
            return

        if self.settings.dry_run:
            # The controller prints the dry-run detail itself, right
            # after the audit event is safely on disk.
            return

        self._print(
            "Automatic shutdown requested."
        )
        self._print(
            "Machine will power off in "
            f"{self.settings.delay_seconds} seconds."
        )


def build_shutdown_controller(
    settings,
    executor=None,
    sleeper=None,
    printer=print
):
    """
    Factory used by agent.py. Tests patch this to inject a recording
    fake executor, so no automated test can reach the real one.
    """

    if executor is None and settings.enabled:
        executor = LinuxPowerOffExecutor(
            argv=settings.poweroff_argv
        )

    return ShutdownController(
        settings,
        executor=executor,
        sleeper=sleeper,
        printer=printer
    )
