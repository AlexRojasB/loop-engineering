"""
Regression coverage for problem 1 of Ledger Full #3: deterministic
authorization of future API must reach the model reviewers.

Spec 005 required "extend successful transfers with an optional
description". The deterministic gate compiled each candidate contract and
reported:

    CONTRACT COMPILATION CHECK: expected-red diagnostics only
    (2 requested symbol(s) missing)

...and then that verdict was discarded. Reviewers, seeing only the prose
task and today's production code, rejected the same contract five
attempts in a row for reasons that were all restatements of "it does not
exist yet":

    "the production LedgerService.Transfer method does NOT have this
     parameter"
    "the Transaction class in production code has no Description
     property"

Both statements were true; neither was a defect. The spec exhausted all
five attempts and ended the run at 4/8.

These tests pin the propagation, not a model's reaction to it: the gate's
classification is captured, rendered as structured evidence, and reaches
the structural reviewer, the semantic reviewer and the revision prompt --
while an invented API still gets no protection.

Fixtures are a generic Widget/Registry domain; no Ledger, Transfer or
Description names appear except inside the provenance sample.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.authorized_future import (  # noqa: E402
    NO_AUTHORIZED_FUTURE,
    authorized_future_entries,
    authorized_future_text,
    format_authorized_future,
)
from core.phases import test_contract_phase  # noqa: E402
from languages.dotnet import DotNetAdapter  # noqa: E402

from tests.test_test_contract_phase import (  # noqa: E402
    ScriptedCallModel,
    approve,
    coder_returns,
)


# ---------------------------------------------------------------------
# Generic fixtures
# ---------------------------------------------------------------------

TASK = """
# Annotate Registrations

## Requirements

1. Extend `Register` with an optional note argument.
2. Expose the stored value as a `Note` member on `Registration`.
3. Trim leading and trailing whitespace from the note.
4. Store nothing when the note is empty or whitespace only.
5. Preserve all existing behavior.
"""

PRODUCTION = """
public class Registration
{
    public string Code { get; }
    public int Quantity { get; }
}

public class Registry
{
    public bool Register(string code, int quantity) => true;
}
"""

ORIGINAL_TESTS = """
using Xunit;

