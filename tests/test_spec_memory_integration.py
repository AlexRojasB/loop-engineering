"""
Integration coverage tying the pieces together inside the real
Test Contract phase:

- reviewer rejections are condensed into cross-attempt memory when the
  contract is finally abandoned;
- the NEXT outer attempt's generation/revision/review prompts carry that
  memory;
- the deterministic compilation gate short-circuits an invalid contract
  before either model reviewer is called (Problem 5's main saving);
- none of that leaks into a different work item.

Models are scripted; no Ollama and no toolchain are involved.
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
from core.spec_memory import (  # noqa: E402
    NO_MEMORY_TEXT,
    SpecFailureMemory,
    spec_scope_key,
)
from languages.dotnet import DotNetAdapter  # noqa: E402

from tests.fixtures.toy_domains import (  # noqa: E402
    LEDGER_ORIGINAL_TEST_FILE,
    LEDGER_PRODUCTION,
    LEDGER_TASK,
)
from tests.test_test_contract_phase import (  # noqa: E402
    ScriptedCallModel,
    approve,
    coder_returns,
    reject,
)


GOOD_SNIPPET = """
[Fact]
public void Withdraw_ReducesBalance_WhenSuccessful()
{
    var ledger = new Ledger();
    ledger.Open("A-1", 100);
    var account = ledger.Find("A-1");
    Assert.True(ledger.Withdraw(account.Id, 20));
}
"""

REVIEWER_ISSUE = (
    "Withdraw_ReducesBalance_WhenSuccessful: the setup treats "
    "Open's bool return value as an account identifier."
)


class StubAdapter(DotNetAdapter):
    def build_argv(self, workspace_files):
        return ["stub-build"]


def runner_returning(output):
    def run(workspace, argv):
        run.calls.append(argv)
        return {"exit_code": 1, "output": output}

    run.calls = []
    return run


CONVERSION_ERROR = (
    "/repo/LedgerTests.cs(9,30): error CS1503: Argument 1: "
    "cannot convert from 'bool' to 'System.Guid'"
)


class MemoryFlowsAcrossAttemptsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.workspace = self._tmp.name

        self._write("Ledger.cs", LEDGER_PRODUCTION)
        self._write("LedgerTests.cs", LEDGER_ORIGINAL_TEST_FILE)

        self.memory_file = (
            Path(self.workspace).parent
            / "integration-spec-memory.json"
        )

        self.scope = spec_scope_key(
            "work/queue/alpha.md",
            LEDGER_TASK
        )

        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(
            lambda: self.memory_file.unlink(missing_ok=True)
        )

    def _write(self, relative, content):
        (Path(self.workspace) / relative).write_text(content)

    def _config(self, memory=None, **overrides):
        config = {
            "coder_model": "mock-coder-model",
            "test_reviewer_model": "mock-structural-model",
            "semantic_reviewer_model": "mock-semantic-model",
            "max_test_generation_attempts": 1,
            "state_file":
                str(Path(self.workspace) / "state.json"),
            "history_file":
                str(Path(self.workspace) / "history.jsonl"),
        }

        if memory is not None:
            config["spec_memory"] = memory

        config.update(overrides)

        return config

    def _changes(self):
        return (
            [{"path": "Ledger.cs", "type": "implementation",
              "reasons": ["impl"]}],
            [{"path": "LedgerTests.cs", "type": "test",
              "reasons": ["tests"]}],
        )

    def _run(self, scripted, config):
        implementation_changes, test_changes = self._changes()

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            return test_contract_phase.run_test_contract_phase(
                config,
                self.workspace,
                LEDGER_TASK,
                {},
                implementation_changes,
                test_changes,
                config.get("_adapter"),
                []
            )

    # -- attempt 1 records, attempt 2 consumes -------------------------

    def test_rejected_contract_records_condensed_reviewer_issues(self):
        memory = SpecFailureMemory.load(
            self.memory_file,
            self.scope
        )

        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": reject(REVIEWER_ISSUE),
            }
        )

        result = self._run(
            scripted,
            self._config(memory=memory)
        )

        self.assertIsNone(result)

        text = memory.as_text()

        self.assertIn("structural", text)
        self.assertIn("Open's bool return value", text)

    def test_next_attempt_generation_prompt_carries_the_memory(self):
        memory = SpecFailureMemory.load(
            self.memory_file,
            self.scope
        )

        self._run(
            ScriptedCallModel(
                {
                    "mock-coder-model":
                        coder_returns(GOOD_SNIPPET),
                    "mock-structural-model":
                        reject(REVIEWER_ISSUE),
                }
            ),
            self._config(memory=memory)
        )

        # Repository restored; a brand new attempt reloads memory.
        reloaded = SpecFailureMemory.load(
            self.memory_file,
            self.scope
        )

        self.assertFalse(reloaded.is_empty)

        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        self._run(
            scripted,
            self._config(memory=reloaded)
        )

        generation_prompt = scripted.calls_for(
            "mock-coder-model"
        )[0]["prompt"]

        self.assertIn(
            "Open's bool return value",
            generation_prompt
        )

        reviewer_prompt = scripted.calls_for(
            "mock-structural-model"
        )[0]["prompt"]

        self.assertIn(
            "Open's bool return value",
            reviewer_prompt
        )

        semantic_prompt = scripted.calls_for(
            "mock-semantic-model"
        )[0]["prompt"]

        self.assertIn(
            "Open's bool return value",
            semantic_prompt
        )

    def test_a_different_work_item_sees_no_memory_in_its_prompts(self):
        memory = SpecFailureMemory.load(
            self.memory_file,
            self.scope
        )

        self._run(
            ScriptedCallModel(
                {
                    "mock-coder-model":
                        coder_returns(GOOD_SNIPPET),
                    "mock-structural-model":
                        reject(REVIEWER_ISSUE),
                }
            ),
            self._config(memory=memory)
        )

        other = SpecFailureMemory.load(
            self.memory_file,
            spec_scope_key(
                "work/queue/beta.md",
                "# Beta\n\n1. Something else.\n"
            )
        )

        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        self._run(
            scripted,
            self._config(memory=other)
        )

        for call in scripted.calls:
            self.assertNotIn(
                "Open's bool return value",
                call["prompt"]
            )

        self.assertIn(
            NO_MEMORY_TEXT,
            scripted.calls_for(
                "mock-coder-model"
            )[0]["prompt"]
        )

    def test_phase_runs_unchanged_without_any_memory_bound(self):
        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        result = self._run(scripted, self._config())

        self.assertIsNotNone(result)


class DeterministicGateShortCircuitsReviewersTests(unittest.TestCase):
    """
    Problem 5: the compile check must happen BEFORE the expensive
    semantic reviewer, not after it.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.workspace = self._tmp.name

        (Path(self.workspace) / "Ledger.cs").write_text(
            LEDGER_PRODUCTION
        )
        (Path(self.workspace) / "LedgerTests.cs").write_text(
            LEDGER_ORIGINAL_TEST_FILE
        )

        self.addCleanup(self._tmp.cleanup)

    def test_invalid_contract_never_reaches_a_model_reviewer(self):
        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        runner = runner_returning(CONVERSION_ERROR)

        config = {
            "coder_model": "mock-coder-model",
            "test_reviewer_model": "mock-structural-model",
            "semantic_reviewer_model": "mock-semantic-model",
            "max_test_generation_attempts": 2,
            "state_file":
                str(Path(self.workspace) / "state.json"),
            "history_file":
                str(Path(self.workspace) / "history.jsonl"),
            "contract_build_runner": runner,
        }

        with mock.patch.object(
            test_contract_phase,
            "call_model",
            scripted
        ):
            result = (
                test_contract_phase.run_test_contract_phase(
                    config,
                    self.workspace,
                    LEDGER_TASK,
                    {},
                    [{"path": "Ledger.cs",
                      "type": "implementation",
                      "reasons": ["impl"]}],
                    [{"path": "LedgerTests.cs",
                      "type": "test",
                      "reasons": ["tests"]}],
                    StubAdapter(),
                    []
                )
            )

        self.assertIsNone(result)

        self.assertEqual(
            scripted.calls_for("mock-structural-model"),
            []
        )
        self.assertEqual(
            scripted.calls_for("mock-semantic-model"),
            []
        )

        # And the compiler was consulted exactly once: the second
        # attempt reproduced the same contract and was recognised as a
        # repeat rather than recompiled.
        self.assertEqual(len(runner.calls), 1)

    def test_gate_rejection_reaches_the_revision_prompt(self):
        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        config = {
            "coder_model": "mock-coder-model",
            "test_reviewer_model": "mock-structural-model",
            "semantic_reviewer_model": "mock-semantic-model",
            "max_test_generation_attempts": 1,
            "state_file":
                str(Path(self.workspace) / "state.json"),
            "history_file":
                str(Path(self.workspace) / "history.jsonl"),
            "contract_build_runner":
                runner_returning(CONVERSION_ERROR),
        }

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
                [{"path": "Ledger.cs",
                  "type": "implementation",
                  "reasons": ["impl"]}],
                [{"path": "LedgerTests.cs",
                  "type": "test",
                  "reasons": ["tests"]}],
                StubAdapter(),
                []
            )

        revision_prompt = scripted.calls_for(
            "mock-coder-model"
        )[1]["prompt"]

        self.assertIn("CS1503", revision_prompt)

    def test_frozen_test_file_is_restored_after_gate_rejection(self):
        original = (
            Path(self.workspace) / "LedgerTests.cs"
        ).read_text()

        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        config = {
            "coder_model": "mock-coder-model",
            "test_reviewer_model": "mock-structural-model",
            "semantic_reviewer_model": "mock-semantic-model",
            "max_test_generation_attempts": 1,
            "state_file":
                str(Path(self.workspace) / "state.json"),
            "history_file":
                str(Path(self.workspace) / "history.jsonl"),
            "contract_build_runner":
                runner_returning(CONVERSION_ERROR),
        }

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
                [{"path": "Ledger.cs",
                  "type": "implementation",
                  "reasons": ["impl"]}],
                [{"path": "LedgerTests.cs",
                  "type": "test",
                  "reasons": ["tests"]}],
                StubAdapter(),
                []
            )

        self.assertEqual(
            (Path(self.workspace) / "LedgerTests.cs").read_text(),
            original
        )

    def test_gate_records_its_verdict_in_history(self):
        scripted = ScriptedCallModel(
            {
                "mock-coder-model": coder_returns(GOOD_SNIPPET),
                "mock-structural-model": approve(),
                "mock-semantic-model": approve(),
            }
        )

        history_file = Path(self.workspace) / "history.jsonl"

        config = {
            "coder_model": "mock-coder-model",
            "test_reviewer_model": "mock-structural-model",
            "semantic_reviewer_model": "mock-semantic-model",
            "max_test_generation_attempts": 1,
            "state_file":
                str(Path(self.workspace) / "state.json"),
            "history_file": str(history_file),
            "contract_build_runner":
                runner_returning(CONVERSION_ERROR),
        }

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
                [{"path": "Ledger.cs",
                  "type": "implementation",
                  "reasons": ["impl"]}],
                [{"path": "LedgerTests.cs",
                  "type": "test",
                  "reasons": ["tests"]}],
                StubAdapter(),
                []
            )

        events = [
            json.loads(line)
            for line in history_file.read_text().splitlines()
            if line.strip()
        ]

        verdicts = [
            event["data"]["verdict"]
            for event in events
            if event["event"] == "contract_compilation_checked"
        ]

        self.assertEqual(verdicts, ["INVALID"])


if __name__ == "__main__":
    unittest.main()
