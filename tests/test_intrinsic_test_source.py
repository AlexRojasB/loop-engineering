"""
Regression coverage for problem 2 of Ledger Full #3: a test contract with
compiler errors intrinsic to the TEST CODE must never be frozen.

Spec 003 froze this:

    Assert.Collection(transactionHistory,
        t1 => Assert.Equal("Deposit", t1.Type) && Assert.Equal(50m, t1.Amount),
        t2 => Assert.Equal("Withdraw", t2.Type) && Assert.Equal(20m, t2.Amount)
    );

`Assert.Equal` returns void, so `&&` is invalid C#. It still passed the
deterministic compilation gate, Expected RED, and both model reviewers,
then destroyed 1117 seconds of agentic implementation before the attempt
failed.

The reason it passed is NOT a gap in the diagnostic classification --
CS0019 and CS0201 were already classified as contract misuse. The reason
is that at gate time THOSE DIAGNOSTICS DO NOT EXIST. Verified against the
real .NET SDK:

    with GetTransactionHistory() absent:
        only CS1061 for the missing method
    after adding GetTransactionHistory():
        CS0019 + CS0201 on both lambda bodies

Roslyn stops binding an expression whose sub-expression has an error
type, so the defect is masked by the very future-API absence that makes
this a test-first contract. Every mainstream compiler does this. No
diagnostic-classification rule can catch it, so the check has to be a
compiler-free analysis of the test source itself.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.contract_validation import (  # noqa: E402
    analyze_candidate_test_source,
)
from core.phases import test_contract_phase  # noqa: E402
from core.state import read_history  # noqa: E402
from languages.base import LanguageAdapter  # noqa: E402
from languages.dotnet import DotNetAdapter  # noqa: E402

from tests.test_test_contract_phase import (  # noqa: E402
    ScriptedCallModel,
    approve,
    coder_returns,
)


# The Ledger Spec 003 sample, kept verbatim for provenance.
LEDGER_BROKEN_SAMPLE = """
    [Fact]
    public void GetTransactionHistory_ReturnsTransactionInOrder()
    {
        var service = new LedgerService();
        service.CreateAccount("Checking", 100m);
        service.Deposit("Checking", 50m);
        service.Withdraw("Checking", 20m);
        var transactionHistory = service.GetTransactionHistory();
        Assert.Collection(transactionHistory,
            t1 => Assert.Equal("Deposit", t1.Type) && Assert.Equal(50m, t1.Amount),
            t2 => Assert.Equal("Withdraw", t2.Type) && Assert.Equal(20m, t2.Amount)
        );
    }
"""

# The same defect in a generic domain, which is what the harness must
# actually generalize to.
GENERIC_BROKEN = """
using Xunit;

public class RegistryTests
{
    [Fact]
    public void Register_RecordsEntry()
    {
        var registry = new Registry();
        Assert.Collection(registry.Entries(),
            e => Assert.Equal("W-1", e.Code) && Assert.Equal(10, e.Quantity));
    }
}
"""

GENERIC_CORRECT = """
using Xunit;

