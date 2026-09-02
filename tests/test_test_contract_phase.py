"""
End-to-end (but fully mocked-model) regression tests for
core.phases.test_contract_phase.run_test_contract_phase covering:

- the two real Inventory-benchmark failure patterns, generalized to a
  toy Widget/Ledger domain (see tests/fixtures/toy_domains.py)
- the new `think` flag forwarding for the structural reviewer
- the new reviewer-thinking observability logging

No Ollama/model service is used: core.phases.test_contract_phase.call_model
is monkeypatched with a scripted stand-in. This tests the harness's
*wiring* (does it forward think=, does it log thinking, does it roll back
on REJECT, does it freeze on APPROVE) rather than whether a specific local
model would actually reject these snippets — that judgment call belongs to
tests/manual_eval_reviewer_prompts.py, run against a real model by hand.
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

from core.phases import test_contract_phase  # noqa: E402
from core.state import read_history  # noqa: E402

from tests.fixtures.toy_domains import (  # noqa: E402
    LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION,
    LEDGER_GOOD_SNIPPET,
    LEDGER_ORIGINAL_TEST_FILE,
    LEDGER_PRODUCTION,
    LEDGER_TASK,
    WIDGET_BAD_SNIPPET_FRESH_INSTANCE_GUARD,
    WIDGET_ORIGINAL_TEST_FILE,
    WIDGET_PRODUCTION,
    WIDGET_TASK,
)


def clean_audit():
    """
    The minimum evidence-first audit a semantic reviewer must produce.

    Every scripted verdict below carries one, because since the
    evidence-first redesign a bare {"decision": ...} is not a semantic
    review at all -- it is discarded and retried. The structural
    reviewer ignores the extra key, so one builder still serves both
    roles and these tests keep exercising phase orchestration rather
    than schema shape. Schema shape has its own suite in
    tests/test_semantic_audit.py.
    """

    return {
        "requirements": [
            {
                "id": "1",
                "covered": True,
                "evidence": "the scripted contract covers this requirement"
            }
        ],
        "setup": {
            "applicable": True,
            "checks": [
                {
                    "target": "ScriptedTest",
                    "valid": True,
                    "evidence": "Arrange builds the state under test"
                }
            ]
        },
        "identity": {
            "applicable": False,
            "reason": "no assertion reads an externally built object"
        },
        "transitions": {
            "applicable": False,
            "reason": "no test asserts a state or quantity change"
        },
        "future_api": {
            "applicable": False,
            "reason": "the scripted gate authorized no future symbols"
        },
        "contradictions": []
    }


def approve(thinking=None, done_reason="stop"):
    return {
        "ok": True,
        "response": json.dumps(
            {
                "audit": clean_audit(),
                "decision": "APPROVE",
                "issues": []
            }
        ),
        "thinking": thinking,
        "done_reason": done_reason,
        "truncated": done_reason == "length"
    }


def reject(issue, thinking=None, done_reason="stop"):
    return {
        "ok": True,
        "response": json.dumps(
            {
                "audit": clean_audit(),
                "decision": "REJECT",
                "issues": [issue]
            }
        ),
        "thinking": thinking,
        "done_reason": done_reason,
        "truncated": done_reason == "length"
    }


def truncated_response(partial_text, thinking=None):
    """
    Simulates what Ollama actually does on a length cutoff: the
    json_mode grammar force-closes braces/quotes, so this can be
    syntactically valid (parseable) JSON whose content is incomplete.
    done_reason="length" is the only reliable signal that it happened.
    """
    return {
        "ok": True,
        "response": partial_text,
        "thinking": thinking,
        "done_reason": "length",
        "truncated": True
    }


def coder_returns(snippet):
    return {
        "ok": True,
        "response": snippet,
        "thinking": None,
        "done_reason": "stop",
        "truncated": False
    }


def envelope_response(
    role,
    content,
    thinking=None,
    done_reason="stop",
    audit=True
):
    """
    Simulates a complete, non-truncated response that is valid JSON
    but the wrong top-level shape — the exact failure mode found by
    the real-model smoke test: {"role": ..., "content": ...} instead
    of {"decision": ..., "issues": [...]}.

    `audit=True` carries the structured audit alongside the wrong
    envelope, which is what a semantic reviewer's malformed response
    actually looks like: the evidence is there, the wrapper is wrong.
    That is the only malformed shape schema repair is allowed to touch,
    since repair reshapes what the model said and cannot supply an
    audit it never performed. Pass audit=False for the bare envelope
    that must NOT be repaired.
    """

    payload = {"role": role, "content": content}

    if audit:
        # A complete audit whose verdict field is unusable: schema
        # repair exists for exactly this, and the audit content it must
        # preserve is all present. Note the audit alone would now be
        # valid -- `decision` is optional since the verdict is derived
        # from the evidence -- so the bad value is what makes this
        # malformed.
        payload["audit"] = clean_audit()
        payload["decision"] = "UNCLEAR"

    return {
        "ok": True,
        "response": json.dumps(payload),
        "thinking": thinking,
        "done_reason": done_reason,
        "truncated": done_reason == "length"
    }


def malformed_decision_value(value, thinking=None, done_reason="stop"):
    return {
        "ok": True,
        "response": json.dumps({"decision": value, "issues": []}),
        "thinking": thinking,
        "done_reason": done_reason,
        "truncated": done_reason == "length"
    }


def malformed_issues_type(thinking=None, done_reason="stop"):
    return {
        "ok": True,
        "response": json.dumps(
            {"decision": "REJECT", "issues": "not a list"}
        ),
        "thinking": thinking,
        "done_reason": done_reason,
        "truncated": done_reason == "length"
    }


class ScriptedCallModel:
    """
    Dispatches on model name; records every call made.

    A response registered for a model may be a single dict (returned
    on every call to that model) or a list of dicts (consumed in
    order, one per call, repeating the last entry once exhausted) —
    needed to script a model that answers differently across
    successive calls, e.g. REJECT then APPROVE then a confirmation
    verdict.
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(
        self,
        config,
        model,
        prompt,
        json_mode=False,
        think=False,
        num_ctx=None,
        num_predict=None
    ):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "json_mode": json_mode,
                "think": think,
                "num_ctx": num_ctx,
                "num_predict": num_predict
            }
        )

        value = self.responses[model]

        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            return value[0]

        return value

    def calls_for(self, model):
        return [c for c in self.calls if c["model"] == model]


class TestContractPhaseHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name

    def _write(self, relative, content):
        (Path(self.workspace) / relative).write_text(content)

    def _base_config(self, **overrides):
        config = {
            "coder_model": "mock-coder-model",
            "test_reviewer_model": "mock-structural-model",
            "semantic_reviewer_model": "mock-semantic-model",
            "test_reviewer_thinking": True,
            "semantic_reviewer_thinking": True,
            "max_test_generation_attempts": 1,
            "state_file":
                str(Path(self.workspace) / "state.json"),
            "history_file":
                str(Path(self.workspace) / "history.jsonl")
        }
        config.update(overrides)
        return config

    def _history_events(self, config):
        return read_history(config)

    # -- Widget domain: fresh-instance contradictory setup --------------

    def test_structural_reviewer_rejection_rolls_back_and_logs(self):
        self._write("Widget.cs", WIDGET_PRODUCTION)
        self._write("WidgetTests.cs", WIDGET_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(
                        WIDGET_BAD_SNIPPET_FRESH_INSTANCE_GUARD
                    ),
                "mock-structural-model":
                    reject(
                        "Register_RejectsWhenCodeAlreadyExists: "
                        "registry is freshly constructed, so the "
                        "guarded branch always executes and "
                        "Assert.False contradicts a valid new "
                        "registration.",
                        thinking=(
                            "registry has just been constructed; "
                            "FindByCode cannot find anything yet, "
                            "so the if-branch always runs and "
                            "Register('W-1', 10) must return true"
                        )
                    )
            }
        )

        config = self._base_config()

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                WIDGET_TASK,
                {},
                [{"path": "Widget.cs", "type": "implementation", "reason": "x"}],
                [{"path": "WidgetTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNone(result)

        # test file must be rolled back to its original content
        self.assertEqual(
            (Path(self.workspace) / "WidgetTests.cs").read_text(),
            WIDGET_ORIGINAL_TEST_FILE
        )

        structural_calls = scripted.calls_for("mock-structural-model")
        self.assertEqual(len(structural_calls), 1)
        self.assertTrue(structural_calls[0]["think"])

        # semantic reviewer must never be reached once structural rejects
        self.assertEqual(
            scripted.calls_for("mock-semantic-model"),
            []
        )

        events = self._history_events(config)
        reasoning = [
            e for e in events
            if e["event"] == "test_review_reasoning"
        ]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0]["data"]["reviewer"], "structural")
        self.assertIn(
            "just been constructed",
            reasoning[0]["data"]["thinking"]
        )

        rejected = [
            e for e in events
            if e["event"] == "test_contract_rejected"
        ]
        self.assertEqual(len(rejected), 1)

    # -- Ledger domain: quantitative-state contradiction -----------------

    def test_semantic_reviewer_rejection_rolls_back_and_logs_both_reasoners(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(
                        LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION
                    ),
                "mock-structural-model":
                    approve(
                        thinking="setup identity chain looks fine"
                    ),
                "mock-semantic-model":
                    reject(
                        "Withdraw_DoesNotChangeBalance_WhenSuccessful: "
                        "Withdraw is asserted to succeed and production "
                        "applies balance -= amount on success, so "
                        "Balance cannot remain unchanged.",
                        thinking=(
                            "Withdraw unconditionally does "
                            "balance -= amount on success; a "
                            "successful withdraw cannot leave "
                            "Balance unchanged"
                        )
                    )
            }
        )

        config = self._base_config()

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNone(result)

        self.assertEqual(
            (Path(self.workspace) / "LedgerTests.cs").read_text(),
            LEDGER_ORIGINAL_TEST_FILE
        )

        semantic_calls = scripted.calls_for("mock-semantic-model")
        self.assertEqual(len(semantic_calls), 1)
        self.assertTrue(semantic_calls[0]["think"])

        events = self._history_events(config)
        reasoning = [
            e for e in events
            if e["event"] == "test_review_reasoning"
        ]
        self.assertEqual(len(reasoning), 2)

        by_reviewer = {
            e["data"]["reviewer"]: e["data"]["thinking"]
            for e in reasoning
        }
        self.assertIn("balance -= amount", by_reviewer["semantic"])
        self.assertIn("identity chain", by_reviewer["structural"])

        rejected = [
            e for e in events
            if e["event"] == "test_contract_rejected"
        ]
        self.assertEqual(len(rejected), 1)

    # -- Control: a legitimate contract is still frozen normally ---------

    def test_approved_contract_is_still_frozen_and_logged(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup and assertions are consistent"),
                "mock-semantic-model":
                    approve(thinking="balance decreases as expected")
            }
        )

        config = self._base_config(
            max_test_generation_attempts=8
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)
        self.assertIn(
            "Withdraw_ReducesBalance_WhenSuccessful",
            result["frozen_tests"]["LedgerTests.cs"]
        )

        events = self._history_events(config)
        self.assertTrue(
            any(
                e["event"] == "test_contract_approved"
                for e in events
            )
        )
        self.assertEqual(
            len(
                [
                    e for e in events
                    if e["event"] == "test_review_reasoning"
                ]
            ),
            2
        )

    # -- Reviewer-consistency fix: confirmation after instability --------
    #
    # Regression coverage for the Spec-003 flip-flop: a semantic REJECT
    # followed, later in the same run, by a semantic APPROVE on the same
    # test_change. Generalized to the Ledger/Balance domain (the same
    # shape of bug as Inventory's AvailableQuantity).

    def test_confirmation_reject_after_prior_rejection_blocks_freeze(self):
        # Scenario A: REJECT -> revised -> APPROVE -> confirmation REJECT
        # Expected: contract is NOT frozen; revision continues.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(
                        LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION
                    ),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    reject(
                        "Withdraw_DoesNotChangeBalance_WhenSuccessful: "
                        "Balance should be reduced by amount on a "
                        "successful Withdraw.",
                        thinking="first pass: caught the contradiction"
                    ),
                    approve(thinking="second pass: looks fine now"),
                    reject(
                        "Withdraw_DoesNotChangeBalance_WhenSuccessful: "
                        "still asserts Balance unchanged after a "
                        "successful Withdraw.",
                        thinking="confirmation pass: still broken"
                    ),
                ],
            }
        )

        config = self._base_config(
            max_test_generation_attempts=2
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNone(result)

        self.assertEqual(
            (Path(self.workspace) / "LedgerTests.cs").read_text(),
            LEDGER_ORIGINAL_TEST_FILE
        )

        # 3 semantic calls: reject, approve, confirmation-reject
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 3
        )

        # revision continues: initial generation + revision after the
        # first REJECT + revision after the confirmation REJECT
        self.assertEqual(
            len(scripted.calls_for("mock-coder-model")), 3
        )

        events = self._history_events(config)
        self.assertFalse(
            any(
                e["event"] == "test_contract_approved"
                for e in events
            )
        )
        self.assertTrue(
            any(
                e["event"] == "test_contract_rejected"
                for e in events
            )
        )

        confirmation_reasoning = [
            e for e in events
            if e["event"] == "test_review_reasoning"
            and e["data"]["reviewer"] == "semantic_confirmation"
        ]
        self.assertEqual(len(confirmation_reasoning), 1)
        self.assertIn(
            "still broken",
            confirmation_reasoning[0]["data"]["thinking"]
        )

    def test_confirmation_approve_after_prior_rejection_freezes(self):
        # Scenario B: REJECT -> revised -> APPROVE -> confirmation APPROVE
        # Expected: contract freezes.
        # Also proves Scenario F: the confirmation call does not consume
        # its own max_test_generation_attempts slot — everything here
        # happens within 2 attempts (structural calls == 2, not 3).
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    reject(
                        "Withdraw_DoesNotChangeBalance_WhenSuccessful: "
                        "Balance should be reduced by amount on a "
                        "successful Withdraw.",
                        thinking="first pass: caught the contradiction"
                    ),
                    approve(thinking="second pass: now correct"),
                    approve(thinking="confirmation pass: agrees"),
                ],
            }
        )

        config = self._base_config(
            max_test_generation_attempts=2
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)
        self.assertIn(
            "Withdraw_ReducesBalance_WhenSuccessful",
            result["frozen_tests"]["LedgerTests.cs"]
        )

        # Scenario F: confirmation did not need a 3rd attempt's worth
        # of structural review — only 2 "Test snippet attempt" passes
        # occurred even though 3 semantic calls were made.
        self.assertEqual(
            len(scripted.calls_for("mock-structural-model")), 2
        )
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 3
        )

        events = self._history_events(config)
        self.assertTrue(
            any(
                e["event"] == "test_contract_approved"
                for e in events
            )
        )

        confirmation_reasoning = [
            e for e in events
            if e["event"] == "test_review_reasoning"
            and e["data"]["reviewer"] == "semantic_confirmation"
        ]
        self.assertEqual(len(confirmation_reasoning), 1)

    def test_clean_first_attempt_approve_skips_confirmation(self):
        # Scenario C: clean first-attempt APPROVE requires only one
        # semantic call; no confirmation call is made.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model":
                    approve(thinking="balance decreases as expected"),
            }
        )

        config = self._base_config(
            max_test_generation_attempts=8
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 1
        )

        events = self._history_events(config)
        self.assertFalse(
            any(
                e["data"].get("reviewer") == "semantic_confirmation"
                for e in events
                if e["event"] == "test_review_reasoning"
            )
        )

    def test_prior_rejection_issue_appears_in_subsequent_prompts(self):
        # Scenario D: a previously raised issue must appear in later
        # revision prompts and later review prompts for the same
        # test_change, not just the immediately-following revision.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        distinctive_issue = (
            "DISTINCTIVE_MARKER: Balance must decrease by amount "
            "on a successful Withdraw."
        )

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(
                        LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION
                    ),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    reject(distinctive_issue, thinking="first pass"),
                    approve(thinking="second pass"),
                    approve(thinking="confirmation pass"),
                ],
            }
        )

        config = self._base_config(
            max_test_generation_attempts=2
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # The revision call made right after the first REJECT (call #2
        # to the coder model) obviously carries the issue as its
        # immediate ISSUES section. The important assertion is that
        # LATER calls — the second attempt's structural/semantic
        # review prompts, made after that revision — still carry it
        # under "Previously Raised Concerns", proving it persists
        # rather than being forgotten after one revision cycle.
        second_attempt_structural_prompt = (
            scripted.calls_for("mock-structural-model")[1]["prompt"]
        )
        second_attempt_semantic_prompt = (
            scripted.calls_for("mock-semantic-model")[1]["prompt"]
        )
        confirmation_prompt = (
            scripted.calls_for("mock-semantic-model")[2]["prompt"]
        )

        self.assertIn(
            distinctive_issue, second_attempt_structural_prompt
        )
        self.assertIn(
            distinctive_issue, second_attempt_semantic_prompt
        )
        self.assertIn(
            distinctive_issue, confirmation_prompt
        )

        # And it must be framed as something to re-evaluate, not as
        # an automatically-still-valid defect.
        self.assertIn(
            "Previously Raised Concerns",
            second_attempt_semantic_prompt
        )

    # -- Reviewer output integrity: truncated verdicts are never trusted -

    def test_truncated_semantic_response_is_never_frozen_or_remembered(self):
        # A truncated verdict must not: (a) be treated as APPROVE or
        # REJECT, (b) add anything to rejection_memory, or (c) trigger
        # a revision. Proven here by the follow-up clean APPROVE
        # needing no confirmation call (rejection_memory stayed
        # empty) and the coder model being called only once (no
        # revision was ever triggered by the truncated attempt).
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    truncated_response(
                        '{"decision": "REJECT", "issues": '
                        '[{"test": "Withdraw_ReducesBalance_'
                        'WhenSuccessful", "issue": "Setup '
                        'identity mismatch: the test '
                        'instantiates Account directly (new '
                        'Account("}',
                        thinking=(
                            "Setup identity mismatch: the test "
                            "instantiates Account directly (new "
                            "Account("
                        )
                    ),
                    approve(thinking="looks fine on retry"),
                ],
            }
        )

        config = self._base_config(
            max_test_generation_attempts=2
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # (a) not frozen on the truncated attempt, but the contract
        # DOES eventually freeze once a real verdict comes in.
        self.assertIsNotNone(result)

        # (b) rejection_memory stayed empty: the follow-up clean
        # APPROVE required no confirmation call, so only 2 semantic
        # calls happened total (attempt 1 truncated, attempt 2
        # approve) — a 3rd call would mean confirmation triggered,
        # which only happens when rejection_memory is non-empty.
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 2
        )

        # (c) no revision was triggered by the truncated attempt:
        # only the initial generation call to the coder model.
        self.assertEqual(
            len(scripted.calls_for("mock-coder-model")), 1
        )

        events = self._history_events(config)
        self.assertFalse(
            any(e["event"] == "test_contract_rejected" for e in events)
        )
        self.assertTrue(
            any(e["event"] == "test_contract_approved" for e in events)
        )

        # Both attempts logged thinking (truncated attempt 1, clean
        # attempt 2) — the truncated one must be clearly tagged.
        reasoning = [
            e for e in events
            if e["event"] == "test_review_reasoning"
            and e["data"]["reviewer"] == "semantic"
        ]
        self.assertEqual(len(reasoning), 2)
        self.assertEqual(
            reasoning[0]["data"]["done_reason"], "length"
        )
        self.assertEqual(
            reasoning[1]["data"]["done_reason"], "stop"
        )

    def test_truncated_confirmation_is_retried_not_trusted(self):
        # Same guarantee, but for the confirmation call specifically:
        # REJECT -> revised -> APPROVE -> confirmation TRUNCATED ->
        # retried -> confirmation APPROVE -> freeze. The truncated
        # confirmation must not itself trigger a revision.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    reject(
                        "Withdraw_ReducesBalance_WhenSuccessful: "
                        "setup identity mismatch.",
                        thinking="first pass: caught the contradiction"
                    ),
                    approve(thinking="second pass: looks fine now"),
                    truncated_response(
                        '{"decision": "APPROVE", "issu',
                        thinking="confirmation pass: cut off"
                    ),
                    approve(thinking="confirmation retry: agrees"),
                ],
            }
        )

        config = self._base_config(
            max_test_generation_attempts=3
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)

        # Only 2 coder calls: initial generation + the ONE revision
        # after the genuine first REJECT. The truncated confirmation
        # must not have triggered a second revision.
        self.assertEqual(
            len(scripted.calls_for("mock-coder-model")), 2
        )

        events = self._history_events(config)
        confirmation_reasoning = [
            e for e in events
            if e["event"] == "test_review_reasoning"
            and e["data"]["reviewer"] == "semantic_confirmation"
        ]
        # Two confirmation attempts logged: the truncated one and the
        # successful retry.
        self.assertEqual(len(confirmation_reasoning), 2)
        self.assertEqual(
            confirmation_reasoning[0]["data"]["done_reason"], "length"
        )
        self.assertEqual(
            confirmation_reasoning[1]["data"]["done_reason"], "stop"
        )

        self.assertFalse(
            any(e["event"] == "test_contract_rejected" for e in events)
        )
        self.assertTrue(
            any(e["event"] == "test_contract_approved" for e in events)
        )

    def test_reviewer_calls_use_the_configured_output_budget(self):
        # Part A requirement 1: structural and semantic reviewer
        # calls must carry an explicit, configured num_ctx/num_predict
        # rather than relying on Ollama's small server default.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model":
                    approve(thinking="balance decreases as expected"),
            }
        )

        config = self._base_config(
            reviewer_context_size=12345,
            reviewer_output_tokens=678
        )

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        for call in (
            scripted.calls_for("mock-structural-model")
            + scripted.calls_for("mock-semantic-model")
        ):
            self.assertEqual(call["num_ctx"], 12345)
            self.assertEqual(call["num_predict"], 678)

    # -- Reviewer response schema validation / repair --------------------
    #
    # Regression coverage for the real-model smoke-test finding: a
    # complete, non-truncated response that is valid JSON but the wrong
    # top-level shape (e.g. a chat-style {"role", "content"} envelope
    # instead of {"decision", "issues"}).

    def test_A_valid_schema_response_skips_repair(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model":
                    approve(thinking="balance decreases as expected"),
            }
        )

        config = self._base_config(max_test_generation_attempts=8)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)

        # Exactly one call each — no repair call was ever made.
        self.assertEqual(
            len(scripted.calls_for("mock-structural-model")), 1
        )
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 1
        )

        events = self._history_events(config)
        self.assertFalse(
            any(e["event"] == "reviewer_schema_invalid" for e in events)
        )
        self.assertFalse(
            any(e["event"] == "reviewer_schema_repair" for e in events)
        )

    def test_B_malformed_envelope_triggers_exactly_one_repair_call(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    envelope_response(
                        "test_audit",
                        "## Audit\n...\nDecision: REJECT\nIssues: "
                        "[\"Balance should decrease\"]"
                    ),
                    reject(
                        "Balance should decrease by amount on a "
                        "successful Withdraw.",
                        thinking="repair: re-emitted in schema"
                    ),
                ],
            }
        )

        config = self._base_config(max_test_generation_attempts=1)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # Exactly 2 semantic calls: the original malformed one, and
        # exactly one repair call. Never more than one repair.
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 2
        )

        events = self._history_events(config)
        invalid_events = [
            e for e in events if e["event"] == "reviewer_schema_invalid"
        ]
        repair_events = [
            e for e in events if e["event"] == "reviewer_schema_repair"
        ]
        self.assertEqual(len(invalid_events), 1)
        self.assertEqual(invalid_events[0]["data"]["reviewer"], "semantic")
        self.assertEqual(len(repair_events), 1)
        self.assertEqual(repair_events[0]["data"]["outcome"], "ok")

    def test_B2_semantic_response_without_audit_is_never_repaired(self):
        # The evidence-first rule at phase level: a semantic response
        # carrying no audit is not a formatting problem, so repair is
        # not even attempted. Asking the model to reformat an audit it
        # never performed can only produce an invented one.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model":
                    envelope_response(
                        "test_audit",
                        "Decision: APPROVE",
                        audit=False
                    ),
            }
        )

        config = self._base_config(max_test_generation_attempts=1)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # One semantic call and no repair call.
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 1
        )

        events = self._history_events(config)

        self.assertEqual(
            [
                event
                for event in events
                if event["event"] == "reviewer_schema_repair"
            ],
            []
        )

        absent = [
            event
            for event in events
            if event["event"] == "reviewer_audit_absent"
        ]

        self.assertEqual(len(absent), 1)
        self.assertEqual(absent[0]["data"]["reviewer"], "semantic")

    def test_B3_bare_approve_never_freezes_a_contract(self):
        # The single most important regression: the verdict both models
        # defaulted to in the A/B evaluation must not be able to freeze
        # anything.
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        bare = {
            "ok": True,
            "response": json.dumps(
                {"decision": "APPROVE", "issues": []}
            ),
            "thinking": None,
            "done_reason": "stop",
            "truncated": False
        }

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": bare,
            }
        )

        config = self._base_config(max_test_generation_attempts=2)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        events = self._history_events(config)

        self.assertEqual(
            [
                event
                for event in events
                if event["event"] == "test_contract_frozen"
            ],
            []
        )

    def test_C_repair_recovers_reject_and_issues_flow_into_rejection_memory(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        distinctive_issue = (
            "DISTINCTIVE_REPAIRED_ISSUE: Balance should decrease "
            "by amount on a successful Withdraw."
        )

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(
                        LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION
                    ),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    envelope_response(
                        "test_audit",
                        f"reasoning... Decision: REJECT. "
                        f"Issue: {distinctive_issue}"
                    ),
                    reject(distinctive_issue),
                    approve(thinking="clean on the revised attempt"),
                ],
            }
        )

        config = self._base_config(max_test_generation_attempts=2)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # The revision call made after the repaired REJECT must carry
        # the repaired issue text — proving it reached
        # rejection_memory / the revision prompt normally, exactly as
        # a directly-well-formed REJECT would have.
        revision_prompt = (
            scripted.calls_for("mock-coder-model")[1]["prompt"]
        )
        self.assertIn(distinctive_issue, revision_prompt)

        # And the second attempt's review prompts must carry it under
        # Previously Raised Concerns, same as any other rejection.
        second_attempt_structural_prompt = (
            scripted.calls_for("mock-structural-model")[1]["prompt"]
        )
        self.assertIn(distinctive_issue, second_attempt_structural_prompt)

    def test_D_repair_recovers_approve_and_normal_approval_continues(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    envelope_response(
                        "test_audit",
                        "reasoning... Decision: APPROVE. No issues."
                    ),
                    approve(thinking="repair: re-emitted in schema"),
                ],
            }
        )

        config = self._base_config(max_test_generation_attempts=1)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)
        self.assertIn(
            "Withdraw_ReducesBalance_WhenSuccessful",
            result["frozen_tests"]["LedgerTests.cs"]
        )

        events = self._history_events(config)
        self.assertTrue(
            any(e["event"] == "test_contract_approved" for e in events)
        )
        # A clean first-time APPROVE via repair still needed no
        # confirmation call: only 2 semantic calls total (original +
        # repair), not 3.
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 2
        )

    def test_E_repair_still_invalid_never_approves_and_stores_no_partial_issues(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    envelope_response(
                        "test_audit", "first malformed answer"
                    ),
                    envelope_response(
                        "test_audit", "repair also malformed"
                    ),
                    approve(thinking="clean on the next attempt"),
                ],
            }
        )

        config = self._base_config(max_test_generation_attempts=2)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # Eventually approved on attempt 2 (clean APPROVE) — proving
        # the failed repair on attempt 1 didn't corrupt the run.
        self.assertIsNotNone(result)

        # Repair must NEVER be retried recursively: exactly 2 calls
        # for the failed attempt (original + one repair attempt),
        # plus 1 clean call on attempt 2 = 3 total.
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 3
        )

        # No revision was triggered by the schema-invalid attempt —
        # only the initial generation call to the coder model. A
        # schema-invalid verdict must never be treated as a REJECT
        # with issues to revise against.
        self.assertEqual(
            len(scripted.calls_for("mock-coder-model")), 1
        )

        repair_events = [
            e for e in self._history_events(config)
            if e["event"] == "reviewer_schema_repair"
        ]
        self.assertEqual(len(repair_events), 1)
        self.assertNotEqual(repair_events[0]["data"]["outcome"], "ok")

    def test_F_truncated_response_never_triggers_schema_repair(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    truncated_response(
                        '{"decision": "REJECT", "issu',
                        thinking="cut off mid-generation"
                    ),
                    approve(thinking="clean on retry"),
                ],
            }
        )

        config = self._base_config(max_test_generation_attempts=2)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        # Only 2 semantic calls total (truncated attempt 1 + clean
        # attempt 2) — a repair call would make this 3.
        self.assertEqual(
            len(scripted.calls_for("mock-semantic-model")), 2
        )

        events = self._history_events(config)
        self.assertFalse(
            any(e["event"] == "reviewer_schema_invalid" for e in events)
        )
        self.assertFalse(
            any(e["event"] == "reviewer_schema_repair" for e in events)
        )

    def test_G_invalid_decision_value_and_non_list_issues_trigger_repair(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    malformed_issues_type(),
                    reject(
                        "Balance should decrease.",
                        thinking="repair re-emitted correctly"
                    ),
                ],
            }
        )

        config = self._base_config(max_test_generation_attempts=1)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        events = self._history_events(config)
        invalid_events = [
            e for e in events if e["event"] == "reviewer_schema_invalid"
        ]
        self.assertEqual(len(invalid_events), 1)
        self.assertIn("issues", invalid_events[0]["data"]["reason"])

    def test_H_schema_repair_does_not_consume_attempt_slot(self):
        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model":
                    coder_returns(LEDGER_GOOD_SNIPPET),
                "mock-structural-model":
                    approve(thinking="setup looks fine"),
                "mock-semantic-model": [
                    envelope_response(
                        "test_audit", "reasoning... Decision: APPROVE."
                    ),
                    approve(thinking="repaired"),
                ],
            }
        )

        # Only 1 attempt allowed. If schema repair consumed its own
        # attempt slot, this would exhaust max_test_generation_attempts
        # before ever reaching a usable verdict and the contract would
        # fail to freeze. Since repair happens INSIDE the same attempt,
        # it succeeds within that single allowed attempt.
        config = self._base_config(max_test_generation_attempts=1)

        with mock.patch.object(
            test_contract_phase, "call_model", scripted
        ):
            result = test_contract_phase.run_test_contract_phase(
                config, self.workspace, LEDGER_TASK, {},
                [{"path": "Ledger.cs", "type": "implementation", "reason": "x"}],
                [{"path": "LedgerTests.cs", "type": "test", "reason": "x"}]
            )

        self.assertIsNotNone(result)
        # Exactly one structural call proves only one "Test snippet
        # attempt" iteration ran — the repair call did not need a
        # second one.
        self.assertEqual(
            len(scripted.calls_for("mock-structural-model")), 1
        )


