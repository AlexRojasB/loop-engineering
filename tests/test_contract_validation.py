"""
Regression coverage for deterministic contract validation and Expected
RED classification.

Two distinct Ledger failures motivate this file.

1. A contract was frozen that used an existing API incorrectly:

       var sourceId = service.CreateAccount("Source", 100m);
       service.Transfer(sourceId, ..., 50m);

   `CreateAccount` returns bool, not Guid, so the build failed with
   "cannot convert from 'bool' to 'System.Guid'". That is not the
   requested-feature-is-missing RED the workflow expects; it is proof the
   contract itself is broken. It reached implementation anyway.

2. "No overload for method 'Transfer' takes 4 arguments" was classified
   UNKNOWN even though the extra argument was exactly what the current
   spec introduced.

No .NET toolchain is required: the adapter classifier is a pure function
over compiler output, and the compile step is injected.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.contract_validation import (  # noqa: E402
    adapter_supports_validation,
    validate_candidate_contract,
)
from core.phases.test_contract_phase import (  # noqa: E402
    deterministic_contract_gate,
)
from core.red_state import classify_expected_red  # noqa: E402
from core.symbols import spec_requests_symbol  # noqa: E402
from languages.base import LanguageAdapter  # noqa: E402
from languages.dotnet import DotNetAdapter  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures - generic domain, no benchmark vocabulary
# --------------------------------------------------------------------------

SPEC_ADDS_METHOD = """
# Archive Widget

## Requirements

1. Add a public method named `ArchiveWidget`.
2. Return false when the widget does not exist.
3. Return true when an existing widget is archived.
"""

SPEC_ADDS_OVERLOAD = """
# Annotated Transfer

## Requirements

1. Extend `MoveStock` with an optional note argument.
2. Trim the note before storing it.
"""

SPEC_UNRELATED = """
# Low Stock Query

## Requirements

