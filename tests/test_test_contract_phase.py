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


def approve(thinking=None):
    return {
        "ok": True,
        "response": json.dumps({"decision": "APPROVE", "issues": []}),
        "thinking": thinking
    }


def reject(issue, thinking=None):
    return {
        "ok": True,
        "response": json.dumps(
            {"decision": "REJECT", "issues": [issue]}
        ),
        "thinking": thinking
    }


def coder_returns(snippet):
    return {"ok": True, "response": snippet, "thinking": None}


class ScriptedCallModel:
    """Dispatches on model name; records every call made."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(
        self,
        config,
        model,
        prompt,
        json_mode=False,
        think=False
    ):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "json_mode": json_mode,
                "think": think
            }
        )

        return self.responses[model]

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


if __name__ == "__main__":
    unittest.main()