class NormalizeReviewerDecisionTests(unittest.TestCase):
    # Scenario E: decision=APPROVE with non-empty issues is
    # self-contradictory and must be treated as REJECT.

    def test_approve_with_issues_is_treated_as_reject(self):
        decision, issues = test_contract_phase.normalize_reviewer_decision(
            {"decision": "APPROVE", "issues": ["problem"]}
        )
        self.assertEqual(decision, "REJECT")
        self.assertEqual(issues, ["problem"])

    def test_approve_with_no_issues_stays_approve(self):
        decision, issues = test_contract_phase.normalize_reviewer_decision(
            {"decision": "APPROVE", "issues": []}
        )
        self.assertEqual(decision, "APPROVE")
        self.assertEqual(issues, [])

    def test_reject_with_issues_stays_reject(self):
        decision, issues = test_contract_phase.normalize_reviewer_decision(
            {"decision": "REJECT", "issues": ["problem"]}
        )
        self.assertEqual(decision, "REJECT")
        self.assertEqual(issues, ["problem"])

    def test_missing_decision_field_is_not_approve(self):
        decision, issues = test_contract_phase.normalize_reviewer_decision(
            {"issues": []}
        )
        self.assertNotEqual(decision, "APPROVE")


class ValidateReviewerSchemaTests(unittest.TestCase):
    # A syntactically valid JSON object is not automatically a valid
    # reviewer verdict — pure-function coverage of the schema gate
    # itself, independent of the repair machinery.

    def test_well_formed_reject_is_valid(self):
        self.assertIsNone(
            test_contract_phase.validate_reviewer_schema(
                {"decision": "REJECT", "issues": ["x"]}
            )
        )

    def test_well_formed_approve_is_valid(self):
        self.assertIsNone(
            test_contract_phase.validate_reviewer_schema(
                {"decision": "APPROVE", "issues": []}
            )
        )

    def test_lowercase_decision_is_valid(self):
        self.assertIsNone(
            test_contract_phase.validate_reviewer_schema(
                {"decision": "reject", "issues": []}
            )
        )

    def test_chat_style_envelope_is_invalid(self):
        reason = test_contract_phase.validate_reviewer_schema(
            {"role": "test_audit", "content": "Decision: REJECT"}
        )
        self.assertIsNotNone(reason)

    def test_missing_decision_is_invalid(self):
        reason = test_contract_phase.validate_reviewer_schema(
            {"issues": []}
        )
        self.assertIsNotNone(reason)

    def test_invalid_decision_value_is_invalid(self):
        reason = test_contract_phase.validate_reviewer_schema(
            {"decision": "MAYBE", "issues": []}
        )
        self.assertIsNotNone(reason)

    def test_issues_not_a_list_is_invalid(self):
        reason = test_contract_phase.validate_reviewer_schema(
            {"decision": "REJECT", "issues": "not a list"}
        )
        self.assertIsNotNone(reason)

    def test_missing_issues_is_invalid(self):
        reason = test_contract_phase.validate_reviewer_schema(
            {"decision": "APPROVE"}
        )
        self.assertIsNotNone(reason)

    def test_non_dict_top_level_is_invalid(self):
        reason = test_contract_phase.validate_reviewer_schema(
            ["decision", "REJECT"]
        )
        self.assertIsNotNone(reason)


