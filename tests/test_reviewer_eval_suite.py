"""
Invariants of the reviewer evaluation suite itself.

These exist because the suite shipped a self-contradictory pair and the
contradiction was read as a model failure. In the 16K context evaluation
(results/context-20260902-112156) case 6 expected APPROVE and case 9
expected REJECT, while case 9's contract was a strict SUPERSET of case
6's and BOTH omitted requirement 3. No reviewer consistent about
requirement coverage could satisfy both, and qwen3.5:9b's rejection of
case 6 -- citing requirements 3 and 7, correctly -- was scored as a false
reject.

A benchmark that cannot be satisfied measures nothing, so its consistency
is worth asserting.

This module deliberately does NOT import the manual evaluation harnesses:
manual_eval_reviewer_models patches urllib.request.urlopen at import time,
and the unit suite should not inherit that.
"""

import re
import unittest

from languages.dotnet import DotNetAdapter
from tests.reviewer_model_eval.fixtures import CASES


def test_names(contract):
    return {
        match.group(1)
        for match in re.finditer(
            r"public void (\w+)",
            contract
        )
    }


def case(case_id):
    return next(
        entry
        for entry in CASES
        if entry["id"] == case_id
    )


def compiler_output(entry):
    """The MSBuild-shaped output the deterministic gate would parse."""

    diagnostics = list(
        entry.get("authorized_future") or []
    ) + list(
        entry.get("unauthorized_future") or []
    )

    return "\n".join(
        f"/w/LedgerPipeline.Tests/UnitTest1.cs({index * 10},13): "
        f"error {diagnostic['code']}: {diagnostic['message']} "
        f"[/w/LedgerPipeline.Tests/LedgerPipeline.Tests.csproj]"
        for index, diagnostic in enumerate(diagnostics, start=1)
    )


class SuiteShapeTest(unittest.TestCase):
    def test_every_case_declares_both_diagnostic_sets(self):
        for entry in CASES:
            self.assertIn(
                "authorized_future",
                entry,
                f"case {entry['id']}"
            )
            self.assertIn(
                "unauthorized_future",
                entry,
                f"case {entry['id']}"
            )

    def test_expected_verdicts_are_valid(self):
        for entry in CASES:
            self.assertIn(
                entry["expected"],
                ("APPROVE", "REJECT"),
                f"case {entry['id']}"
            )

    def test_case_ids_are_unique(self):
        ids = [entry["id"] for entry in CASES]

        self.assertEqual(
            len(ids),
            len(set(ids))
        )


class DepositPairConsistencyTest(unittest.TestCase):
    """
    Cases 6 and 9 share TASK_DEPOSIT and its eight requirements. They must
    differ by exactly the thing they claim to differ by.
    """

    def setUp(self):
        self.clean = case(6)
        self.missing = case(9)

    def test_the_pair_shares_one_task(self):
        self.assertEqual(
            self.clean["task"],
            self.missing["task"]
        )
        self.assertEqual(
            self.clean["production"],
            self.missing["production"]
        )

    def test_they_expect_opposite_verdicts(self):
        self.assertEqual(self.clean["expected"], "APPROVE")
        self.assertEqual(self.missing["expected"], "REJECT")

    def test_the_rejected_contract_is_not_a_superset_of_the_approved_one(
        self
    ):
        # The original defect: case 9 contained every test case 6 had,
        # plus two more, and was still the one expected to be rejected.
        self.assertFalse(
            test_names(self.clean["contract"])
            <= test_names(self.missing["contract"])
        )

    def test_they_differ_by_exactly_the_uncovered_requirement(self):
        only_in_clean = (
            test_names(self.clean["contract"])
            - test_names(self.missing["contract"])
        )

        self.assertEqual(
            only_in_clean,
            {"Deposit_WithNonExistentAccount_ReturnsFalse"}
        )

        self.assertEqual(
            test_names(self.missing["contract"])
            - test_names(self.clean["contract"]),
            set()
        )

    def test_the_approved_contract_covers_every_requirement(self):
        names = test_names(self.clean["contract"])

        # Requirement 3: unknown account returns false.
        self.assertTrue(
            any("NonExistent" in name for name in names)
        )

        # Requirement 4: zero AND negative amounts are refused.
        self.assertTrue(
            any("ZeroAmount_ReturnsFalse" in name for name in names)
        )
        self.assertTrue(
            any("NegativeAmount" in name for name in names)
        )

        # Requirement 5 / 6: success returns true and moves the balance.
        self.assertTrue(
            any("IncreasesBalance" in name for name in names)
        )
        self.assertTrue(
            any("ReturnsTrue" in name for name in names)
        )

        # Requirement 7: a failed deposit leaves state alone.
        self.assertTrue(
            any("DoesNotModifyBalance" in name for name in names)
        )

    def test_the_rejected_contract_still_omits_requirement_three(self):
        self.assertFalse(
            any(
                "NonExistent" in name
                for name in test_names(self.missing["contract"])
            )
        )

    def test_the_approved_contract_compiles_its_guid_usage(self):
        # Deposit_WithNonExistentAccount_ReturnsFalse uses Guid.NewGuid().
        self.assertIn(
            "using System;",
            self.clean["contract"]
        )


class DeterministicGateRoutingTest(unittest.TestCase):
    """
    Which cases the deterministic gate is expected to settle on its own.

    This is what makes "model calls avoided" meaningful, and it is also
    the regression guard: gating case 3 would be a false rejection of a
    legitimate test-first contract.
    """

    def setUp(self):
        self.adapter = DotNetAdapter()

    def verdict(self, case_id):
        entry = case(case_id)

        report = self.adapter.classify_contract_diagnostics(
            compiler_output(entry),
            entry["task"]
        )

        return report["verdict"]

    def test_invented_api_is_settled_deterministically(self):
        self.assertEqual(
            self.verdict(4),
            "INVALID"
        )

    def test_the_invented_symbols_are_named_in_the_issues(self):
        entry = case(4)

        report = self.adapter.classify_contract_diagnostics(
            compiler_output(entry),
            entry["task"]
        )

        joined = " ".join(report["issues"])

        self.assertIn("TransferRequest", joined)
        self.assertNotIn(
            "Description",
            " ".join(
                diagnostic["symbol"]
                for diagnostic in report["invalid"]
            )
        )

    def test_authorized_future_api_is_not_gated(self):
        # Case 3 references only spec-requested future symbols. Rejecting
        # it deterministically would be the regression that matters most.
        self.assertEqual(
            self.verdict(3),
            "VALID"
        )

    def test_every_other_case_reaches_the_semantic_reviewer(self):
        for entry in CASES:
            if entry["id"] == 4:
                continue

            self.assertNotEqual(
                self.verdict(entry["id"]),
                "INVALID",
                f"case {entry['id']} must not be gated deterministically"
            )

    def test_only_case_four_declares_unauthorized_symbols(self):
        declaring = {
            entry["id"]
            for entry in CASES
            if entry["unauthorized_future"]
        }

        self.assertEqual(declaring, {4})


if __name__ == "__main__":
    unittest.main()
