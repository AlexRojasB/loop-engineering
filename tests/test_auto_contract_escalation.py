"""
Regression coverage for problem 3 of Ledger Full #3: the harness must be
able to raise a contract challenge on deterministic evidence, instead of
depending on the implementation model choosing to call the tool.

In Spec 003 the 4B implementation model diagnosed the broken frozen
contract correctly and repeatedly --

    "The build is failing on the tests, not on production code."
    "This appears to be a bug in the test file, but I'm not supposed to
     modify tests."

-- and never once called `report_contract_issue`. It looped until it
gave up, 1117 seconds later.

The escalation here is deliberately narrow. It fires only on evidence a
machine can check: production compiles, the frozen test file does not,
every diagnostic is inside files the agent may not edit, and the same
diagnostics reproduce across repair rounds. Filing it grants nothing --
it enters the same independent adjudication as a model-filed report, and
two reviewers must still confirm.
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import contract_challenge  # noqa: E402
from core.contract_challenge import (  # noqa: E402
    FROZEN_TEST_COMPILATION,
    attribute_diagnostics,
    frozen_test_compilation_evidence,
)
from core.phases import agentic_implementation_phase as impl  # noqa: E402
from core.spec_memory import (  # noqa: E402
    SpecFailureMemory,
    spec_scope_key,
)
from core.state import read_history  # noqa: E402
from languages.dotnet import DotNetAdapter  # noqa: E402


TASK = (
    "# Entries\n\n1. Add `Entries` exposing recorded entries with "
    "`Code` and `Quantity`.\n"
)

FROZEN_TESTS = {
    "RegistryTests.cs": """
using Xunit;