class CoerceOmittedIssuesTests(unittest.TestCase):
    # A bare {"decision": "APPROVE"} is a correct verdict in the wrong
    # shape. Coercion runs before the schema gate so the decision
    # survives; the gate itself stays strict.

    def test_bare_approve_gains_empty_issues(self):
        self.assertEqual(
            test_contract_phase.coerce_omitted_issues(
                {"decision": "APPROVE"}
            ),
            {"decision": "APPROVE", "issues": []}
        )

    def test_coerced_bare_approve_passes_the_schema_gate(self):
        self.assertIsNone(
            test_contract_phase.validate_reviewer_schema(
                test_contract_phase.coerce_omitted_issues(
                    {"decision": "APPROVE"}
                )
            )
        )

    def test_lowercase_bare_approve_is_coerced(self):
        self.assertEqual(
            test_contract_phase.coerce_omitted_issues(
                {"decision": "approve"}
            ),
            {"decision": "approve", "issues": []}
        )

    def test_bare_reject_is_left_alone(self):
        # A rejection with no issues has nothing for the revision
        # prompt to act on, so it must stay invalid.
        coerced = test_contract_phase.coerce_omitted_issues(
            {"decision": "REJECT"}
        )
        self.assertEqual(coerced, {"decision": "REJECT"})
        self.assertIsNotNone(
            test_contract_phase.validate_reviewer_schema(coerced)
        )

    def test_existing_issues_are_never_overwritten(self):
        self.assertEqual(
            test_contract_phase.coerce_omitted_issues(
                {"decision": "APPROVE", "issues": ["x"]}
            ),
            {"decision": "APPROVE", "issues": ["x"]}
        )

    def test_approve_with_non_list_issues_stays_invalid(self):
        coerced = test_contract_phase.coerce_omitted_issues(
            {"decision": "APPROVE", "issues": "not a list"}
        )
        self.assertIsNotNone(
            test_contract_phase.validate_reviewer_schema(coerced)
        )

    def test_input_is_not_mutated(self):
        original = {"decision": "APPROVE"}
        test_contract_phase.coerce_omitted_issues(original)
        self.assertEqual(original, {"decision": "APPROVE"})

    def test_chat_style_envelope_is_left_alone(self):
        self.assertEqual(
            test_contract_phase.coerce_omitted_issues(
                {"role": "test_audit", "content": "Decision: APPROVE"}
            ),
            {"role": "test_audit", "content": "Decision: APPROVE"}
        )

    def test_non_dict_is_returned_unchanged(self):
        self.assertEqual(
            test_contract_phase.coerce_omitted_issues(
                ["decision", "APPROVE"]
            ),
            ["decision", "APPROVE"]
        )

    def test_coerced_approval_normalizes_to_approve(self):
        decision, issues = (
            test_contract_phase.normalize_reviewer_decision(
                test_contract_phase.coerce_omitted_issues(
                    {"decision": "APPROVE"}
                )
            )
        )
        self.assertEqual(decision, "APPROVE")
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
