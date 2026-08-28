import hashlib
import json
import re
import urllib.request
from pathlib import Path

from core.contract_challenge import (
    CHALLENGE_KINDS,
    DEFAULT_MAX_CHALLENGES,
    challenge_fingerprint,
    challenge_memory_entry,
    format_challenge,
    normalize_challenge,
    validate_challenge,
)
from core.isolation import WorkIsolation
from core.repository import read_file, run_argv
from core.spec_memory import record_spec_failure
from core.state import (
    append_history,
    mark_phase_completed,
    mark_phase_started,
    save_state,
)


DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MAX_STEPS = 40
DEFAULT_CONTEXT = 16384

# A challenge is an interrupt, not a workflow. Two full validations per
# implementation phase is already generous: the whole point is to stop
# early, and every accepted validation costs two independent reviewer
# calls.
DEFAULT_MAX_CHALLENGE_SUBMISSIONS = 4

# After this many IDENTICAL test failures the harness reminds the agent
# that the dispute procedure exists. In Ledger Full #2 the agent restated
# the same correct diagnosis from step 6 to step 31 without ever changing
# the outcome; a bounded, deterministic nudge is what turns that into a
# decision.
DEFAULT_CHALLENGE_HINT_AFTER_REPEATS = 3

MAX_CHALLENGE_HINTS = 2

CHALLENGE_TOOL = "report_contract_issue"

DIGITS = re.compile(r"\d+")

PATHS = re.compile(r"[A-Za-z]?:?[\\/][^\s:()]+")


def _safe_path(root, requested):
    path = (
        root / requested
    ).resolve()

    if (
        path != root
        and root not in path.parents
    ):
        raise ValueError(
            "Path escapes workspace."
        )

    return path


def _read_file(root, path, isolation=None):
    if (
        isolation is not None
        and isolation.is_restricted(path)
    ):
        return isolation.rejection_message(
            path
        )

    target = _safe_path(
        root,
        path
    )

    return target.read_text()