public class RegistryTests
{
    [Fact]
    public void Register_RecordsEntry()
    {
        var registry = new Registry();
        Assert.Collection(registry.Entries(),
            e =>
            {
                Assert.Equal("W-1", e.Code);
                Assert.Equal(10, e.Quantity);
            });
    }
}
"""


class VoidAssertionOperatorTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DotNetAdapter()

    def _defects(self, source):
        return self.adapter.analyze_test_source(source)

    def test_ledger_sample_is_detected(self):
        defects = self._defects(
            LEDGER_BROKEN_SAMPLE
        )

        self.assertEqual(len(defects), 2)

        for defect in defects:
            self.assertEqual(defect["code"], "TEST0001")
            self.assertIn("returns void", defect["message"])
            self.assertIn("'&&'", defect["message"])

    def test_generic_domain_sample_is_detected(self):
        self.assertTrue(
            self._defects(GENERIC_BROKEN)
        )

    def test_correct_braced_lambda_is_accepted(self):
        self.assertEqual(
            self._defects(GENERIC_CORRECT),
            []
        )

    def test_or_operator_is_detected_too(self):
        self.assertTrue(
            self._defects(
                "e => Assert.True(e.Ok) || Assert.False(e.Bad);"
            )
        )

    def test_assertion_on_the_right_of_the_operator_is_detected(self):
        self.assertTrue(
            self._defects(
                "var ok = e.Ready && Assert.True(e.Ok);"
            )
        )

    # -- precision: none of these may be flagged ----------------------

    def test_boolean_operator_inside_the_assertion_is_fine(self):
        self.assertEqual(
            self._defects(
                "Assert.True(first && second);"
            ),
            []
        )

    def test_operator_inside_a_string_literal_is_fine(self):
        self.assertEqual(
            self._defects(
                'Assert.Equal("a && b", value);'
            ),
            []
        )

    def test_operator_inside_a_verbatim_string_is_fine(self):
        self.assertEqual(
            self._defects(
                'Assert.Equal(@"a && b", value);'
            ),
            []
        )

    def test_commented_out_defect_is_fine(self):
        self.assertEqual(
            self._defects(
                "// Assert.Equal(1, x) && Assert.Equal(2, y);\n"
                "/* Assert.True(a) && Assert.True(b); */"
            ),
            []
        )

    def test_value_returning_assertion_helpers_are_not_flagged(self):
        """
        xUnit's IsType/Throws/Single return a value, so combining them
        with a boolean operator is legitimate. Flagging them would
        reject valid contracts.
        """

        for source in (
            "var ok = Assert.IsType<Widget>(o) != null && ready;",
            "var ex = Assert.Throws<Exception>(act) != null && ready;",
            "var one = Assert.Single(items) != null && ready;",
        ):
            self.assertEqual(
                self._defects(source),
                [],
                source
            )

    def test_ordinary_sequential_assertions_are_fine(self):
        self.assertEqual(
            self._defects(
                "Assert.Equal(1, a);\nAssert.Equal(2, b);\n"
            ),
            []
        )

    def test_empty_source_is_fine(self):
        self.assertEqual(self._defects(""), [])
        self.assertEqual(self._defects(None), [])


class SourceGateTests(unittest.TestCase):
    def test_gate_rejects_and_explains(self):
        ok, issues = analyze_candidate_test_source(
            DotNetAdapter(),
            LEDGER_BROKEN_SAMPLE,
            "Tests.cs"
        )

        self.assertFalse(ok)
        self.assertTrue(issues)
        self.assertIn(
            "own statement",
            " ".join(issues)
        )

    def test_gate_fails_open_without_adapter_support(self):
        class Bare(LanguageAdapter):
            def can_handle(self, files):
                return True

            def build_command(self, files):
                return "build"

            def test_command(self, files):
                return "test"

        ok, issues = analyze_candidate_test_source(
            Bare(),
            LEDGER_BROKEN_SAMPLE,
            "Tests.cs"
        )

        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_gate_fails_open_when_the_hook_raises(self):
        class Exploding:
            def analyze_test_source(self, source, path=None):
                raise RuntimeError("boom")

        ok, _ = analyze_candidate_test_source(
            Exploding(),
            LEDGER_BROKEN_SAMPLE,
            "Tests.cs"
        )

        self.assertTrue(ok)


class IntrinsicDefectCannotBeExpectedRedTests(unittest.TestCase):
    """
    Once the masking future symbol exists, the SAME file produces
    CS0019/CS0201. Those must never be read as expected red.
    """

    REAL_DIAGNOSTICS = (
        "/repo/Tests.cs(7,13): error CS0019: Operator '&&' cannot be "
        "applied to operands of type 'void' and 'void'\n"
        "/repo/Tests.cs(7,13): error CS0201: Only assignment, call, "
        "increment, decrement, await, and new object expressions can "
        "be used as a statement"
    )

    TASK = (
        "# History\n\n1. Add `GetTransactionHistory`.\n"
        "2. Record the transaction `Type` and `Amount`.\n"
    )

    def test_intrinsic_type_errors_are_classified_invalid(self):
        report = DotNetAdapter().classify_contract_diagnostics(
            self.REAL_DIAGNOSTICS,
            self.TASK
        )

        self.assertEqual(report["verdict"], "INVALID")
        self.assertEqual(report["expected_red"], [])
        self.assertTrue(report["invalid"])

    def test_still_invalid_even_when_mixed_with_authorized_red(self):
        report = DotNetAdapter().classify_contract_diagnostics(
            "/repo/Tests.cs(5,23): error CS1061: 'Svc' does not "
            "contain a definition for 'GetTransactionHistory'\n"
            + self.REAL_DIAGNOSTICS,
            self.TASK
        )

        self.assertEqual(report["verdict"], "INVALID")


class ContractPhaseRejectsBrokenSourceTests(unittest.TestCase):
    """
    The whole point: this must be caught BEFORE freeze, without a
    toolchain and without a model reviewer being consulted.
    """

    PRODUCTION = "public class Registry { }\n"

    ORIGINAL = (
        "using Xunit;\n\n"
        "public class RegistryTests\n"
        "{\n"
        "    [Fact]\n"
        "    public void Existing() { Assert.True(true); }\n"
        "}\n"
    )

    BROKEN_SNIPPET = """
