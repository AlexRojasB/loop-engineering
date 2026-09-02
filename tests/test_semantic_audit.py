"""
Deterministic coverage of the evidence-first semantic audit.

No Ollama and no model service: the pure audit functions are exercised
directly, and the two integration cases monkeypatch
core.phases.test_contract_phase.call_model with a scripted stand-in.

The property under test throughout is that a semantic reviewer cannot
reach APPROVE without producing inspectable evidence, and that nothing in
the recovery paths can manufacture that evidence on the model's behalf.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.phases import semantic_audit
from core.phases import test_contract_phase


AUTHORIZED = ["CloseAccount", "LedgerService.Deposit"]


def requirement(rid="1", covered=True, evidence=None):
    return {
        "id": rid,
        "covered": covered,
        "evidence": evidence or (
            f"CloseAccount_Requirement{rid}_Test asserts this behavior"
        )
    }


def check(target="SomeTest", valid=True, evidence=None, **extra):
    entry = {
        "target": target,
        "valid": valid,
        "evidence": evidence or (
            "Arrange creates the state the test claims to exercise"
        )
    }
    entry.update(extra)
    return entry


def not_applicable(reason="no construct of this kind in the contract"):
    return {
        "applicable": False,
        "reason": reason
    }


def applicable(*checks):
    return {
        "applicable": True,
        "checks": list(checks) or [check()]
    }


def audit(**overrides):
    """
    A complete, clean, schema-valid audit. Every test below starts from
    this and breaks exactly one thing, so a failure names the property
    that broke.
    """

    body = {
        "requirements": [requirement()],
        "setup": applicable(),
        "identity": not_applicable(),
        "transitions": not_applicable(),
        "future_api": applicable(
            check(
                target="CloseAccount",
                evidence="listed as deterministically authorized"
            )
        ),
        "contradictions": []
    }

    body.update(overrides)

    return body


def response(decision="APPROVE", issues=None, **audit_overrides):
    return {
        "audit": audit(**audit_overrides),
        "decision": decision,
        "issues": issues or []
    }


class BareVerdictTests(unittest.TestCase):
    # 1. A bare APPROVE is exactly what the old contract permitted and
    # what the A/B evaluation showed both models defaulting to.

    def test_bare_approve_is_not_a_valid_audit(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                {"decision": "APPROVE", "issues": []}
            )
        )

    def test_bare_reject_is_not_a_valid_audit(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                {"decision": "REJECT", "issues": ["something"]}
            )
        )

    def test_bare_approve_still_passes_the_old_structural_schema(self):
        # The cheap structural reviewer must be untouched by this change.
        self.assertIsNone(
            test_contract_phase.validate_reviewer_schema(
                {"decision": "APPROVE", "issues": []}
            )
        )

    def test_complete_audit_without_decision_is_valid(self):
        # Observed from qwen3.5:9b: it emits all six audit sections and
        # then closes the object, treating the audit as the answer.
        # Under an evidence-first contract it is: the verdict is derived
        # from the evidence, so the field is redundant.
        payload = response()
        del payload["decision"]
        del payload["issues"]

        self.assertIsNone(
            semantic_audit.validate_audit_schema(payload, AUTHORIZED)
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "APPROVE")
        self.assertEqual(issues, [])

    def test_audit_without_decision_still_convicts_on_evidence(self):
        payload = response(
            contradictions=["the assertion contradicts requirement 3"]
        )
        del payload["decision"]

        self.assertIsNone(
            semantic_audit.validate_audit_schema(payload, AUTHORIZED)
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "REJECT")
        self.assertTrue(issues)

    def test_garbage_decision_value_is_still_invalid(self):
        # Optional is not "anything goes".
        payload = response()
        payload["decision"] = "MAYBE"

        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(payload)
        )

    def test_bare_audit_without_decision_is_not_a_free_approval(self):
        # No audit at all remains invalid whether or not a decision
        # field is present.
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema({"issues": []})
        )

    def test_complete_audit_is_valid(self):
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(),
                AUTHORIZED
            )
        )


class RequirementCoverageTests(unittest.TestCase):
    # 2. An uncovered requirement is the missing-scenario contract.

    def test_approve_with_uncovered_requirement_becomes_reject(self):
        payload = response(
            requirements=[
                requirement("1"),
                requirement(
                    "2",
                    covered=False,
                    evidence="no test exercises the zero-amount case"
                )
            ]
        )

        self.assertIsNone(
            semantic_audit.validate_audit_schema(payload, AUTHORIZED)
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "REJECT")
        self.assertTrue(
            any("Requirement 2" in issue for issue in issues)
        )

    def test_empty_requirements_is_invalid(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                response(requirements=[])
            )
        )

    def test_requirement_without_evidence_is_invalid(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                response(
                    requirements=[
                        {"id": "1", "covered": True, "evidence": "ok"}
                    ]
                )
            )
        )

    def test_requirement_without_covered_flag_is_invalid(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                response(
                    requirements=[
                        {"id": "1", "evidence": "a test asserts this"}
                    ]
                )
            )
        )


class FailedCheckTests(unittest.TestCase):
    # 3 and 4. Evidence of a defect outranks the model's own verdict.

    def test_approve_with_invalid_setup_becomes_reject(self):
        payload = response(
            setup=applicable(
                check(
                    target="CloseAccount_Test",
                    valid=False,
                    evidence="Arrange never registers the account"
                )
            )
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "REJECT")
        self.assertTrue(
            any("CloseAccount_Test" in issue for issue in issues)
        )

    def test_approve_with_contradiction_becomes_reject(self):
        payload = response(
            contradictions=[
                "the task requires false but the test asserts true"
            ]
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "REJECT")
        self.assertTrue(
            any("Contradiction" in issue for issue in issues)
        )

    def test_clean_audit_approves(self):
        decision, issues = semantic_audit.derive_effective_verdict(
            response()
        )

        self.assertEqual(decision, "APPROVE")
        self.assertEqual(issues, [])


class RejectionEvidenceTests(unittest.TestCase):
    # 5. A rejection with nothing to act on drives a revision loop with
    # no content, so it fails closed instead.

    def test_reject_without_evidence_is_unusable(self):
        decision, reason = semantic_audit.derive_effective_verdict(
            response(decision="REJECT")
        )

        self.assertIsNone(decision)
        self.assertIn("nothing for a revision", reason)

    def test_issues_without_a_decision_are_read_as_a_rejection(self):
        # A model that lists defects and omits `decision` has rejected
        # the contract in everything but name. Reading that as approval
        # would discard the only findings it produced.
        payload = response(issues=["the assertion contradicts req 3"])
        del payload["decision"]

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "REJECT")
        self.assertIn("the assertion contradicts req 3", issues)

    def test_empty_issues_without_a_decision_still_approves(self):
        payload = response()
        del payload["decision"]

        decision, _ = semantic_audit.derive_effective_verdict(payload)

        self.assertEqual(decision, "APPROVE")

    def test_reject_with_its_own_issue_survives(self):
        decision, issues = semantic_audit.derive_effective_verdict(
            response(
                decision="REJECT",
                issues=["the assertion contradicts requirement 4"]
            )
        )

        self.assertEqual(decision, "REJECT")
        self.assertIn(
            "the assertion contradicts requirement 4",
            issues
        )

    def test_reject_backed_only_by_a_failed_check_survives(self):
        decision, issues = semantic_audit.derive_effective_verdict(
            response(
                decision="REJECT",
                setup=applicable(
                    check(
                        valid=False,
                        evidence="Arrange builds an unregistered object"
                    )
                )
            )
        )

        self.assertEqual(decision, "REJECT")
        self.assertTrue(issues)

    def test_model_reject_is_never_overturned(self):
        # A model may reject for something the dimensions do not model.
        decision, _ = semantic_audit.derive_effective_verdict(
            response(
                decision="REJECT",
                issues=["an defect none of the dimensions cover"]
            )
        )

        self.assertEqual(decision, "REJECT")


class ApplicabilityTests(unittest.TestCase):
    # 6. Irrelevant dimensions are declared, never silently omitted.

    def test_not_applicable_with_reason_is_valid(self):
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    identity=not_applicable(
                        "no test asserts state on an external object"
                    )
                ),
                AUTHORIZED
            )
        )

    def test_not_applicable_without_reason_is_invalid(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                response(identity={"applicable": False})
            )
        )

    def test_omitted_dimension_is_invalid(self):
        payload = response()
        del payload["audit"]["identity"]

        reason = semantic_audit.validate_audit_schema(payload)

        self.assertIsNotNone(reason)
        self.assertIn("identity", reason)

    def test_applicable_with_no_checks_is_invalid(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                response(
                    setup={"applicable": True, "checks": []}
                )
            )
        )

    def test_check_without_substantive_evidence_is_invalid(self):
        self.assertIsNotNone(
            semantic_audit.validate_audit_schema(
                response(
                    setup=applicable(
                        check(evidence="ok")
                    )
                )
            )
        )

    def test_every_dimension_may_be_not_applicable(self):
        # Requirement coverage is never optional; the other four are.
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    setup=not_applicable(),
                    identity=not_applicable(),
                    transitions=not_applicable(),
                    future_api=not_applicable(
                        "the gate authorized no future symbols"
                    )
                )
            )
        )


class AuthorizedFutureApiTests(unittest.TestCase):
    # 7. Deterministic machine evidence is not the model's to overrule.

    def test_authorized_symbol_marked_valid_is_accepted(self):
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    future_api=applicable(
                        check(
                            target="CloseAccount",
                            valid=True,
                            evidence="authorized by the deterministic gate"
                        )
                    )
                ),
                AUTHORIZED
            )
        )

    def test_authorized_symbol_marked_invalid_voids_the_audit(self):
        reason = semantic_audit.validate_audit_schema(
            response(
                decision="REJECT",
                issues=["CloseAccount does not exist"],
                future_api=applicable(
                    check(
                        target="CloseAccount",
                        valid=False,
                        evidence="this method is absent from production"
                    )
                )
            ),
            AUTHORIZED
        )

        self.assertIsNotNone(reason)
        self.assertIn("CloseAccount", reason)

    def test_qualified_symbol_name_still_matches(self):
        reason = semantic_audit.validate_audit_schema(
            response(
                future_api=applicable(
                    check(
                        target="LedgerService.CloseAccount",
                        valid=False,
                        evidence="absent from current production"
                    )
                )
            ),
            AUTHORIZED
        )

        self.assertIsNotNone(reason)

    def test_unauthorized_symbol_may_still_be_rejected(self):
        # An invented API is a real defect and must stay rejectable.
        payload = response(
            decision="REJECT",
            issues=["TransferWithFee is not requested by the task"],
            future_api=applicable(
                check(
                    target="TransferWithFee",
                    valid=False,
                    evidence="the task never mentions this symbol"
                )
            )
        )

        self.assertIsNone(
            semantic_audit.validate_audit_schema(payload, AUTHORIZED)
        )

        decision, _ = semantic_audit.derive_effective_verdict(payload)

        self.assertEqual(decision, "REJECT")

    def test_no_authorized_list_disables_the_cross_check(self):
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    future_api=applicable(
                        check(
                            target="Whatever",
                            valid=False,
                            evidence="not requested by the task"
                        )
                    )
                ),
                None
            )
        )


class LiteralConsistencyTests(unittest.TestCase):
    # 9 and 10. The Ledger Spec 007 false premise, generically.

    def test_hundred_is_non_zero(self):
        self.assertTrue(
            semantic_audit.evaluate_condition("100m", "!= 0")
        )

    def test_zero_is_not_non_zero(self):
        self.assertFalse(
            semantic_audit.evaluate_condition("0m", "!= 0")
        )

    def test_prose_spelling_is_understood(self):
        self.assertTrue(
            semantic_audit.evaluate_condition(
                "100m",
                "balance must be non-zero"
            )
        )

    def test_unparseable_value_yields_no_opinion(self):
        self.assertIsNone(
            semantic_audit.evaluate_condition("account.Balance", "!= 0")
        )

    def test_unparseable_condition_yields_no_opinion(self):
        self.assertIsNone(
            semantic_audit.evaluate_condition("100m", "is reachable")
        )

    def test_hundred_failing_non_zero_voids_the_audit(self):
        # The exact claim the Ledger Spec 007 run produced repeatedly:
        # "creates the account with 100m but does not make the balance
        # non-zero".
        reason = semantic_audit.validate_audit_schema(
            response(
                decision="REJECT",
                issues=["the balance is never made non-zero"],
                setup=applicable(
                    check(
                        target="CloseAccount_NonZero_ReturnsFalse",
                        valid=False,
                        observed_value="100m",
                        required_condition="!= 0",
                        evidence="the account is created with 100m"
                    )
                )
            ),
            AUTHORIZED
        )

        self.assertIsNotNone(reason)
        self.assertIn("internally inconsistent", reason)

    def test_hundred_satisfying_non_zero_is_consistent(self):
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    setup=applicable(
                        check(
                            target="CloseAccount_NonZero_ReturnsFalse",
                            valid=True,
                            observed_value="100m",
                            required_condition="!= 0",
                            evidence="created with 100m, which is non-zero"
                        )
                    )
                ),
                AUTHORIZED
            )
        )

    def test_zero_balance_fixture_is_handled_correctly(self):
        # A genuinely zero balance failing a non-zero requirement is
        # consistent, and must NOT be flagged as a contradiction.
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    decision="REJECT",
                    issues=["the account balance is zero"],
                    setup=applicable(
                        check(
                            target="CloseAccount_ZeroBalance_Test",
                            valid=False,
                            observed_value="0m",
                            required_condition="!= 0",
                            evidence="the account is created with 0m"
                        )
                    )
                ),
                AUTHORIZED
            )
        )

    def test_zero_satisfying_a_zero_requirement_is_consistent(self):
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    setup=applicable(
                        check(
                            valid=True,
                            observed_value="0m",
                            required_condition="== 0",
                            evidence="the account is created with 0m"
                        )
                    )
                ),
                AUTHORIZED
            )
        )

    def test_rubber_stamp_with_numbers_is_also_caught(self):
        # The mirror image: claiming a violated condition passed.
        reason = semantic_audit.validate_audit_schema(
            response(
                setup=applicable(
                    check(
                        valid=True,
                        observed_value="0m",
                        required_condition="!= 0",
                        evidence="the account is created with 0m"
                    )
                )
            ),
            AUTHORIZED
        )

        self.assertIsNotNone(reason)
        self.assertIn("internally inconsistent", reason)

    def test_partial_numeric_fields_are_ignored(self):
        # Only one of the two fields present means no deterministic
        # opinion, never a failure.
        self.assertIsNone(
            semantic_audit.validate_audit_schema(
                response(
                    setup=applicable(
                        check(
                            valid=False,
                            observed_value="100m",
                            evidence="the account is never registered"
                        )
                    )
                ),
                AUTHORIZED
            )
        )


class DefectFixtureTests(unittest.TestCase):
    # 11, 12 and 13. The three fixture shapes the evaluation suite
    # exercises must still reach the verdict they are meant to reach.

    def test_identity_provenance_defect_remains_rejectable(self):
        payload = response(
            decision="REJECT",
            issues=[
                "the assertion reads an object the service cannot reach"
            ],
            identity=applicable(
                check(
                    target="Deposit_ValidAccount_ReturnsTrue",
                    valid=False,
                    evidence=(
                        "the account is constructed directly and never "
                        "registered through CreateAccount"
                    )
                )
            )
        )

        self.assertIsNone(
            semantic_audit.validate_audit_schema(payload, AUTHORIZED)
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "REJECT")
        self.assertTrue(issues)

    def test_quantitative_contradiction_remains_rejectable(self):
        payload = response(
            decision="APPROVE",
            transitions=applicable(
                check(
                    target="Withdraw_Succeeds_BalanceUnchanged",
                    valid=False,
                    evidence=(
                        "balance 100 -> Withdraw(20) mutates to 80, but "
                        "the test asserts 100"
                    )
                )
            )
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        # Stated APPROVE, but the audit convicts the contract.
        self.assertEqual(decision, "REJECT")
        self.assertTrue(
            any("Withdraw_Succeeds" in issue for issue in issues)
        )

    def test_complete_good_contract_remains_approvable(self):
        payload = response(
            requirements=[
                requirement("1"),
                requirement("2"),
                requirement("3")
            ],
            setup=applicable(
                check(
                    target="Deposit_ValidAccount_ReturnsTrue",
                    evidence="the account is created through the service"
                )
            ),
            transitions=applicable(
                check(
                    target="Deposit_ValidAccount_IncreasesBalance",
                    observed_value="100m",
                    required_condition="> 0",
                    evidence="balance 100 -> Deposit(50) -> asserts 150"
                )
            )
        )

        self.assertIsNone(
            semantic_audit.validate_audit_schema(payload, AUTHORIZED)
        )

        decision, issues = semantic_audit.derive_effective_verdict(
            payload
        )

        self.assertEqual(decision, "APPROVE")
        self.assertEqual(issues, [])


class RepairFabricationTests(unittest.TestCase):
    # 8. Repair reshapes; it never supplies.

    def test_bare_approve_is_not_repair_eligible(self):
        self.assertFalse(
            semantic_audit.audit_repair_eligible(
                {"decision": "APPROVE", "issues": []}
            )
        )

    def test_bare_reject_with_issues_is_not_repair_eligible(self):
        # The verdict's own issues are not audit evidence. Counting them
        # would buy a repair call whose only possible output is an
        # invented audit.
        self.assertFalse(
            semantic_audit.audit_repair_eligible(
                {
                    "decision": "REJECT",
                    "issues": ["one problem", "another problem"]
                }
            )
        )

    def test_misplaced_audit_still_counts_as_substance(self):
        self.assertTrue(
            semantic_audit.audit_repair_eligible(
                {
                    "role": "test_audit",
                    "audit": {"requirements": [requirement()]}
                }
            )
        )

    def test_response_with_audit_substance_is_repair_eligible(self):
        self.assertTrue(
            semantic_audit.audit_repair_eligible(
                {
                    "decision": "APPROVE",
                    "requirements": [requirement()]
                }
            )
        )

    def test_repair_inventing_an_audit_is_rejected(self):
        original = {"decision": "APPROVE", "issues": []}

        self.assertTrue(
            semantic_audit.repair_fabricated(original, response())
        )

    def test_repair_reshaping_the_same_entries_is_allowed(self):
        # The realistic malformed case: the audit is all there, the
        # envelope around it is wrong. Reshaping that is legitimate.
        original = {
            "decision": "UNCLEAR",
            "audit": audit()
        }

        self.assertFalse(
            semantic_audit.repair_fabricated(original, response())
        )

    def test_repair_adding_a_whole_dimension_is_rejected(self):
        # An original that never classified identity or transitions did
        # not audit them, and repair may not decide they were fine.
        original = {
            "verdict": "APPROVE",
            "audit": {
                "requirements": [requirement()],
                "setup": {"checks": [check()]}
            }
        }

        self.assertTrue(
            semantic_audit.repair_fabricated(original, response())
        )

    def test_repair_dropping_entries_is_allowed(self):
        self.assertFalse(
            semantic_audit.repair_fabricated(
                response(
                    requirements=[
                        requirement("1"),
                        requirement("2"),
                        requirement("3")
                    ]
                ),
                response()
            )
        )


class ResolverIntegrationTests(unittest.TestCase):
    # 14 and 15. The behavior the phase actually depends on.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.config = {
            "history_file": str(
                Path(self.tmp.name) / "history.jsonl"
            )
        }

    def resolve(self, responses, label="semantic", **kwargs):
        scripted = list(responses)

        def call_model(config, model, prompt, **_):
            return scripted.pop(0)

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            side_effect=call_model
        ) as patched:
            outcome = test_contract_phase._resolve_reviewer_verdict(
                self.config,
                "test-model",
                "prompt",
                label,
                "Tests/UnitTest1.cs",
                1,
                False,
                16384,
                2048,
                **kwargs
            )

        return outcome, patched

    @staticmethod
    def reply(payload):
        return {
            "ok": True,
            "response": json.dumps(payload),
            "thinking": None,
            "done_reason": "stop",
            "truncated": False
        }

    def test_semantic_label_requires_an_audit(self):
        outcome, patched = self.resolve(
            [self.reply({"decision": "APPROVE", "issues": []})]
        )

        self.assertEqual(outcome["status"], "invalid")
        # Repair was never attempted: there was nothing to reshape.
        self.assertEqual(patched.call_count, 1)

    def test_semantic_confirmation_uses_the_same_contract(self):
        outcome, patched = self.resolve(
            [self.reply({"decision": "APPROVE", "issues": []})],
            label="semantic_confirmation"
        )

        self.assertEqual(outcome["status"], "invalid")
        self.assertEqual(patched.call_count, 1)

    def test_structural_label_keeps_the_bare_verdict_schema(self):
        outcome, _ = self.resolve(
            [self.reply({"decision": "APPROVE", "issues": []})],
            label="structural"
        )

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "APPROVE")

    def test_complete_audit_resolves_to_approve(self):
        outcome, _ = self.resolve(
            [self.reply(response())],
            authorized_symbols=AUTHORIZED
        )

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "APPROVE")

    def test_effective_verdict_overrides_a_contradicted_approve(self):
        outcome, _ = self.resolve(
            [
                self.reply(
                    response(
                        contradictions=[
                            "the test asserts true where the task "
                            "requires false"
                        ]
                    )
                )
            ],
            authorized_symbols=AUTHORIZED
        )

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "REJECT")
        self.assertTrue(outcome["issues"])

    def test_repair_runs_when_there_is_audit_substance(self):
        # A complete audit wrapped in the wrong envelope: "verdict"
        # instead of "decision". Repair reshapes it and the verdict
        # survives.
        malformed = {
            "decision": "UNCLEAR",
            "audit": audit()
        }

        outcome, patched = self.resolve(
            [
                self.reply(malformed),
                self.reply(response())
            ],
            authorized_symbols=AUTHORIZED
        )

        self.assertEqual(patched.call_count, 2)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "APPROVE")

    def test_repair_that_fabricates_an_audit_is_discarded(self):
        # Original has one requirement entry; the "repair" returns a
        # full audit with several. Counting catches the invention.
        malformed = {
            "verdict": "APPROVE",
            "audit": {"requirements": [requirement()]}
        }

        outcome, patched = self.resolve(
            [
                self.reply(malformed),
                self.reply(
                    response(
                        requirements=[
                            requirement("1"),
                            requirement("2"),
                            requirement("3")
                        ],
                        transitions=applicable(check(), check())
                    )
                )
            ],
            authorized_symbols=AUTHORIZED
        )

        self.assertEqual(patched.call_count, 2)
        self.assertEqual(outcome["status"], "invalid")

    def test_call_failure_still_fails_closed(self):
        outcome, _ = self.resolve(
            [
                {
                    "ok": False,
                    "error": "timeout",
                    "response": None,
                    "thinking": None,
                    "done_reason": None,
                    "truncated": False
                }
            ]
        )

        self.assertEqual(outcome["status"], "call_failed")

    def test_truncated_audit_still_fails_closed(self):
        outcome, _ = self.resolve(
            [
                {
                    "ok": True,
                    "response": json.dumps(response()),
                    "thinking": None,
                    "done_reason": "length",
                    "truncated": True
                }
            ]
        )

        self.assertEqual(outcome["status"], "truncated")

    def test_reject_without_evidence_fails_closed(self):
        outcome, _ = self.resolve(
            [self.reply(response(decision="REJECT"))],
            authorized_symbols=AUTHORIZED
        )

        self.assertEqual(outcome["status"], "invalid")

    def test_audit_never_depends_on_the_thinking_field(self):
        # Ollama returns no separate thinking for some models; the audit
        # must validate from the structured response alone.
        payload = self.reply(response())
        payload["thinking"] = None

        outcome, _ = self.resolve(
            [payload],
            authorized_symbols=AUTHORIZED
        )

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["decision"], "APPROVE")


if __name__ == "__main__":
    unittest.main()