public class RegistryTests
{
    [Fact]
    public void Register_WithValidCode_ReturnsTrue()
    {
        var registry = new Registry();
        Assert.True(registry.Register("W-1", 10));
    }
}
"""

SNIPPET = """
[Fact]
public void Register_WithNote_TrimsTheNote()
{
    var registry = new Registry();
    Assert.True(registry.Register("W-1", 10, "  hello  "));
    Assert.Equal("hello", registry.Find("W-1").Note);
}
"""

# What the .NET adapter really emits for a future overload plus a future
# property. Both symbols are named by the task above.
FUTURE_OVERLOAD_DIAGNOSTIC = (
    "/repo/RegistryTests.cs(12,30): error CS1501: No overload for "
    "method 'Register' takes 3 arguments"
)

FUTURE_PROPERTY_DIAGNOSTIC = (
    "/repo/RegistryTests.cs(13,52): error CS1061: 'Registration' does "
    "not contain a definition for 'Note' and no accessible extension "
    "method 'Note' accepting a first argument of type 'Registration' "
    "could be found"
)

INVENTED_DIAGNOSTIC = (
    "/repo/RegistryTests.cs(20,30): error CS1061: 'Registry' does not "
    "contain a definition for 'PurgeArchive' and no accessible "
    "extension method 'PurgeArchive' accepting a first argument of "
    "type 'Registry' could be found"
)


def runner_returning(output, exit_code=1):
    def run(workspace, argv, timeout=None):
        run.calls.append(argv)
        return {"exit_code": exit_code, "output": output}

    run.calls = []
    return run


class AdapterClassificationTests(unittest.TestCase):
    """
    The deterministic half: what the adapter decides, before any model
    is involved.
    """

    def setUp(self):
        self.adapter = DotNetAdapter()

    def _classify(self, output):
        return self.adapter.classify_contract_diagnostics(
            output,
            TASK
        )

    def test_future_method_overload_is_authorized_not_rejected(self):
        report = self._classify(
            FUTURE_OVERLOAD_DIAGNOSTIC
        )

        self.assertEqual(report["verdict"], "VALID")

        self.assertEqual(
            [
                entry["symbol"]
                for entry in report["expected_red"]
            ],
            ["Register"]
        )

        self.assertEqual(report["invalid"], [])

    def test_future_property_is_authorized_not_rejected(self):
        report = self._classify(
            FUTURE_PROPERTY_DIAGNOSTIC
        )

        self.assertEqual(report["verdict"], "VALID")

        self.assertIn(
            "Note",
            [
                entry["symbol"]
                for entry in report["expected_red"]
            ]
        )

    def test_symbol_the_task_never_names_is_not_authorized(self):
        """
        Authorization is earned from the specification text, not
        granted by being absent. `PurgeArchive` appears nowhere in the
        task, so its absence is an invented API, not expected red.
        """

        from core.symbols import spec_requests_symbol

        self.assertTrue(
            spec_requests_symbol("Register", TASK)
        )
        self.assertTrue(
            spec_requests_symbol("Note", TASK)
        )
        self.assertFalse(
            spec_requests_symbol("PurgeArchive", TASK)
        )

    def test_invented_api_is_still_rejected(self):
        report = self._classify(
            INVENTED_DIAGNOSTIC
        )

        self.assertEqual(report["verdict"], "INVALID")

        self.assertEqual(report["expected_red"], [])

        self.assertTrue(
            any(
                "PurgeArchive" in issue
                for issue in report["issues"]
            )
        )

    def test_authorized_and_invented_together_still_reject(self):
        """
        One invented symbol poisons the contract even when other
        symbols are properly authorized.
        """

        report = self._classify(
            FUTURE_PROPERTY_DIAGNOSTIC
            + "\n"
            + INVENTED_DIAGNOSTIC
        )

        self.assertEqual(report["verdict"], "INVALID")


class AuthorizedFutureRenderingTests(unittest.TestCase):
    def _report(self):
        return DotNetAdapter().classify_contract_diagnostics(
            FUTURE_OVERLOAD_DIAGNOSTIC
            + "\n"
            + FUTURE_PROPERTY_DIAGNOSTIC,
            TASK
        )

    def test_entries_carry_symbol_authority_and_evidence(self):
        entries = authorized_future_entries(
            self._report()
        )

        self.assertEqual(
            sorted(
                entry["symbol"]
                for entry in entries
            ),
            ["Note", "Register"]
        )

        for entry in entries:
            self.assertTrue(entry["authority"])
            self.assertIn("CS", entry["evidence"])

    def test_rendering_names_every_authorized_symbol(self):
        text = authorized_future_text(
            self._report()
        )

        self.assertIn("`Register`", text)
        self.assertIn("`Note`", text)
        self.assertIn("CS1501", text)

    def test_absent_or_unclassifiable_report_renders_placeholder(self):
        for report in (None, {}, {"expected_red": []}, "nonsense"):
            self.assertEqual(
                authorized_future_text(report),
                NO_AUTHORIZED_FUTURE
            )

    def test_rendering_is_bounded(self):
        report = {
            "expected_red": [
                {
                    "code": "CS1061",
                    "symbol": f"Symbol{index}",
                    "message": "m" * 5000,
                    "reason": "r" * 5000
                }
                for index in range(200)
            ]
        }

        entries = authorized_future_entries(report)

        self.assertLessEqual(len(entries), 12)

        for entry in entries:
            self.assertLess(len(entry["authority"]), 260)
            self.assertLess(len(entry["evidence"]), 260)


class ReviewerPromptPropagationTests(unittest.TestCase):
    """
    End-to-end through the real Test Contract phase: what the gate
    decided must appear in what the reviewers are asked.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name

        self._write("Registry.cs", PRODUCTION)
        self._write("RegistryTests.cs", ORIGINAL_TESTS)

    def _write(self, relative, content):
        (Path(self.workspace) / relative).write_text(content)

    def _config(self, **overrides):
        config = {
            "coder_model": "mock-coder",
            "test_reviewer_model": "mock-structural",
            "semantic_reviewer_model": "mock-semantic",
            "max_test_generation_attempts": 1,
            "state_file":
                str(Path(self.workspace).parent / "af-state.json"),
            "history_file":
                str(Path(self.workspace).parent / "af-history.jsonl"),
        }
        config.update(overrides)

        self.addCleanup(
            lambda: Path(config["state_file"]).unlink(missing_ok=True)
        )
        self.addCleanup(
            lambda: Path(
                config["history_file"]
            ).unlink(missing_ok=True)
        )

        return config

    def _run(self, build_output):
        scripted = ScriptedCallModel(
            {
                "mock-coder": coder_returns(SNIPPET),
                "mock-structural": approve(),
                "mock-semantic": approve(),
            }
        )

        config = self._config(
            contract_build_runner=runner_returning(
                build_output
            )
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
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
                [
                    {
                        "path": "RegistryTests.cs",
                        "type": "test",
                        "reasons": ["tests"]
                    }
                ],
                DotNetAdapter(),
                [
                    "Registry.csproj",
                    "Registry.cs",
                    "RegistryTests.cs",
                ]
            )

        return result, scripted

    def test_gate_classification_reaches_both_reviewers(self):
        result, scripted = self._run(
            FUTURE_OVERLOAD_DIAGNOSTIC
            + "\n"
            + FUTURE_PROPERTY_DIAGNOSTIC
        )

        self.assertIsNotNone(result)

        for model in ("mock-structural", "mock-semantic"):
            calls = scripted.calls_for(model)

            self.assertTrue(calls, model)

            prompt = calls[0]["prompt"]

            self.assertIn(
                "Deterministically Authorized Future API",
                prompt
            )

            self.assertIn("`Register`", prompt)
            self.assertIn("`Note`", prompt)

            self.assertIn(
                "not yours to",
                prompt
            )

    def test_no_authorized_symbols_renders_the_placeholder(self):
        result, scripted = self._run(
            ""
        )

        self.assertIsNotNone(result)

        prompt = scripted.calls_for(
            "mock-structural"
        )[0]["prompt"]

        self.assertIn(
            NO_AUTHORIZED_FUTURE,
            prompt
        )

    def test_revision_prompt_also_carries_the_authorization(self):
        """
        The reviser must not "fix" a valid future reference away.
        """

        scripted = ScriptedCallModel(
            {
                "mock-coder": coder_returns(SNIPPET),
                "mock-structural": approve(),
                "mock-semantic": approve(),
            }
        )

        prompt = test_contract_phase.test_snippet_revision_prompt(
            TASK,
            {"Registry.cs": PRODUCTION},
            ORIGINAL_TESTS,
            SNIPPET,
            ["some issue"],
            authorized_future=format_authorized_future(
                authorized_future_entries(
                    DotNetAdapter().classify_contract_diagnostics(
                        FUTURE_PROPERTY_DIAGNOSTIC,
                        TASK
                    )
                )
            )
        )

        self.assertIn("`Note`", prompt)
        self.assertIn(
            "Deterministically Authorized Future API",
            prompt
        )


if __name__ == "__main__":
    unittest.main()
