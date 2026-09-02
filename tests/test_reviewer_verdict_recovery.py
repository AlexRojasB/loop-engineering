"""
Recovery of reviewer verdicts from incomplete responses, and the limits
on it.

Every case here is derived from the 16K context evaluation
(tests/reviewer_model_eval/results/context-20260902-112156). The central
rule under test is asymmetric and must stay that way:

    a rejection the model finished stating may be recovered from an
    incomplete response;

    an approval may NEVER be, because truncation removes audit content
    and the removed part may be exactly the defect.
"""

import json
import tempfile
import unittest
from pathlib import Path

from core.phases.semantic_audit import (
    derive_effective_verdict,
    hoist_misplaced_verdict,
)
from core.phases.test_contract_phase import (
    _recover_verdict_from_incomplete,
    _resolve_reviewer_verdict,
)
import core.phases.test_contract_phase as phase


AUTHORIZED = ["LedgerService.Deposit"]


def _requirements(uncovered=False):
    entries = [
        {
            "id": "1",
            "covered": True,
            "evidence":
                "Deposit_WithValidAccountAndAmount_ReturnsTrue asserts "
                "that Deposit returns true for valid input"
        }
    ]

    if uncovered:
        entries.append(
            {
                "id": "3",
                "covered": False,
                "evidence":
                    "No test verifies Deposit returns false when the "
                    "account does not exist"
            }
        )

    return entries


def _dimension(applicable=False):
    if applicable:
        return {
            "applicable": True,
            "checks": [
                {
                    "target": "Deposit_WithValidAccountAndAmount",
                    "valid": True,
                    "evidence":
                        "the account id comes from the service's own "
                        "lookup, so provenance is correct"
                }
            ]
        }

    return {
        "applicable": False,
        "reason":
            "this contract exercises no behaviour in this dimension at "
            "all, so there is nothing to check here"
    }


def _audit(uncovered=False):
    return {
        "requirements": _requirements(uncovered),
        "setup": _dimension(True),
        "identity": _dimension(),
        "transitions": _dimension(),
        "future_api": _dimension(),
        "contradictions": []
    }


def _response(uncovered=False, decision=None, issues=None, nested=False):
    audit = _audit(uncovered)
    body = {"audit": audit}

    target = audit if nested else body

    if decision is not None:
        target["decision"] = decision

    if issues is not None:
        target["issues"] = issues

    return json.dumps(body)


class HoistMisplacedVerdictTest(unittest.TestCase):
    """
    The case-9 shape: a correct rejection written one level too deep.
    """

    def test_hoists_decision_and_issues_out_of_audit(self):
        parsed = json.loads(
            _response(
                decision="REJECT",
                issues=["Requirement 3 is not covered"],
                nested=True
            )
        )

        # Without the hoist this reads as a clean audit with no verdict.
        self.assertEqual(
            derive_effective_verdict(parsed),
            ("APPROVE", [])
        )

        hoisted = hoist_misplaced_verdict(parsed)

        decision, issues = derive_effective_verdict(hoisted)

        self.assertEqual(decision, "REJECT")
        self.assertIn(
            "Requirement 3 is not covered",
            issues
        )

    def test_removes_the_hoisted_keys_from_audit(self):
        parsed = json.loads(
            _response(
                decision="REJECT",
                issues=["gap"],
                nested=True
            )
        )

        hoisted = hoist_misplaced_verdict(parsed)

        self.assertNotIn("decision", hoisted["audit"])
        self.assertNotIn("issues", hoisted["audit"])

    def test_a_root_verdict_always_wins(self):
        parsed = json.loads(
            _response(decision="REJECT", issues=["real"])
        )
        parsed["audit"]["decision"] = "APPROVE"
        parsed["audit"]["issues"] = ["shadowed"]

        hoisted = hoist_misplaced_verdict(parsed)

        self.assertEqual(hoisted["decision"], "REJECT")
        self.assertEqual(hoisted["issues"], ["real"])

    def test_cannot_turn_a_rejection_into_an_approval(self):
        # A nested APPROVE must not overturn failing audit evidence.
        parsed = json.loads(
            _response(
                uncovered=True,
                decision="APPROVE",
                nested=True
            )
        )

        decision, _ = derive_effective_verdict(
            hoist_misplaced_verdict(parsed)
        )

        self.assertEqual(decision, "REJECT")

    def test_is_a_no_op_without_a_nested_verdict(self):
        parsed = json.loads(_response(decision="APPROVE"))

        self.assertEqual(
            hoist_misplaced_verdict(parsed),
            parsed
        )

    def test_tolerates_junk_input(self):
        for value in (None, [], "text", 3, {}, {"audit": "not a dict"}):
            self.assertEqual(
                hoist_misplaced_verdict(value),
                value
            )


class RecoverVerdictFromIncompleteTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            suffix=".jsonl",
            delete=False
        )
        handle.close()

        self.history = Path(handle.name)
        self.config = {"history_file": str(self.history)}

    def tearDown(self):
        self.history.unlink(missing_ok=True)

    def recover(self, text, require_audit=True):
        return _recover_verdict_from_incomplete(
            self.config,
            text,
            "semantic",
            "UnitTest1.cs",
            1,
            require_audit,
            AUTHORIZED,
            "test"
        )

    def test_recovers_a_stated_rejection_from_a_truncated_response(self):
        truncated = _response(
            decision="REJECT",
            issues=[
                "Requirement 3 is not covered - no test verifies "
                "Deposit returns false when the account does not exist"
            ]
        )[:-1]

        with self.assertRaises(json.JSONDecodeError):
            json.loads(truncated)

        outcome = self.recover(truncated)

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "REJECT")
        self.assertIn(
            "Requirement 3 is not covered",
            outcome["issues"][0]
        )

    def test_recovers_the_case_9_nested_and_truncated_shape(self):
        # Both defects at once, exactly as observed at 16K.
        truncated = _response(
            decision="REJECT",
            issues=["Requirement 3 is not covered"],
            nested=True
        )[:-1]

        outcome = self.recover(truncated)

        self.assertEqual(outcome["decision"], "REJECT")

    def test_refuses_to_recover_an_approval(self):
        truncated = _response(decision="APPROVE")[:-1]

        self.assertIsNone(
            self.recover(truncated)
        )

    def test_refuses_to_recover_a_clean_audit_with_no_verdict(self):
        # Derivation would call this APPROVE. From a document missing its
        # tail, that inference is not available.
        self.assertIsNone(
            self.recover(_response()[:-1])
        )

    def test_refuses_a_rejection_with_nothing_to_act_on(self):
        truncated = _response(decision="REJECT", issues=[])[:-1]

        self.assertIsNone(
            self.recover(truncated)
        )

    def test_refuses_when_the_audit_schema_does_not_hold(self):
        # A bare verdict is not an audit and never becomes one.
        self.assertIsNone(
            self.recover('{"decision": "REJECT", "issues": ["x"]')
        )

    def test_refuses_when_truncation_destroyed_the_evidence(self):
        # Cut inside the first requirement's evidence: the entry loses
        # its evidence, the schema gate fails, nothing is recovered.
        text = (
            '{"audit":{"requirements":[{"id":"1","covered":false,'
            '"evidence":"No test verif'
        )

        self.assertIsNone(
            self.recover(text)
        )

    def test_recovers_a_structural_reviewer_rejection(self):
        truncated = json.dumps(
            {
                "decision": "REJECT",
                "issues": ["missing required scenario"]
            }
        )[:-1]

        outcome = self.recover(
            truncated,
            require_audit=False
        )

        self.assertEqual(outcome["decision"], "REJECT")

    def test_refuses_a_structural_reviewer_approval(self):
        truncated = json.dumps(
            {"decision": "APPROVE", "issues": []}
        )[:-1]

        self.assertIsNone(
            self.recover(
                truncated,
                require_audit=False
            )
        )

    def test_returns_none_for_empty_input(self):
        for value in ("", "   ", None):
            self.assertIsNone(
                self.recover(value)
            )

    def test_records_a_history_event_when_it_recovers(self):
        truncated = _response(
            decision="REJECT",
            issues=["Requirement 3 is not covered"]
        )[:-1]

        self.recover(truncated)

        events = [
            json.loads(line)
            for line in self.history.read_text().splitlines()
            if line.strip()
        ]

        recovered = [
            event
            for event in events
            if event.get("event") == "reviewer_verdict_recovered"
        ]

        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            recovered[0]["data"]["decision"],
            "REJECT"
        )

    def test_makes_no_model_call(self):
        calls = []

        original = phase.call_model
        phase.call_model = lambda *a, **k: calls.append(a) or {}

        try:
            self.recover(
                _response(
                    decision="REJECT",
                    issues=["Requirement 3 is not covered"]
                )[:-1]
            )

        finally:
            phase.call_model = original

        self.assertEqual(calls, [])