def _write_file(
    root,
    path,
    content,
    writable_paths
):
    if path not in writable_paths:
        return (
            "WRITE REJECTED: "
            f"{path} is not an authorized "
            "implementation target. "
            "Frozen tests and specifications "
            "must not be modified."
        )

    target = _safe_path(
        root,
        path
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    target.write_text(
        content
    )

    return f"Wrote {path}"


def _list_files(
    root,
    requested_path=None,
    isolation=None
):
    start = root

    if requested_path:
        candidate = _safe_path(
            root,
            requested_path
        )

        if candidate.is_dir():
            start = candidate

    result = []

    for path in start.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(
            root
        )

        if any(
            part in {
                ".git",
                "bin",
                "obj",
                ".agent",
            }
            for part in relative.parts
        ):
            continue

        # Restricted work items are not merely unreadable: they are not
        # discoverable either. Listing them would still tell the agent
        # that future requirements exist.
        if (
            isolation is not None
            and isolation.is_restricted(
                str(relative)
            )
        ):
            continue

        result.append(
            str(relative)
        )

    return "\n".join(
        sorted(result)
    )


SUPPORTED_OPERATIONS = (
    "build",
    "test",
    "test_filtered",
    "git_status",
    "git_diff",
)


def _resolve_operation_argv(
    operation,
    filter_value,
    adapter,
    repository_files
):
    """
    Translate a structured operation request into a subprocess argv
    list. Never builds a shell command string: every element returned
    here becomes its own subprocess argument, so shell metacharacters
    in a filter value (&&, ;, |, >, $(), backticks, ...) cannot become
    shell syntax.

    Returns (argv, error). Exactly one of the two is None.
    """

    if operation == "build":
        return (
            adapter.build_argv(
                repository_files
            ),
            None
        )

    if operation == "test":
        return (
            adapter.test_argv(
                repository_files
            ),
            None
        )

    if operation == "test_filtered":
        if not filter_value:
            return (
                None,
                "OPERATION REJECTED: "
                "'test_filtered' requires a "
                "non-empty 'filter' argument."
            )

        argv = adapter.test_argv(
            repository_files,
            filter=filter_value
        )

        if argv is None:
            return (
                None,
                "OPERATION REJECTED: this "
                "project's language adapter "
                "does not support filtered "
                "test execution."
            )

        return (argv, None)

    if operation == "git_status":
        return (
            ["git", "status", "--short"],
            None
        )

    if operation == "git_diff":
        return (
            ["git", "diff"],
            None
        )

    return (
        None,
        "OPERATION REJECTED.\n"
        "Supported operations:\n- "
        + "\n- ".join(SUPPORTED_OPERATIONS)
    )


def _run_argv(root, argv):
    result = run_argv(
        root,
        argv,
        timeout=240
    )

    output = result["output"]

    if len(output) > 16000:
        output = (
            output[:8000]
            + "\n...[TRUNCATED]...\n"
            + output[-8000:]
        )

    return (
        f"exit_code={result['exit_code']}\n"
        f"{output}"
    )


def _run_operation(
    root,
    operation,
    filter_value,
    adapter,
    repository_files
):
    argv, error = _resolve_operation_argv(
        operation,
        filter_value,
        adapter,
        repository_files
    )

    if error is not None:
        return error

    return _run_argv(root, argv)


CHALLENGE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": CHALLENGE_TOOL,
        "description":
            "Report that the FROZEN TEST CONTRACT itself appears "
            "inconsistent, and stop implementing. Use this ONLY when no "
            "correct implementation of the authorized production files "
            "could satisfy a frozen test - not because a feature is "
            "still missing, and not because the work is difficult. "
            "Filing a report does NOT change or delete any test: the "
            "harness validates the report independently, reproduces the "
            "cited failure, and either reopens the Test Contract or "
            "rejects the report and requires you to continue. Reports "
            "are strictly limited, so file at most one per distinct "
            "contradiction and make the evidence exact. Before using "
            "this tool you must already have written an implementation "
            "in an authorized production file and run the tests at "
            "least once.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(
                        CHALLENGE_KINDS
                    ),
                    "description":
                        "spec_contradiction: the test requires "
                        "behavior the specification forbids or "
                        "contradicts. production_behavior: the test "
                        "requires existing, unchanged production code "
                        "to behave in a way it demonstrably does not. "
                        "object_identity: the test acts on an object, "
                        "id or handle the code under test was never "
                        "given. api_semantics: the test misuses an "
                        "existing API's contract. invariant: the test "
                        "violates another demonstrable invariant."
                },
                "summary": {
                    "type": "string",
                    "description":
                        "One sentence stating the contradiction."
                },
                "failing_tests": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    },
                    "description":
                        "Exact test name(s), copied from the frozen "
                        "test file, that cannot be satisfied."
                },
                "authoritative_requirement": {
                    "type": "string",
                    "description":
                        "The requirement from the task that the test "
                        "contradicts, quoted or closely paraphrased."
                },
                "production_path": {
                    "type": "string",
                    "description":
                        "Repository-relative path of the production "
                        "file that demonstrates the conflict."
                },
                "production_quote": {
                    "type": "string",
                    "description":
                        "A literal excerpt copied from "
                        "production_path proving the conflicting "
                        "behavior. The harness verifies this text "
                        "really occurs in that file."
                },
                "contradiction": {
                    "type": "string",
                    "description":
                        "Concretely, why no conforming "
                        "implementation can satisfy the cited "
                        "test(s). Trace the test line by line."
                }
            },
            "required": [
                "kind",
                "summary",
                "failing_tests",
                "authoritative_requirement",
                "production_path",
                "production_quote",
                "contradiction"
            ]
        }
    }
}