public class RegistryTests
{
    [Fact]
    public void Entries_RecordsCodeAndQuantity()
    {
        var registry = new Registry();
        Assert.Collection(registry.Entries(),
            e => Assert.Equal("W-1", e.Code) && Assert.Equal(10, e.Quantity));
    }
}
"""
}

PRODUCTION = "public class Registry { }\n"

FROZEN_ONLY_FAILURE = (
    "exit_code=1\n"
    "  Registry -> /repo/bin/Registry.dll\n"
    "/repo/RegistryTests.cs(10,13): error CS0019: Operator '&&' "
    "cannot be applied to operands of type 'void' and 'void' "
    "[/repo/RegistryTests.csproj]\n"
    "/repo/RegistryTests.cs(10,13): error CS0201: Only assignment, "
    "call, increment, decrement, await, and new object expressions "
    "can be used as a statement [/repo/RegistryTests.csproj]\n"
)

PRODUCTION_ALSO_BROKEN = (
    FROZEN_ONLY_FAILURE
    + "/repo/Registry.cs(3,5): error CS1002: ; expected "
    "[/repo/Registry.csproj]\n"
)

ORDINARY_ASSERTION_FAILURE = (
    "exit_code=1\n"
    "  Failed RegistryTests.Entries_RecordsCodeAndQuantity\n"
    "  Assert.Equal() Failure: Values differ\n"
    "  Expected: 10\n"
    "  Actual:   0\n"
)


def confirm():
    return {
        "ok": True,
        "response": json.dumps(
            {
                "decision": "CONFIRM",
                "reasons": ["The frozen test file cannot compile."]
            }
        ),
        "thinking": None,
        "done_reason": "stop",
        "truncated": False,
        "error": None
    }


def reject():
    return {
        "ok": True,
        "response": json.dumps(
            {
                "decision": "REJECT",
                "reasons": ["Not a contract defect."]
            }
        ),
        "thinking": None,
        "done_reason": "stop",
        "truncated": False,
        "error": None
    }


class ScriptedReviewer:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    def __call__(self, config, model, prompt, **kwargs):
        self.calls.append(prompt)

        if len(self.verdicts) > 1:
            return self.verdicts.pop(0)

        return self.verdicts[0]


class BuildOnlyAdapter(DotNetAdapter):
    """Real diagnostic parsing, no toolchain."""

    def build_argv(self, workspace_files):
        return ["fake-build"]

    def test_argv(self, workspace_files, filter=None):
        return ["fake-test"]


def runner(output, exit_code=1):
    def run(workspace, argv, timeout=None):
        run.calls.append(argv)
        return {"exit_code": exit_code, "output": output}

    run.calls = []
    return run


class DiagnosticAttributionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DotNetAdapter()

    def test_frozen_only_failure_is_attributed_to_the_contract(self):
        frozen, other, located = attribute_diagnostics(
            self.adapter,
            FROZEN_ONLY_FAILURE,
            FROZEN_TESTS
        )

        self.assertTrue(located)
        self.assertEqual(len(frozen), 2)
        self.assertEqual(other, [])

    def test_production_failure_is_kept_separate(self):
        frozen, other, _ = attribute_diagnostics(
            self.adapter,
            PRODUCTION_ALSO_BROKEN,
            FROZEN_TESTS
        )

        self.assertTrue(frozen)
        self.assertEqual(len(other), 1)
        self.assertIn("Registry.cs", other[0]["path"])

    def test_assertion_failure_carries_no_locations(self):
        _, _, located = attribute_diagnostics(
            self.adapter,
            ORDINARY_ASSERTION_FAILURE,
            FROZEN_TESTS
        )

        self.assertEqual(located, [])

    def test_adapter_without_the_hook_attributes_nothing(self):
        class Bare:
            pass

        self.assertEqual(
            attribute_diagnostics(
                Bare(),
                FROZEN_ONLY_FAILURE,
                FROZEN_TESTS
            ),
            ([], [], [])
        )


class FrozenCompilationEvidenceTests(unittest.TestCase):
    def _evidence(self, output, exit_code=1):
        return frozen_test_compilation_evidence(
            "/repo",
            {
                "kind": FROZEN_TEST_COMPILATION,
                "failing_tests": ["Entries_RecordsCodeAndQuantity"],
                "diagnostics": ["CS0019"]
            },
            FROZEN_TESTS,
            BuildOnlyAdapter(),
            [],
            runner=runner(output, exit_code)
        )

    def test_frozen_only_compile_failure_is_admissible(self):
        gate = self._evidence(FROZEN_ONLY_FAILURE)

        self.assertTrue(gate["ok"])
        self.assertEqual(len(gate["frozen_diagnostics"]), 2)

    def test_a_building_project_is_not_admissible(self):
        gate = self._evidence("exit_code=0\nBuild succeeded.", 0)

        self.assertFalse(gate["ok"])
        self.assertIn("now builds", gate["reason"])

    def test_production_errors_block_the_claim(self):
        gate = self._evidence(PRODUCTION_ALSO_BROKEN)

        self.assertFalse(gate["ok"])
        self.assertIn(
            "allowed to change",
            gate["reason"]
        )

    def test_unattributable_failure_is_not_admissible(self):
        gate = self._evidence(ORDINARY_ASSERTION_FAILURE)

        self.assertFalse(gate["ok"])
        self.assertIn(
            "could not be attributed",
            gate["reason"]
        )


class EscalationTriggerTests(unittest.TestCase):
    def _controller(self, **overrides):
        settings = {
            "auto_after_repeats": 3,
            "max_auto_escalations": 1,
        }
        settings.update(overrides)

        controller = impl.ChallengeController(
            {"semantic_reviewer_model": "mock"},
            "/repo",
            TASK,
            FROZEN_TESTS,
            Path("/repo"),
            [{"path": "Registry.cs"}],
            BuildOnlyAdapter(),
            [],
            4,
            2,
            **settings
        )

        controller.wrote_production = True

        return controller

    def _observe(self, controller, output, times):
        for _ in range(times):
            controller.note_operation("build", output)

    def test_no_escalation_before_the_repeat_threshold(self):
        controller = self._controller()

        self._observe(controller, FROZEN_ONLY_FAILURE, 2)

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_escalation_after_repeated_identical_failures(self):
        controller = self._controller()

        self._observe(controller, FROZEN_ONLY_FAILURE, 3)

        args = controller.auto_escalation()

        self.assertIsNotNone(args)
        self.assertEqual(
            args["kind"],
            FROZEN_TEST_COMPILATION
        )
        self.assertIn(
            "Entries_RecordsCodeAndQuantity",
            args["failing_tests"]
        )
        self.assertTrue(args["diagnostics"])

    def test_ordinary_test_failure_never_escalates(self):
        """
        The requirement is explicit: do NOT escalate merely because
        tests fail.
        """

        controller = self._controller()

        self._observe(
            controller,
            ORDINARY_ASSERTION_FAILURE,
            10
        )

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_production_compile_errors_never_escalate(self):
        controller = self._controller()

        self._observe(
            controller,
            PRODUCTION_ALSO_BROKEN,
            10
        )

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_a_passing_run_resets_the_evidence(self):
        controller = self._controller()

        self._observe(controller, FROZEN_ONLY_FAILURE, 2)

        controller.note_operation(
            "build",
            "exit_code=0\nBuild succeeded."
        )

        self._observe(controller, FROZEN_ONLY_FAILURE, 2)

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_untouched_production_never_escalates(self):
        controller = self._controller()
        controller.wrote_production = False

        self._observe(controller, FROZEN_ONLY_FAILURE, 5)

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_escalation_is_bounded(self):
        controller = self._controller()

        self._observe(controller, FROZEN_ONLY_FAILURE, 3)

        self.assertIsNotNone(
            controller.auto_escalation()
        )

        controller.auto_escalations = 1

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_exhausted_review_budget_blocks_escalation(self):
        controller = self._controller()
        controller.reviews = 2

        self._observe(controller, FROZEN_ONLY_FAILURE, 3)

        self.assertIsNone(
            controller.auto_escalation()
        )

    def test_no_determinable_test_names_blocks_escalation(self):
        controller = impl.ChallengeController(
            {"semantic_reviewer_model": "mock"},
            "/repo",
            TASK,
            {"RegistryTests.cs": "// no tests declared here\n"},
            Path("/repo"),
            [{"path": "Registry.cs"}],
            BuildOnlyAdapter(),
            [],
            4,
            2,
            auto_after_repeats=3
        )
        controller.wrote_production = True

        self._observe(controller, FROZEN_ONLY_FAILURE, 3)

        self.assertIsNone(
            controller.auto_escalation()
        )


class EscalationAdjudicationTests(unittest.TestCase):
    """
    A harness-raised report is still only a report.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.history = (
            Path(self._tmp.name) / "history.jsonl"
        )

        self.config = {
            "semantic_reviewer_model": "mock-reviewer",
            "state_file": str(
                Path(self._tmp.name) / "state.json"
            ),
            "history_file": str(self.history),
        }

    def _controller(self, memory=None):
        config = dict(self.config)

        if memory is not None:
            config["spec_memory"] = memory

        controller = impl.ChallengeController(
            config,
            self._tmp.name,
            TASK,
            FROZEN_TESTS,
            Path(self._tmp.name),
            [{"path": "Registry.cs"}],
            BuildOnlyAdapter(),
            [],
            4,
            2,
            runner=runner(FROZEN_ONLY_FAILURE),
            auto_after_repeats=3
        )

        controller.wrote_production = True

        for _ in range(3):
            controller.note_operation(
                "build",
                FROZEN_ONLY_FAILURE
            )

        return controller

    def _escalate(self, verdicts):
        controller = self._controller()

        reviewer = ScriptedReviewer(verdicts)

        with mock.patch.object(
            contract_challenge,
            "call_model",
            reviewer
        ):
            message, outcome = controller.escalate()

        return message, outcome, reviewer, controller

    def test_confirmed_escalation_stops_implementation(self):
        message, outcome, reviewer, _ = self._escalate(
            [confirm(), confirm()]
        )

        self.assertIsNotNone(outcome)
        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )
        self.assertEqual(len(reviewer.calls), 2)

    def test_independent_confirmation_is_still_required(self):
        message, outcome, reviewer, _ = self._escalate(
            [confirm(), reject()]
        )

        self.assertIsNone(
            outcome,
            "one confirming reviewer was enough to stop the run"
        )
        self.assertEqual(len(reviewer.calls), 2)

    def test_a_rejecting_reviewer_leaves_the_contract_standing(self):
        message, outcome, reviewer, _ = self._escalate(
            [reject()]
        )

        self.assertIsNone(outcome)
        self.assertIn("REJECTED", message)

    def test_escalation_consumes_the_shared_budgets(self):
        _, _, _, controller = self._escalate(
            [confirm(), confirm()]
        )

        self.assertEqual(controller.submissions, 1)
        self.assertEqual(controller.reviews, 1)
        self.assertEqual(controller.auto_escalations, 1)

    def test_escalation_is_recorded_in_history(self):
        self._escalate([confirm(), confirm()])

        events = [
            event["event"]
            for event in read_history(self.config)
        ]

        self.assertIn(
            "contract_challenge_auto_escalated",
            events
        )

    def test_rejected_escalation_does_not_immediately_refire(self):
        controller = self._controller()

        reviewer = ScriptedReviewer([reject()])

        with mock.patch.object(
            contract_challenge,
            "call_model",
            reviewer
        ):
            controller.escalate()

            message, outcome = controller.escalate()

        self.assertIsNone(outcome)
        self.assertIsNone(message)
        self.assertEqual(len(reviewer.calls), 1)