class ResolveReviewerVerdictStatusTest(unittest.TestCase):
    """
    End-to-end through _resolve_reviewer_verdict, with call_model stubbed.
    """

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            suffix=".jsonl",
            delete=False
        )
        handle.close()

        self.history = Path(handle.name)
        self.config = {"history_file": str(self.history)}
        self.original = phase.call_model

    def tearDown(self):
        phase.call_model = self.original
        self.history.unlink(missing_ok=True)

    def stub(self, **response):
        payload = {
            "ok": True,
            "response": "",
            "thinking": None,
            "done_reason": "stop",
            "truncated": False,
            "error": None
        }
        payload.update(response)

        phase.call_model = lambda *a, **k: payload

    def resolve(self):
        return _resolve_reviewer_verdict(
            self.config,
            "qwen3.5:9b",
            "prompt",
            "semantic",
            "UnitTest1.cs",
            1,
            True,
            16384,
            2048,
            authorized_symbols=AUTHORIZED
        )

    def test_call_failure_stays_call_failed(self):
        phase.call_model = lambda *a, **k: {
            "ok": False,
            "response": None,
            "thinking": None,
            "done_reason": None,
            "truncated": False,
            "error": "qwen3.5:9b timed out after 420s"
        }

        self.assertEqual(
            self.resolve()["status"],
            "call_failed"
        )

    def test_unparseable_response_is_not_reported_as_call_failed(self):
        # The distinction that matters: the model answered, and the
        # answer could not be read. That is not a service failure.
        self.stub(response='{"audit": {"requirements": [')

        self.assertEqual(
            self.resolve()["status"],
            "unparseable"
        )

    def test_unparseable_but_stated_rejection_is_recovered(self):
        self.stub(
            response=_response(
                decision="REJECT",
                issues=["Requirement 3 is not covered"]
            )[:-1]
        )

        outcome = self.resolve()

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "REJECT")

    def test_truncated_response_still_reports_truncated_when_unusable(self):
        self.stub(
            response=_response()[:-1],
            done_reason="length",
            truncated=True
        )

        self.assertEqual(
            self.resolve()["status"],
            "truncated"
        )

    def test_truncated_response_with_a_stated_rejection_is_recovered(self):
        self.stub(
            response=_response(
                decision="REJECT",
                issues=["Requirement 3 is not covered"]
            )[:-1],
            done_reason="length",
            truncated=True
        )

        outcome = self.resolve()

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "REJECT")

    def test_truncated_approval_is_never_recovered(self):
        self.stub(
            response=_response(decision="APPROVE")[:-1],
            done_reason="length",
            truncated=True
        )

        self.assertEqual(
            self.resolve()["status"],
            "truncated"
        )

    def test_a_complete_response_is_unaffected(self):
        self.stub(response=_response(decision="APPROVE"))

        outcome = self.resolve()

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "APPROVE")

    def test_a_complete_nested_rejection_is_hoisted_not_approved(self):
        # The latent false-approve this fix closes: schema-valid,
        # parseable, and its verdict one level too deep.
        self.stub(
            response=_response(
                decision="REJECT",
                issues=[
                    "Requirement 3 is not covered - no test verifies "
                    "Deposit returns false when the account does not "
                    "exist"
                ],
                nested=True
            )
        )

        outcome = self.resolve()

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "REJECT")

    def test_bare_verdict_without_audit_is_still_invalid(self):
        # The existing no-fabrication rule is untouched.
        self.stub(response='{"decision": "APPROVE"}')

        self.assertEqual(
            self.resolve()["status"],
            "invalid"
        )


if __name__ == "__main__":
    unittest.main()