1. Add a public property named `LowStockThreshold` with default 50.
2. Add a public query method named `GetLowStockItems`.
"""


def error(code, message, project="Widgets.Tests.csproj"):
    return (
        f"/repo/tests/WidgetTests.cs(41,21): "
        f"error {code}: {message} [/repo/tests/{project}]"
    )


MISSING_MEMBER = error(
    "CS1061",
    "'WidgetRegistry' does not contain a definition for "
    "'ArchiveWidget' and no accessible extension method "
    "'ArchiveWidget' accepting a first argument of type "
    "'WidgetRegistry' could be found"
)

MISSING_OVERLOAD = error(
    "CS1501",
    "No overload for method 'MoveStock' takes 4 arguments"
)

BAD_CONVERSION = error(
    "CS1503",
    "Argument 1: cannot convert from 'bool' to 'System.Guid'"
)

BROKEN_SYNTAX = error(
    "CS1513",
    "} expected"
)

CLEAN_BUILD = (
    "  Determining projects to restore...\n"
    "  Widgets -> /repo/src/bin/Debug/net10.0/Widgets.dll\n"
    "\nBuild succeeded.\n"
)


class MissingMemberIsExpectedRedTests(unittest.TestCase):
    """Requirement 5."""

    def setUp(self):
        self.adapter = DotNetAdapter()

    def test_requested_missing_member_is_expected_red(self):
        report = self.adapter.classify_contract_diagnostics(
            MISSING_MEMBER,
            SPEC_ADDS_METHOD
        )

        self.assertEqual(report["verdict"], "VALID")
        self.assertEqual(len(report["expected_red"]), 1)
        self.assertEqual(
            report["expected_red"][0]["symbol"],
            "ArchiveWidget"
        )
        self.assertEqual(report["invalid"], [])

    def test_expected_red_phase_classification(self):
        result = classify_expected_red(
            MISSING_MEMBER,
            adapter=self.adapter,
            spec_text=SPEC_ADDS_METHOD
        )

        self.assertEqual(
            result["classification"],
            "EXPECTED_RED"
        )

    def test_member_the_spec_never_requested_is_not_expected_red(self):
        # Exactly the invented-API leakage seen when a later work item's
        # behaviour bled into an earlier one.
        result = classify_expected_red(
            MISSING_MEMBER,
            adapter=self.adapter,
            spec_text=SPEC_UNRELATED
        )

        self.assertEqual(
            result["classification"],
            "INVALID_CONTRACT"
        )


class MissingOverloadIsExpectedRedTests(unittest.TestCase):
    """Requirement 6 - the case the benchmark classified UNKNOWN."""

    def setUp(self):
        self.adapter = DotNetAdapter()

    def test_requested_missing_overload_is_expected_red(self):
        report = self.adapter.classify_contract_diagnostics(
            MISSING_OVERLOAD,
            SPEC_ADDS_OVERLOAD
        )

        self.assertEqual(report["verdict"], "VALID")
        self.assertEqual(
            [d["symbol"] for d in report["expected_red"]],
            ["MoveStock"]
        )

    def test_expected_red_phase_no_longer_returns_unknown(self):
        result = classify_expected_red(
            MISSING_OVERLOAD,
            adapter=self.adapter,
            spec_text=SPEC_ADDS_OVERLOAD
        )

        self.assertEqual(
            result["classification"],
            "EXPECTED_RED"
        )

    def test_overload_on_a_method_the_spec_never_mentions_is_not_expected_red(self):
        result = classify_expected_red(
            MISSING_OVERLOAD,
            adapter=self.adapter,
            spec_text=SPEC_UNRELATED
        )

        self.assertEqual(
            result["classification"],
            "INVALID_CONTRACT"
        )


class TypeConversionIsNotExpectedRedTests(unittest.TestCase):
    """Requirement 7."""

    def setUp(self):
        self.adapter = DotNetAdapter()

    def test_conversion_error_is_invalid_not_expected_red(self):
        report = self.adapter.classify_contract_diagnostics(
            BAD_CONVERSION,
            SPEC_ADDS_METHOD
        )

        self.assertEqual(report["verdict"], "INVALID")
        self.assertEqual(report["expected_red"], [])
        self.assertEqual(
            report["invalid"][0]["category"],
            "invalid_contract"
        )

    def test_conversion_error_stays_invalid_even_for_a_related_spec(self):
        # The spec introduces MoveStock's new overload, yet a bool/Guid
        # conversion against the *existing* API is still a setup defect.
        result = classify_expected_red(
            BAD_CONVERSION,
            adapter=self.adapter,
            spec_text=SPEC_ADDS_OVERLOAD
        )

        self.assertEqual(
            result["classification"],
            "INVALID_CONTRACT"
        )

    def test_conversion_alongside_a_legitimate_missing_member_still_rejects(self):
        result = classify_expected_red(
            MISSING_MEMBER + "\n" + BAD_CONVERSION,
            adapter=self.adapter,
            spec_text=SPEC_ADDS_METHOD
        )

        self.assertEqual(
            result["classification"],
            "INVALID_CONTRACT"
        )

    def test_issue_text_explains_the_defect_to_the_reviser(self):
        report = self.adapter.classify_contract_diagnostics(
            BAD_CONVERSION,
            SPEC_ADDS_METHOD
        )

        joined = " ".join(report["issues"])

        self.assertIn("CS1503", joined)
        self.assertIn("test setup", joined)

    def test_broken_syntax_is_reported_separately(self):
        result = classify_expected_red(
            BROKEN_SYNTAX,
            adapter=DotNetAdapter(),
            spec_text=SPEC_ADDS_METHOD
        )

        self.assertEqual(
            result["classification"],
            "BROKEN_TEST_SUITE"
        )

    def test_behaviour_failure_remains_expected_red(self):
        result = classify_expected_red(
            "Failed!  - Failed: 1, Passed: 12\n"
            "  Assert.Equal() Failure",
            adapter=DotNetAdapter(),
            spec_text=SPEC_ADDS_METHOD
        )

        self.assertEqual(
            result["classification"],
            "EXPECTED_RED"
        )


class DiagnosticParsingTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DotNetAdapter()

    def test_msbuild_repeats_collapse_to_one_diagnostic(self):
        repeated = "\n".join([
            error("CS1061", "'R' does not contain a definition for 'X'"),
            error(
                "CS1061",
                "'R' does not contain a definition for 'X'",
                project="Other.csproj"
            ),
        ])

        self.assertEqual(
            len(
                self.adapter.parse_diagnostics(repeated)
            ),
            1
        )

    def test_warnings_are_not_diagnostics(self):
        self.assertEqual(
            self.adapter.parse_diagnostics(
                "/repo/src/W.cs(29,44): warning CS8602: "
                "Dereference of a possibly null reference."
            ),
            []
        )

    def test_clean_build_reports_no_diagnostics(self):
        report = self.adapter.classify_contract_diagnostics(
            CLEAN_BUILD,
            SPEC_ADDS_METHOD
        )

        self.assertTrue(report["compiles_clean"])
        self.assertEqual(report["verdict"], "VALID")


class SymbolMatchingTests(unittest.TestCase):
    def test_exact_identifier_matches(self):
        self.assertTrue(
            spec_requests_symbol(
                "ArchiveWidget",
                SPEC_ADDS_METHOD
            )
        )

    def test_prose_description_matches_by_words(self):
        self.assertTrue(
            spec_requests_symbol(
                "GetLowStockItems",
                "Add a query returning low stock items."
            )
        )

    def test_unrelated_symbol_does_not_match(self):
        self.assertFalse(
            spec_requests_symbol(
                "ArchiveWidget",
                SPEC_UNRELATED
            )
        )

    def test_empty_spec_matches_nothing(self):
        self.assertFalse(
            spec_requests_symbol("Anything", "")
        )


# --------------------------------------------------------------------------
# The pre-freeze gate
# --------------------------------------------------------------------------

class StubAdapter(DotNetAdapter):
    """Real classifier, stub build command."""

    def build_argv(self, workspace_files):
        return ["stub-build"]


class UnsupportedAdapter(LanguageAdapter):
    name = "unsupported"

    def can_handle(self, files):
        return True

    def build_command(self, workspace_files):
        return "noop"

    def test_command(self, workspace_files):
        return "noop"


def runner_returning(output, exit_code=1):
    def run(workspace, argv):
        run.calls.append((workspace, argv))
        return {"exit_code": exit_code, "output": output}

    run.calls = []
    return run


class PreFreezeGateTests(unittest.TestCase):
    """
    Requirement 4: a contract that misuses an existing return type is
    rejected BEFORE implementation - and before the expensive semantic
    reviewer is ever called.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

        self.test_path = "tests/WidgetTests.cs"

        (self.workspace / "tests").mkdir()

        (self.workspace / self.test_path).write_text(
            "public class WidgetTests { }\n"
        )

        self.config = {
            "state_file": str(self.workspace / "state.json"),
            "history_file": str(self.workspace / "history.jsonl"),
        }

        self.addCleanup(self._tmp.cleanup)

    def gate(self, output, adapter=None, config=None):
        return deterministic_contract_gate(
            config if config is not None else self.config,
            self.workspace,
            self.test_path,
            "public class WidgetTests { /* candidate */ }\n",
            SPEC_ADDS_METHOD,
            adapter if adapter is not None else StubAdapter(),
            ["src/Widgets.csproj"],
            1,
            runner=runner_returning(output)
        )

    def test_return_type_misuse_is_rejected_before_review(self):
        ok, issues = self.gate(BAD_CONVERSION)

        self.assertFalse(ok)
        self.assertTrue(issues)
        self.assertIn("CS1503", " ".join(issues))

    def test_requested_missing_member_passes_the_gate(self):
        ok, issues = self.gate(MISSING_MEMBER)

        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_requested_missing_overload_passes_the_gate(self):
        ok, _ = deterministic_contract_gate(
            self.config,
            self.workspace,
            self.test_path,
            "candidate",
            SPEC_ADDS_OVERLOAD,
            StubAdapter(),
            [],
            1,
            runner=runner_returning(MISSING_OVERLOAD)
        )

        self.assertTrue(ok)

    def test_invented_api_is_rejected(self):
        ok, issues = deterministic_contract_gate(
            self.config,
            self.workspace,
            self.test_path,
            "candidate",
            SPEC_UNRELATED,
            StubAdapter(),
            [],
            1,
            runner=runner_returning(MISSING_MEMBER)
        )

        self.assertFalse(ok)
        self.assertIn("ArchiveWidget", " ".join(issues))

    def test_gate_writes_the_candidate_before_compiling(self):
        self.gate(MISSING_MEMBER)

        self.assertIn(
            "candidate",
            (self.workspace / self.test_path).read_text()
        )

    def test_gate_is_skipped_for_an_adapter_without_the_hook(self):
        runner = runner_returning(BAD_CONVERSION)

        ok, issues = deterministic_contract_gate(
            self.config,
            self.workspace,
            self.test_path,
            "candidate",
            SPEC_ADDS_METHOD,
            UnsupportedAdapter(),
            [],
            1,
            runner=runner
        )

        self.assertTrue(ok)
        self.assertEqual(issues, [])
        self.assertEqual(runner.calls, [])

    def test_gate_can_be_disabled_by_configuration(self):
        config = dict(self.config)
        config["contract_compilation_check"] = False

        ok, _ = self.gate(BAD_CONVERSION, config=config)

        self.assertTrue(ok)

    def test_gate_fails_open_when_the_toolchain_is_unavailable(self):
        def exploding_runner(workspace, argv):
            raise FileNotFoundError("dotnet")

        ok, issues = deterministic_contract_gate(
            self.config,
            self.workspace,
            self.test_path,
            "candidate",
            SPEC_ADDS_METHOD,
            StubAdapter(),
            [],
            1,
            runner=exploding_runner
        )

        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_unclassifiable_diagnostics_fail_open(self):
        ok, _ = self.gate(
            error("CS9999", "brand new compiler error")
        )

        self.assertTrue(ok)


class ValidationHelperTests(unittest.TestCase):
    def test_dotnet_adapter_supports_validation(self):
        self.assertTrue(
            adapter_supports_validation(
                DotNetAdapter()
            )
        )

    def test_base_adapter_does_not(self):
        self.assertFalse(
            adapter_supports_validation(
                UnsupportedAdapter()
            )
        )

    def test_no_adapter_does_not(self):
        self.assertFalse(
            adapter_supports_validation(None)
        )

    def test_validate_returns_unsupported_without_running_anything(self):
        runner = runner_returning(BAD_CONVERSION)

        result = validate_candidate_contract(
            "/nowhere",
            UnsupportedAdapter(),
            [],
            SPEC_ADDS_METHOD,
            runner=runner
        )

        self.assertFalse(result["supported"])
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