def _tools(challenge_enabled=False):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description":
                    "Read a text file from "
                    "the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type":
                                "string"
                        }
                    },
                    "required": [
                        "path"
                    ]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description":
                    "Replace the complete "
                    "contents of an authorized "
                    "production file. Frozen "
                    "tests and specifications "
                    "cannot be modified.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type":
                                "string"
                        },
                        "content": {
                            "type":
                                "string"
                        }
                    },
                    "required": [
                        "path",
                        "content"
                    ]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description":
                    "List repository files, "
                    "optionally below a path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type":
                                "string"
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_operation",
                "description":
                    "Run a structured, safe repository "
                    "operation: build the project, run "
                    "the full test suite, run a single "
                    "test filter, or inspect Git status/"
                    "diff. Arbitrary shell commands are "
                    "not supported; the harness "
                    "constructs the real command from "
                    "the operation you request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": list(
                                SUPPORTED_OPERATIONS
                            ),
                            "description":
                                "build: compile the "
                                "project. test: run the "
                                "full test suite. "
                                "test_filtered: run only "
                                "tests matching 'filter'. "
                                "git_status: 'git status "
                                "--short'. git_diff: "
                                "'git diff'."
                        },
                        "filter": {
                            "type": "string",
                            "description":
                                "Required only for "
                                "test_filtered. A single "
                                "test filter expression "
                                "(e.g. a fully qualified "
                                "test name or substring), "
                                "passed directly as a "
                                "test-runner argument. "
                                "This is not a shell "
                                "command and cannot "
                                "contain shell syntax."
                        }
                    },
                    "required": [
                        "operation"
                    ]
                }
            }
        }
    ]

    if challenge_enabled:
        tools.append(
            CHALLENGE_TOOL_SCHEMA
        )

    return tools