class EscalationInsideThePhaseTests(unittest.TestCase):
    """
    The Spec 003 shape end to end: a model that never files a report,
    and a harness that files one for it.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name

        (
            Path(self.workspace) / "Registry.cs"
        ).write_text(PRODUCTION)

        self.config = {
            "semantic_reviewer_model": "mock-reviewer",
            "agentic_max_steps": 8,
            "auto_escalation_after_repeats": 2,
            "state_file": str(
                Path(self.workspace).parent / "esc-state.json"
            ),
            "history_file": str(
                Path(self.workspace).parent / "esc-history.jsonl"
            ),
        }

        for key in ("state_file", "history_file"):
            self.addCleanup(
                lambda k=key: Path(
                    self.config[k]
                ).unlink(missing_ok=True)
            )

    def _run(self, verdicts, memory=None):
        config = dict(self.config)

        if memory is not None:
            config["spec_memory"] = memory

        script = [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": {
                                    "path": "Registry.cs",
                                    "content": PRODUCTION
                                }
                            }
                        }
                    ]
                }
            }
        ]

        # The model does exactly what the real one did: run the tests,
        # narrate, and never file a report.
        for _ in range(4):
            script.append(
                {
                    "message": {
                        "role": "assistant",
                        "content":
                            "This looks like a bug in the test file, "
                            "but I cannot modify tests.",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "run_operation",
                                    "arguments": {
                                        "operation": "build"
                                    }
                                }
                            }
                        ]
                    }
                }
            )

        def call_model(model, url, ctx, messages, tools=None):
            if script:
                return script.pop(0)

            return {"message": {"role": "assistant", "content": "done"}}

        reviewer = ScriptedReviewer(verdicts)

        with mock.patch.object(
            impl,
            "_call_model",
            call_model
        ), mock.patch.object(
            impl,
            "_run_argv",
            lambda root, argv: FROZEN_ONLY_FAILURE
        ), mock.patch.object(
            contract_challenge,
            "call_model",
            reviewer
        ):
            outcome = impl.run_agentic_implementation_phase(
                config,
                self.workspace,
                TASK,
                {},
                [
                    {
                        "path": "Registry.cs",
                        "type": "implementation",
                        "reasons": ["registry"]
                    }
                ],
                "build",
                "test",
                BuildOnlyAdapter(),
                [],
                None,
                frozen_tests=FROZEN_TESTS,
                challenge_runner=runner(FROZEN_ONLY_FAILURE)
            )

        return outcome, reviewer, config

    def test_harness_escalates_when_the_model_never_does(self):
        outcome, reviewer, config = self._run(
            [confirm(), confirm()]
        )

        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )

        self.assertEqual(
            outcome["challenge"]["kind"],
            FROZEN_TEST_COMPILATION
        )

        events = [
            event["event"]
            for event in read_history(config)
        ]

        self.assertIn(
            "contract_challenge_auto_escalated",
            events
        )

    def test_rejected_escalation_lets_the_phase_fail_normally(self):
        outcome, _, _ = self._run([reject()])

        self.assertEqual(
            outcome["status"],
            impl.FAILED
        )

    def test_confirmed_escalation_reaches_cross_attempt_memory(self):
        memory = SpecFailureMemory(
            scope=spec_scope_key("work/entries.md", TASK)
        )

        outcome, _, _ = self._run(
            [confirm(), confirm()],
            memory=memory
        )

        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )

        joined = "\n".join(memory.lines())

        self.assertIn(
            "contract/challenge_confirmed",
            joined
        )


if __name__ == "__main__":
    unittest.main()