[Fact]
public void Register_RecordsEntry()
{
    var registry = new Registry();
    Assert.Collection(registry.Entries(),
        e => Assert.Equal("W-1", e.Code) && Assert.Equal(10, e.Quantity));
}
"""

    TASK = (
        "# Entries\n\n1. Add `Entries` returning the recorded "
        "entries with `Code` and `Quantity`.\n"
    )

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name

        (
            Path(self.workspace) / "Registry.cs"
        ).write_text(self.PRODUCTION)

        (
            Path(self.workspace) / "RegistryTests.cs"
        ).write_text(self.ORIGINAL)

        self.state_file = (
            Path(self.workspace).parent / "src-state.json"
        )
        self.history_file = (
            Path(self.workspace).parent / "src-history.jsonl"
        )

        self.addCleanup(
            lambda: self.state_file.unlink(missing_ok=True)
        )
        self.addCleanup(
            lambda: self.history_file.unlink(missing_ok=True)
        )

    def _run(self, snippet, **overrides):
        scripted = ScriptedCallModel(
            {
                "mock-coder": coder_returns(snippet),
                "mock-structural": approve(),
                "mock-semantic": approve(),
            }
        )

        config = {
            "coder_model": "mock-coder",
            "test_reviewer_model": "mock-structural",
            "semantic_reviewer_model": "mock-semantic",
            "max_test_generation_attempts": 2,
            "state_file": str(self.state_file),
            "history_file": str(self.history_file),
            # No toolchain at all: the source check must still fire.
            "contract_compilation_check": False,
        }
        config.update(overrides)

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                self.TASK,
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
                ["Registry.csproj", "Registry.cs", "RegistryTests.cs"]
            )

        return result, scripted, config

    def test_broken_contract_is_never_frozen(self):
        result, scripted, config = self._run(
            self.BROKEN_SNIPPET
        )

        self.assertIsNone(
            result,
            "a test contract with intrinsic compiler errors was frozen"
        )

        # The frozen file on disk must be untouched.
        self.assertEqual(
            (
                Path(self.workspace) / "RegistryTests.cs"
            ).read_text(),
            self.ORIGINAL
        )

    def test_no_model_reviewer_is_consulted(self):
        _, scripted, _ = self._run(
            self.BROKEN_SNIPPET
        )

        self.assertEqual(
            scripted.calls_for("mock-structural"),
            []
        )
        self.assertEqual(
            scripted.calls_for("mock-semantic"),
            []
        )

    def test_rejection_is_recorded_in_history(self):
        _, _, config = self._run(
            self.BROKEN_SNIPPET
        )

        events = [
            event["event"]
            for event in read_history(config)
        ]

        self.assertIn(
            "contract_source_rejected",
            events
        )

    def test_valid_contract_still_freezes(self):
        good = """
[Fact]
public void Register_RecordsEntry()
{
    var registry = new Registry();
    Assert.Collection(registry.Entries(),
        e =>
        {
            Assert.Equal("W-1", e.Code);
            Assert.Equal(10, e.Quantity);
        });
}
"""

        result, scripted, _ = self._run(good)

        self.assertIsNotNone(result)

        self.assertIn(
            "RegistryTests.cs",
            result["frozen_tests"]
        )

        self.assertTrue(
            scripted.calls_for("mock-structural")
        )


if __name__ == "__main__":
    unittest.main()