def _call_model(
    model,
    ollama_url,
    context_size,
    messages,
    tools=None
):
    payload = {
        "model":
            model,

        "messages":
            messages,

        "tools":
            _tools()
            if tools is None
            else tools,

        "stream":
            False,

        "options": {
            "num_ctx":
                context_size
        }
    }

    request = urllib.request.Request(
        ollama_url,
        data=json.dumps(
            payload
        ).encode(),
        headers={
            "Content-Type":
                "application/json"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=420
    ) as response:
        return json.loads(
            response.read()
        )


def _execute_tool(
    root,
    call,
    writable_paths,
    adapter,
    repository_files,
    isolation=None
):
    function = call[
        "function"
    ]

    name = function[
        "name"
    ]

    args = function.get(
        "arguments",
        {}
    )

    print()
    print(
        f">>> AGENTIC TOOL: {name}"
    )

    print(
        json.dumps(
            args,
            indent=2
        )
    )

    try:
        if name == "read_file":
            return _read_file(
                root,
                args["path"],
                isolation
            )

        if name == "write_file":
            return _write_file(
                root,
                args["path"],
                args["content"],
                writable_paths
            )

        if name == "list_files":
            return _list_files(
                root,
                args.get(
                    "path"
                ),
                isolation
            )

        if name == "run_operation":
            return _run_operation(
                root,
                args.get("operation"),
                args.get("filter"),
                adapter,
                repository_files
            )

        return (
            f"Unknown tool: {name}"
        )

    except Exception as exc:
        return (
            "TOOL ERROR: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )


def _failure_signature(result):
    """
    Stable identity of a failure, independent of language or runner.

    Absolute paths, line/column numbers, durations and counts are
    stripped, so "the same failure again" is recognised without knowing
    anything about the toolchain's output format.
    """

    text = PATHS.sub(
        " ",
        result or ""
    )

    text = DIGITS.sub(
        "#",
        text
    )

    text = WHITESPACE_RUN.sub(
        " ",
        text
    ).strip().lower()

    return hashlib.sha256(
        text.encode(
            "utf-8",
            "replace"
        )
    ).hexdigest()


WHITESPACE_RUN = re.compile(r"\s+")


def _production_context(
    root,
    implementation_changes
):
    """
    Current contents of the authorized production files, for the
    independent challenge reviewer. A planned target that does not
    exist yet is simply absent: the reviewer is told what the code IS,
    and "not created yet" is never evidence of a defective contract.
    """

    context = {}

    for change in implementation_changes:
        path = change.get("path")

        if not path:
            continue

        try:
            context[path] = _read_file(
                root,
                path
            )

        except Exception:
            # A missing, unreadable, escaping or undecodable target is
            # simply absent from the reviewer's view. Building this
            # context must never be able to fail the phase.
            continue

    return context


# ---------------------------------------------------------------------
# Phase outcomes
#
# The implementation phase used to answer a plain bool. It now has a
# third answer -- "the frozen contract itself is under challenge" --
# which the pipeline must route differently from ordinary failure, so
# every exit becomes an explicit outcome.
# ---------------------------------------------------------------------

COMPLETED = "completed"

FAILED = "failed"

CONTRACT_CHALLENGED = "contract_challenged"


def completed_outcome(steps=None):
    return {
        "status": COMPLETED,
        "steps": steps
    }


def failed_outcome(reason, steps=None):
    return {
        "status": FAILED,
        "reason": reason,
        "steps": steps
    }


def challenged_outcome(
    challenge,
    verdict,
    steps=None
):
    return {
        "status": CONTRACT_CHALLENGED,
        "challenge": challenge,
        "verdict": verdict,
        "steps": steps
    }


def normalize_implementation_outcome(result):
    """
    Accept either an outcome dict or the legacy bool the non-agentic
    implementation phase still returns.
    """

    if isinstance(result, dict):
        return result

    if result:
        return completed_outcome()

    return failed_outcome(
        "Implementation phase reported failure."
    )


class ChallengeController:
    """
    Bounded, fail-closed gatekeeper for contract challenges inside ONE
    implementation phase.

    Three separate limits, because the failure modes differ:

    - `submissions`: every call to the tool, well-formed or not. Bounds
      the number of agentic steps a confused agent can spend arguing
      with the harness.
    - `reviews`: only submissions that clear the deterministic evidence
      gate reach independent model review. Bounds model cost.
    - prerequisites: no challenge is even considered until the agent has
      actually written an implementation and run the tests, so "I have
      not tried yet" can never be dressed up as "this is impossible".

    Duplicate challenges are recognised by fingerprint and answered
    from the previous verdict instead of being re-validated.
    """

    def __init__(
        self,
        config,
        workspace,
        task,
        frozen_tests,
        root,
        implementation_changes,
        adapter,
        repository_files,
        max_submissions,
        max_reviews,
        runner=None,
        hint_after_repeats=DEFAULT_CHALLENGE_HINT_AFTER_REPEATS
    ):
        self.config = config
        self.workspace = workspace
        self.task = task
        self.frozen_tests = frozen_tests or {}
        self.root = root
        self.implementation_changes = list(
            implementation_changes or []
        )
        self.adapter = adapter
        self.repository_files = repository_files
        self.max_submissions = max_submissions
        self.max_reviews = max_reviews
        self.runner = runner

        self.submissions = 0
        self.reviews = 0
        self.seen = {}

        self.wrote_production = False
        self.ran_tests = False

        self.hint_after_repeats = hint_after_repeats
        self.hints_emitted = 0
        self.last_signature = None
        self.repeat_count = 0

    # -- prerequisites -------------------------------------------------

    def note_write(self, result):
        if isinstance(result, str) and result.startswith(
            "Wrote "
        ):
            self.wrote_production = True

    def note_operation(self, operation, result=None):
        if operation not in (
            "test",
            "test_filtered",
        ):
            return

        # `_run_operation` answers "OPERATION REJECTED: ..." without
        # executing anything. Only a real invocation -- which always
        # reports an exit code -- counts as having run the tests.
        if (
            result is not None
            and not result.startswith("exit_code=")
        ):
            return

        self.ran_tests = True

        if result is None:
            return

        if result.startswith(
            "exit_code=0"
        ):
            self.last_signature = None
            self.repeat_count = 0
            return

        signature = _failure_signature(
            result
        )

        if signature == self.last_signature:
            self.repeat_count += 1

        else:
            self.last_signature = signature
            self.repeat_count = 1

    def stall_hint(self):
        """
        Deterministic, bounded reminder that the dispute procedure
        exists, emitted only once the SAME failure has survived several
        repair rounds and only while a challenge could still be filed.

        This is a reminder, never a verdict: the agent still has to make
        the claim, and the claim is still validated independently.
        """

        if (
            self.hints_emitted >= MAX_CHALLENGE_HINTS
            or self.submissions >= self.max_submissions
            or self.repeat_count < self.hint_after_repeats
        ):
            return None

        self.hints_emitted += 1
        self.repeat_count = 0

        return (
            "HARNESS NOTICE: the identical test failure has now "
            f"survived {self.hint_after_repeats} repair rounds. Either "
            "change your approach substantively, or -- if you can show "
            "that no correct implementation of the authorized files "
            f"could satisfy the failing test -- call {CHALLENGE_TOOL} "
            "once with exact evidence and stop. Do not restate the "
            "same diagnosis again without acting on it."
        )

    # -- submission ----------------------------------------------------

    def _budget_message(self):
        return (
            "The contract-challenge budget for this implementation "
            "attempt is exhausted. The frozen contract stands. "
            "Continue implementing against it, or stop."
        )

    def handle(self, args):
        """
        Returns (tool_result_text, outcome_or_None). A non-None outcome
        means the implementation loop must stop immediately.
        """

        if self.submissions >= self.max_submissions:
            return (
                self._budget_message(),
                None
            )

        self.submissions += 1

        remaining = (
            self.max_submissions
            - self.submissions
        )

        challenge, error = normalize_challenge(
            args
        )

        if error is not None:
            append_history(
                self.config,
                "contract_challenge_malformed",
                {
                    "reason": error,
                    "submission": self.submissions
                }
            )

            return (
                f"{error}\n"
                f"Remaining contract reports: {remaining}.",
                None
            )

        fingerprint = challenge_fingerprint(
            challenge
        )

        if fingerprint in self.seen:
            return (
                "CHALLENGE REJECTED: this is the same report you "
                "already filed, and the previous outcome stands:\n"
                + self.seen[fingerprint]
                + f"\nRemaining contract reports: {remaining}.",
                None
            )

        print()
        print("=" * 60)
        print(
            "CONTRACT CHALLENGE FILED "
            f"({self.submissions}/{self.max_submissions})"
        )
        print("=" * 60)
        print(
            format_challenge(
                challenge
            )
        )

        append_history(
            self.config,
            "contract_challenge_filed",
            {
                "submission": self.submissions,
                "challenge": challenge
            }
        )

        if not (
            self.wrote_production
            and self.ran_tests
        ):
            reason = (
                "CHALLENGE REJECTED: a contract can only be "
                "challenged from a reproduced failure. Write your "
                "implementation into an authorized production file "
                "and run the tests first, then report only if the "
                "contradiction survives."
            )

            # Deliberately NOT memoised against the fingerprint: this
            # refusal says "not yet", not "not true". Caching it would
            # permanently lock out the identical -- and by then
            # legitimate -- report once the agent has implemented and
            # run the tests, which is exactly the Ledger sequence.
            append_history(
                self.config,
                "contract_challenge_premature",
                {
                    "submission": self.submissions
                }
            )

            print(reason)

            return (
                f"{reason}\n"
                f"Remaining contract reports: {remaining}.",
                None
            )

        allow_review = (
            self.reviews < self.max_reviews
        )

        # Read the production files NOW. A challenge filed at step 20
        # must be adjudicated against the code the agent actually
        # wrote, not against the empty stubs that existed at step 0 --
        # otherwise the reviewer is shown a file with no implementation
        # in it and "the feature is missing" looks like a defect.
        production = _production_context(
            self.root,
            self.implementation_changes
        )

        verdict = validate_challenge(
            self.config,
            self.workspace,
            challenge,
            self.task,
            self.frozen_tests,
            production,
            self.adapter,
            self.repository_files,
            read_file,
            runner=self.runner,
            allow_review=allow_review
        )

        if verdict.get("stage") == "review":
            self.reviews += 1

        if verdict["confirmed"]:
            print()
            print(
                "CONTRACT CHALLENGE CONFIRMED by independent "
                "validation. Stopping implementation."
            )

            return (
                verdict["reason"],
                challenged_outcome(
                    challenge,
                    verdict
                )
            )

        rejection = (
            "CHALLENGE REJECTED "
            f"({verdict['stage']}): "
            f"{verdict['reason']}"
        )

        self.seen[fingerprint] = rejection

        print()
        print(rejection)

        return (
            f"{rejection}\n"
            "The frozen contract stands. Continue implementing "
            "against it.\n"
            f"Remaining contract reports: {remaining}.",
            None
        )


def _validate_final_state(
    root,
    adapter,
    repository_files
):
    print()
    print(
        "Agent stopped. Performing "
        "deterministic agentic-phase "
        "validation."
    )

    build = _run_argv(
        root,
        adapter.build_argv(
            repository_files
        )
    )

    print()
    print("AGENTIC FINAL BUILD:")
    print(build)

    if not build.startswith(
        "exit_code=0"
    ):
        return False

    tests = _run_argv(
        root,
        adapter.test_argv(
            repository_files
        )
    )

    print()
    print("AGENTIC FINAL TESTS:")
    print(tests)

    return tests.startswith(
        "exit_code=0"
    )


def run_agentic_implementation_phase(
    config,
    workspace,
    task,
    state,
    implementation_changes,
    build_command,
    test_command,
    adapter,
    repository_files,
    isolation=None,
    frozen_tests=None,
    challenge_runner=None
):
    print()
    print("=" * 60)
    print(
        "PHASE 4 - AGENTIC IMPLEMENTATION"
    )
    print("=" * 60)

    mark_phase_started(
        config,
        state,
        "implementation"
    )

    root = Path(
        workspace
    ).resolve()

    if isolation is None:
        isolation = WorkIsolation.disabled()

    if isolation.active:
        print()
        print(
            isolation.describe()
        )

    writable_paths = {
        change["path"]
        for change
        in implementation_changes
    }

    if not writable_paths:
        print(
            "No authorized implementation "
            "targets."
        )
        return failed_outcome(
            "No authorized implementation targets."
        )

    model = config.get(
        "agentic_model",
        DEFAULT_MODEL
    )

    ollama_url = config.get(
        "ollama_url",
        DEFAULT_OLLAMA_URL
    )

    max_steps = int(
        config.get(
            "agentic_max_steps",
            DEFAULT_MAX_STEPS
        )
    )

    context_size = int(
        config.get(
            "agentic_context_size",
            DEFAULT_CONTEXT
        )
    )

    # The escape hatch only exists when there IS a frozen contract to
    # challenge. A structural change that skipped the Test Contract
    # phase has nothing to appeal against.
    challenge_enabled = bool(
        config.get(
            "contract_challenge_enabled",
            True
        )
        and frozen_tests
    )

    max_challenge_submissions = int(
        config.get(
            "max_contract_challenge_submissions",
            DEFAULT_MAX_CHALLENGE_SUBMISSIONS
        )
    )

    max_challenge_reviews = int(
        config.get(
            "max_contract_challenges",
            DEFAULT_MAX_CHALLENGES
        )
    )

    challenges = None

    if challenge_enabled:
        challenges = ChallengeController(
            config,
            workspace,
            task,
            frozen_tests,
            root,
            implementation_changes,
            adapter,
            repository_files,
            max_challenge_submissions,
            max_challenge_reviews,
            runner=challenge_runner,
            hint_after_repeats=int(
                config.get(
                    "challenge_hint_after_repeats",
                    DEFAULT_CHALLENGE_HINT_AFTER_REPEATS
                )
            )
        )

    tools = _tools(
        challenge_enabled=challenge_enabled
    )

    challenge_rules = (
        f"""
FROZEN CONTRACT DISPUTE PROCEDURE:

If, after implementing and running the tests, you conclude that a frozen
test cannot be satisfied by ANY correct implementation of the authorized
files, do not keep retrying and do not work around it. Call
{CHALLENGE_TOOL} once, with exact evidence, and stop.

- You still must NEVER modify a test. The tool only files a report.
- Filing a report does not change the contract. An independent
  validator reproduces your cited failure and reviews the claim. If it
  is confirmed, the Test Contract is reopened by the harness. If it is
  rejected, the contract stands and you must continue.
- Prerequisites: you must already have written your implementation to an
  authorized production file AND run the tests. A missing feature you
  have not implemented yet is NOT a contract defect.
- 'production_quote' is verified literally against the real file. Do not
  paraphrase it.
- You may file at most {max_challenge_submissions} report(s) in this
  attempt, so do not guess.
"""
        if challenge_enabled
        else ""
    )

    system = f"""
You are an autonomous software engineering implementation agent.

A separate TDD phase has already generated and frozen tests.
Those tests are authoritative and MUST NOT be modified.

You may inspect repository files, modify authorized production
files, run the build, run tests, inspect failures, and repair
the production implementation iteratively.

AUTHORIZED WRITABLE FILES:

{chr(10).join(sorted(writable_paths))}

Rules:

- Treat the supplied task as authoritative.
- Inspect existing production code and frozen tests when useful.
- NEVER modify tests.
- NEVER modify specifications.
- Only write files in AUTHORIZED WRITABLE FILES.
- Preserve existing behavior.
- Prefer minimal production changes.
- Do not introduce unnecessary abstractions or dependencies.
- Run the build after implementation changes.
- Run the tests after the build succeeds.
- If build or tests fail, inspect the exact failure and repair
  production code.
- Continue iterating until build and tests both pass.
- Do not merely describe actions. Use tools.
- Do not stop immediately after a failed command.
- The supplied task is the ONLY specification in scope. Do not implement
  behavior it does not request, even if the repository suggests it.
- Some documents are outside the current work boundary and are neither
  listed nor readable. That is intentional; do not work around it.

Use the run_operation tool for all build/test/Git actions. It does not
accept arbitrary shell commands; you request one of a fixed set of
operations and the harness runs the real command for you:

- operation="build" runs the project build (equivalent to:
  {build_command})
- operation="test" runs the full test suite (equivalent to:
  {test_command})
- operation="test_filtered" with a "filter" argument runs only tests
  matching that filter, useful for iterating on a single failing test
  without rerunning the whole suite
- operation="git_status" runs git status --short
- operation="git_diff" runs git diff
{challenge_rules}"""

    user = f"""
Implement the following frozen-contract task:

{task}

The tests have already been generated and frozen by another
agent. You may read them to understand failures, but you cannot
modify them.

Work until both the required build and test commands succeed.
"""

    messages = [
        {
            "role":
                "system",
            "content":
                system
        },
        {
            "role":
                "user",
            "content":
                user
        }
    ]

    append_history(
        config,
        "agentic_implementation_started",
        {
            "model":
                model,
            "writable_paths":
                sorted(
                    writable_paths
                ),
            "contract_challenge_enabled":
                challenge_enabled
        }
    )

    for step in range(
        1,
        max_steps + 1
    ):
        print()
        print("=" * 60)
        print(
            f"AGENTIC IMPLEMENTATION "
            f"STEP {step}/{max_steps}"
        )
        print("=" * 60)

        try:
            response = _call_model(
                model,
                ollama_url,
                context_size,
                messages,
                tools=tools
            )

        except Exception as exc:
            print(
                "Agentic model call failed: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            return failed_outcome(
                "Agentic model call failed: "
                f"{type(exc).__name__}: {exc}",
                steps=step
            )

        message = response.get(
            "message",
            {}
        )

        messages.append(
            message
        )

        thinking = message.get(
            "thinking",
            ""
        )

        if thinking:
            print()
            print("THINKING:")
            print(thinking)

        content = message.get(
            "content",
            ""
        )

        if content:
            print()
            print("MODEL:")
            print(content)

        tool_calls = message.get(
            "tool_calls",
            []
        )

        if not tool_calls:
            success = (
                _validate_final_state(
                    root,
                    adapter,
                    repository_files
                )
            )

            if success:
                mark_phase_completed(
                    config,
                    state,
                    "implementation"
                )

                append_history(
                    config,
                    "agentic_implementation_completed",
                    {
                        "steps":
                            step
                    }
                )

                save_state(
                    config,
                    state
                )

                return completed_outcome(
                    steps=step
                )

            print(
                "Agent stopped before "
                "reaching GREEN."
            )

            record_spec_failure(
                config,
                "implementation",
                "A previous attempt's implementation never "
                "reached GREEN against the frozen contract. "
                "Re-check that the contract is actually "
                "satisfiable by the requested change alone."
            )

            return failed_outcome(
                "Agent stopped before reaching GREEN.",
                steps=step
            )

        for call in tool_calls:
            name = call[
                "function"
            ][
                "name"
            ]

            outcome = None

            if (
                name == CHALLENGE_TOOL
                and challenges is not None
            ):
                result, outcome = challenges.handle(
                    call[
                        "function"
                    ].get(
                        "arguments",
                        {}
                    )
                )

            elif name == CHALLENGE_TOOL:
                result = (
                    "TOOL UNAVAILABLE: there is no frozen test "
                    "contract to dispute in this phase."
                )

            else:
                result = _execute_tool(
                    root,
                    call,
                    writable_paths,
                    adapter,
                    repository_files,
                    isolation
                )

                if challenges is not None:
                    if name == "write_file":
                        challenges.note_write(
                            result
                        )

                    elif name == "run_operation":
                        challenges.note_operation(
                            call[
                                "function"
                            ].get(
                                "arguments",
                                {}
                            ).get(
                                "operation"
                            ),
                            result
                        )

            print()
            print("TOOL RESULT:")
            print(
                result[:16000]
            )

            messages.append(
                {
                    "role":
                        "tool",

                    "tool_name":
                        name,

                    "content":
                        result
                }
            )

            hint = (
                challenges.stall_hint()
                if challenges is not None
                and outcome is None
                else None
            )

            if hint:
                print()
                print(hint)

                messages.append(
                    {
                        "role": "user",
                        "content": hint
                    }
                )

                append_history(
                    config,
                    "contract_challenge_hint",
                    {
                        "step": step
                    }
                )

            if outcome is not None:
                # A confirmed contract challenge interrupts the loop
                # immediately. The reasons are preserved in
                # cross-attempt memory so a regenerated contract cannot
                # reproduce the same defect.
                record_spec_failure(
                    config,
                    "contract/challenge_confirmed",
                    challenge_memory_entry(
                        outcome["challenge"]
                    )
                )

                record_spec_failure(
                    config,
                    "contract/challenge_review",
                    outcome["verdict"].get(
                        "reasons"
                    )
                    or []
                )

                append_history(
                    config,
                    "contract_challenge_confirmed",
                    {
                        "steps": step,
                        "challenge":
                            outcome["challenge"],
                        "reasons":
                            outcome["verdict"].get(
                                "reasons"
                            )
                    }
                )

                state[
                    "contract_challenge"
                ] = outcome["challenge"]

                save_state(
                    config,
                    state
                )

                outcome["steps"] = step

                return outcome

    print(
        "Agentic implementation reached "
        "maximum steps."
    )

    return failed_outcome(
        "Agentic implementation reached maximum steps.",
        steps=max_steps
    )
