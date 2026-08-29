"""
Regression coverage for the implementation-time frozen-contract
challenge.

The evidence is Ledger Full #2 (run-ledger-full-002.txt). Spec 001 froze
a contract that compiled and was approved by both the structural and the
semantic reviewer, yet was semantically impossible:

    var account = new Account("Checking", 100m);
    service.CreateAccount(account.Name, account.Balance);
    Assert.True(service.Deposit(account.Id, 50m));

`CreateAccount` registers a DIFFERENT instance with a fresh identifier,
so `account.Id` names an object `LedgerService` was never given. The
implementation agent stated this correctly at agentic step 6 of 31
("Step 3 adds a DIFFERENT Account with a DIFFERENT GUID"), then spent 25
more steps and ~3135 seconds unable to act on its own diagnosis, because
frozen tests are immutable and it had no other move.

What is pinned here:

- the agent still cannot touch a frozen test; it can only file a report;
- a report never invalidates a contract by itself;
- validation is deterministic first (provenance + reproduction) and
  independently reviewed second, and fails closed everywhere;
- everything is bounded: submissions, reviews, and contract reopenings;
- a confirmed defect reaches cross-attempt memory and the disproved
  contract cannot be frozen again;
- the identity contradiction above now exits implementation in a handful
  of steps instead of consuming the whole step budget.

No Ollama and no .NET toolchain: models are scripted and the language
adapter is a small Python stand-in.
"""

import json
import subprocess
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
    CHALLENGE_KINDS,
    FROZEN_TEST_COMPILATION,
    MAX_FIELD_CHARS,
    challenge_fingerprint,
    challenge_memory_entry,
    evidence_gate,
    normalize_challenge,
    validate_challenge,
)
from core.phases import agentic_implementation_phase as impl  # noqa: E402
from core.repository import git_status, read_file  # noqa: E402
from core.spec_memory import (  # noqa: E402
    SpecFailureMemory,
    spec_scope_key,
)
from core.state import read_history  # noqa: E402


# ---------------------------------------------------------------------
# The real Ledger Full #2 material, reduced to what matters
# ---------------------------------------------------------------------

TASK = """
# Deposit Funds

Depositing a positive amount into an existing account increases its
balance and returns true. A zero or negative amount returns false.
"""

PRODUCTION = """
public class LedgerService
{
    private readonly List<Account> _accounts = new();

    public bool CreateAccount(string name, decimal balance)
    {
        _accounts.Add(new Account(name, balance));
        return true;
    }
}
"""

FROZEN_CONTRACT = """
public class LedgerServiceTests
{
    [Fact]
    public void Deposit_WithValidAccountAndAmount_ReturnsTrue()
    {
        var service = new LedgerService();
        var account = new Account("Checking", 100m);
        service.CreateAccount(account.Name, account.Balance);
        Assert.True(service.Deposit(account.Id, 50m));
    }
}
"""

FAILING_TEST = "Deposit_WithValidAccountAndAmount_ReturnsTrue"

PRODUCTION_QUOTE = "_accounts.Add(new Account(name, balance));"

CONTRADICTION = (
    "CreateAccount registers a new Account instance with its own "
    "identifier, so account.Id from the directly constructed instance "
    "is never present in _accounts and Deposit can never find it."
)


def challenge_args(**overrides):
    args = {
        "kind": "object_identity",
        "summary":
            "The test deposits into an id the service was never given.",
        "failing_tests": [FAILING_TEST],
        "authoritative_requirement":
            "Depositing a positive amount into an existing account "
            "increases its balance and returns true.",
        "production_path": "LedgerService.cs",
        "production_quote": PRODUCTION_QUOTE,
        "contradiction": CONTRADICTION,
    }

    args.update(overrides)

    return args


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------

class FakeAdapter:
    """
    Stands in for a LanguageAdapter without requiring a toolchain.
    Supports filtered test execution, like the .NET adapter does.
    """

    name = "fake"

    def build_argv(self, workspace_files):
        return [sys.executable, "-c", "print('BUILD_OK')"]

    def test_argv(self, workspace_files, filter=None):
        argv = [
            sys.executable,
            "-c",
            "import sys; print('TESTS'); sys.exit(1)"
        ]

        if filter:
            argv = argv + [filter]

        return argv

    def is_test_path(self, path):
        lower = path.lower()

        return (
            "test" in lower
            or "spec" in lower
        )


class NoTestSupportAdapter(FakeAdapter):
    """An adapter that cannot execute tests at all."""

    def test_argv(self, workspace_files, filter=None):
        return None


class NoFilterAdapter(FakeAdapter):
    """
    The real LanguageAdapter default (languages/base.py): tests run, but
    a single test cannot be selected. Every non-.NET adapter behaves
    this way until it implements filtering.
    """

    def test_argv(self, workspace_files, filter=None):
        if filter:
            return None

        return super().test_argv(
            workspace_files
        )


BUILD_MARKER = "BUILD_OK"

DEFAULT_FAILURE = (
    "  Failed LedgerServiceTests."
    "Deposit_WithValidAccountAndAmount_ReturnsTrue\n"
    "  Assert.True() Failure\n"
    "  Expected: True\n"
    "  Actual:   False"
)


def runner(
    exit_code,
    output=None,
    build_exit_code=0,
    build_output="Build succeeded."
):
    """
    Stands in for the toolchain. The evidence gate builds first and then
    runs tests, so the stub has to answer both; `calls` records only the
    test invocations, which is what the assertions care about.
    """

    if output is None:
        output = DEFAULT_FAILURE

    def run(workspace, argv, timeout=None):
        is_build = BUILD_MARKER in " ".join(
            str(part)
            for part in argv
        )

        if is_build:
            run.build_calls.append(argv)

            return {
                "exit_code": build_exit_code,
                "output": build_output
            }

        run.calls.append(argv)

        return {
            "exit_code": exit_code,
            "output": output
        }

    run.calls = []
    run.build_calls = []

    return run


def model_response(payload, done_reason="stop", ok=True):
    return {
        "ok": ok,
        "response": json.dumps(payload) if payload is not None else "",
        "thinking": None,
        "done_reason": done_reason,
        "truncated": done_reason == "length",
        "error": None if ok else "boom"
    }


def confirm(reason="Identity contradiction confirmed."):
    return model_response(
        {
            "decision": "CONFIRM",
            "reasons": [reason]
        }
    )


def reject_verdict(reason="Feature is simply unimplemented."):
    return model_response(
        {
            "decision": "REJECT",
            "reasons": [reason]
        }
    )


class ScriptedReviewer:
    """Consumes scripted verdicts in order; repeats the last one."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    def __call__(
        self,
        config,
        model,
        prompt,
        json_mode=False,
        think=False,
        num_ctx=None,
        num_predict=None,
        reduced_prompt=None
    ):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "think": think
            }
        )

        if len(self.verdicts) > 1:
            return self.verdicts.pop(0)

        return self.verdicts[0]


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.workspace = self._tmp.name

        self._write("LedgerService.cs", PRODUCTION)
        self._write("LedgerServiceTests.cs", FROZEN_CONTRACT)

        self.frozen_tests = {
            "LedgerServiceTests.cs": FROZEN_CONTRACT
        }

        self.production = {
            "LedgerService.cs": PRODUCTION
        }

    def _write(self, relative, content):
        path = Path(self.workspace) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def _config(self, **overrides):
        config = {
            "semantic_reviewer_model": "mock-reviewer",
            "state_file":
                str(
                    Path(self.workspace).parent
                    / "state.json"
                ),
            "history_file":
                str(
                    Path(self.workspace).parent
                    / "history.jsonl"
                ),
        }

        config.update(overrides)

        return config


# ---------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------

class ChallengeSchemaTests(unittest.TestCase):
    def test_well_formed_challenge_is_accepted(self):
        challenge, error = normalize_challenge(
            challenge_args()
        )

        self.assertIsNone(error)
        self.assertEqual(
            challenge["kind"],
            "object_identity"
        )
        self.assertEqual(
            challenge["failing_tests"],
            [FAILING_TEST]
        )

    def test_kind_is_a_closed_set(self):
        challenge, error = normalize_challenge(
            challenge_args(kind="i_dont_like_it")
        )

        self.assertIsNone(challenge)
        self.assertIn("kind", error)

        # `frozen_test_compilation` carries compiler diagnostics
        # instead of a production quote, so it has its own required
        # fields and its own coverage below.
        for kind in CHALLENGE_KINDS:
            if kind == FROZEN_TEST_COMPILATION:
                continue

            _, error = normalize_challenge(
                challenge_args(kind=kind)
            )
            self.assertIsNone(error)

    def test_every_required_field_is_required(self):
        for field in (
            "summary",
            "contradiction",
            "authoritative_requirement",
            "production_path",
            "production_quote",
        ):
            challenge, error = normalize_challenge(
                challenge_args(**{field: ""})
            )

            self.assertIsNone(
                challenge,
                f"{field} was not required"
            )
            self.assertIn(field, error)

    def test_at_least_one_frozen_test_must_be_cited(self):
        challenge, error = normalize_challenge(
            challenge_args(failing_tests=[])
        )

        self.assertIsNone(challenge)
        self.assertIn(
            "at least one frozen test",
            error.lower()
        )

    def test_hand_waving_contradiction_is_rejected(self):
        challenge, error = normalize_challenge(
            challenge_args(contradiction="impossible")
        )

        self.assertIsNone(challenge)
        self.assertIn("contradiction", error)

    def test_fields_are_length_bounded(self):
        challenge, error = normalize_challenge(
            challenge_args(
                contradiction="x" * 100000
            )
        )

        self.assertIsNone(error)
        self.assertLessEqual(
            len(challenge["contradiction"]),
            MAX_FIELD_CHARS
        )

    def test_cited_test_list_is_count_bounded(self):
        challenge, error = normalize_challenge(
            challenge_args(
                failing_tests=[
                    f"Test{index}"
                    for index in range(50)
                ]
            )
        )

        self.assertIsNone(error)
        self.assertLessEqual(
            len(challenge["failing_tests"]),
            5
        )

    def test_non_object_arguments_are_rejected(self):
        challenge, error = normalize_challenge(
            "just a string"
        )

        self.assertIsNone(challenge)
        self.assertIn("object", error)

    def test_same_claim_has_the_same_fingerprint(self):
        first, _ = normalize_challenge(
            challenge_args()
        )

        second, _ = normalize_challenge(
            challenge_args(
                summary="Reworded summary entirely."
            )
        )

        third, _ = normalize_challenge(
            challenge_args(
                contradiction=CONTRADICTION
                + " And also something else entirely."
            )
        )

        self.assertEqual(
            challenge_fingerprint(first),
            challenge_fingerprint(second)
        )

        self.assertNotEqual(
            challenge_fingerprint(first),
            challenge_fingerprint(third)
        )


# ---------------------------------------------------------------------
# Deterministic evidence gate
# ---------------------------------------------------------------------

class EvidenceGateTests(WorkspaceCase):
    def _gate(self, args=None, adapter=None, test_runner=None):
        challenge, error = normalize_challenge(
            args or challenge_args()
        )

        self.assertIsNone(error)

        return evidence_gate(
            self.workspace,
            challenge,
            self.frozen_tests,
            adapter or FakeAdapter(),
            [],
            read_file,
            runner=test_runner or runner(1)
        )

    def test_reproduced_failure_with_real_evidence_is_admissible(self):
        gate = self._gate()

        self.assertTrue(gate["ok"])
        self.assertIsNone(gate["reason"])

    def test_invented_test_name_is_rejected(self):
        gate = self._gate(
            challenge_args(
                failing_tests=[
                    "Deposit_SomethingIMadeUp"
                ]
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "do not appear in the frozen contract",
            gate["reason"]
        )

    def test_unreadable_production_path_is_rejected(self):
        gate = self._gate(
            challenge_args(
                production_path="NotHere.cs"
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "could not be read",
            gate["reason"]
        )

    def test_fabricated_production_quote_is_rejected(self):
        """
        Provenance: evidence must be copied from real repository
        content, so a plausible-sounding invention cannot buy a
        reviewer call.
        """

        gate = self._gate(
            challenge_args(
                production_quote=
                    "_accounts.Add(account); // reuses caller's id"
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "does not occur in",
            gate["reason"]
        )

    def test_quote_matching_is_whitespace_insensitive(self):
        gate = self._gate(
            challenge_args(
                production_quote=
                    "_accounts.Add(new    Account(name,\n balance));"
            )
        )

        self.assertTrue(gate["ok"])

    def test_passing_test_cannot_be_challenged(self):
        gate = self._gate(
            test_runner=runner(0, "Passed! 1 test")
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "currently PASSES",
            gate["reason"]
        )

    def test_missing_frozen_contract_cannot_be_challenged(self):
        challenge, _ = normalize_challenge(
            challenge_args()
        )

        gate = evidence_gate(
            self.workspace,
            challenge,
            {},
            FakeAdapter(),
            [],
            read_file,
            runner=runner(1)
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "nothing to",
            gate["reason"]
        )

    def test_adapter_that_cannot_run_tests_fails_closed(self):
        gate = self._gate(
            adapter=NoTestSupportAdapter()
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "cannot execute",
            gate["reason"]
        )

    def test_runner_explosion_fails_closed(self):
        def exploding(workspace, argv, timeout=None):
            raise OSError("no toolchain")

        gate = self._gate(
            test_runner=exploding
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "could not be run",
            gate["reason"]
        )

    def test_broken_build_is_not_evidence_about_the_contract(self):
        """
        The agent's own half-written production code makes the whole
        suite red. Without this gate, that breakage would satisfy a
        naive "the tests fail" check and be handed to the adjudicator
        as reproduced evidence.
        """

        gate = self._gate(
            test_runner=runner(
                1,
                build_exit_code=1,
                build_output="error CS1002: ; expected"
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "does not currently build",
            gate["reason"]
        )

    def test_failure_must_name_the_cited_test(self):
        """
        Attribution: a red suite is not evidence about one test. During
        a test-first implementation phase the suite is red by
        construction.
        """

        gate = self._gate(
            test_runner=runner(
                1,
                output="  Failed SomeOtherTest\n  Assert failure"
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "does not name the cited test",
            gate["reason"]
        )

    def test_unfilterable_runner_needs_the_test_named_in_output(self):
        """
        languages/base.py cannot select a single test, so every adapter
        without filter support falls back to the whole suite. That
        fallback is only admissible when the output identifies the
        cited test.
        """

        vague = self._gate(
            adapter=NoFilterAdapter(),
            test_runner=runner(
                1,
                output="Failed! - Failed: 6, Passed: 4"
            )
        )

        self.assertFalse(vague["ok"])
        self.assertIn(
            "could not be scoped",
            vague["reason"]
        )

        attributable = self._gate(
            adapter=NoFilterAdapter(),
            test_runner=runner(1)
        )

        self.assertTrue(attributable["ok"])

    def test_test_file_cannot_be_cited_as_production_evidence(self):
        gate = self._gate(
            challenge_args(
                production_path="LedgerServiceTests.cs",
                production_quote=
                    "Assert.True(service.Deposit(account.Id, 50m));"
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "frozen test file",
            gate["reason"]
        )

    def test_other_test_files_are_also_refused(self):
        self._write(
            "OtherTests.cs",
            "// helper assertions live here for a while\n"
        )

        gate = self._gate(
            challenge_args(
                production_path="OtherTests.cs",
                production_quote=
                    "helper assertions live here for a while"
            )
        )

        self.assertFalse(gate["ok"])
        self.assertIn(
            "is a test file",
            gate["reason"]
        )

    def test_build_runs_before_the_tests(self):
        test_runner = runner(1)

        self._gate(test_runner=test_runner)

        self.assertEqual(
            len(test_runner.build_calls),
            1
        )

    def test_reproduction_uses_the_cited_test_as_a_filter(self):
        test_runner = runner(1)

        self._gate(test_runner=test_runner)

        self.assertEqual(
            len(test_runner.calls),
            1
        )
        self.assertIn(
            FAILING_TEST,
            test_runner.calls[0]
        )


# ---------------------------------------------------------------------
# Independent validation
# ---------------------------------------------------------------------

class IndependentValidationTests(WorkspaceCase):
    def _validate(
        self,
        verdicts,
        config=None,
        allow_review=True
    ):
        challenge, error = normalize_challenge(
            challenge_args()
        )

        self.assertIsNone(error)

        reviewer = ScriptedReviewer(verdicts)

        with mock.patch.object(
            contract_challenge,
            "call_model",
            reviewer
        ):
            verdict = validate_challenge(
                config or self._config(),
                self.workspace,
                challenge,
                TASK,
                self.frozen_tests,
                self.production,
                FakeAdapter(),
                [],
                read_file,
                runner=runner(1),
                allow_review=allow_review
            )

        return verdict, reviewer

    def test_two_independent_confirmations_confirm_the_challenge(self):
        verdict, reviewer = self._validate(
            [confirm(), confirm()]
        )

        self.assertTrue(verdict["confirmed"])
        self.assertEqual(verdict["stage"], "review")
        self.assertEqual(len(reviewer.calls), 2)

    def test_one_confirmation_alone_is_not_enough(self):
        """
        Same rule the Test Contract phase already applies before
        freezing: a single stochastic verdict never moves a gate. Here
        the asymmetry is sharper, because a wrongly confirmed challenge
        discards a contract two reviewers already approved.
        """

        verdict, reviewer = self._validate(
            [confirm(), reject_verdict()]
        )

        self.assertFalse(verdict["confirmed"])
        self.assertEqual(len(reviewer.calls), 2)

    def test_first_rejection_short_circuits(self):
        verdict, reviewer = self._validate(
            [reject_verdict(), confirm()]
        )

        self.assertFalse(verdict["confirmed"])
        self.assertEqual(len(reviewer.calls), 1)

    def test_truncated_verdict_fails_closed(self):
        verdict, _ = self._validate(
            [
                model_response(
                    {
                        "decision": "CONFIRM",
                        "reasons": ["partial"]
                    },
                    done_reason="length"
                )
            ]
        )

        self.assertFalse(verdict["confirmed"])
        self.assertIn(
            "truncated",
            " ".join(verdict["reasons"]).lower()
        )

    def test_unparseable_verdict_fails_closed(self):
        verdict, _ = self._validate(
            [
                {
                    "ok": True,
                    "response": "I think the test is fine, actually",
                    "thinking": None,
                    "done_reason": "stop",
                    "truncated": False,
                    "error": None
                }
            ]
        )

        self.assertFalse(verdict["confirmed"])

    def test_wrong_schema_fails_closed(self):
        verdict, _ = self._validate(
            [
                model_response(
                    {
                        "role": "assistant",
                        "content": "CONFIRM"
                    }
                )
            ]
        )

        self.assertFalse(verdict["confirmed"])

    def test_failed_reviewer_call_fails_closed(self):
        verdict, _ = self._validate(
            [
                model_response(
                    None,
                    ok=False
                )
            ]
        )

        self.assertFalse(verdict["confirmed"])

    def test_no_reviewer_model_fails_closed(self):
        verdict, reviewer = self._validate(
            [confirm(), confirm()],
            config=self._config(
                semantic_reviewer_model=None,
                test_reviewer_model=None
            )
        )

        self.assertFalse(verdict["confirmed"])
        self.assertEqual(len(reviewer.calls), 0)

    def test_review_budget_exhaustion_fails_closed(self):
        verdict, reviewer = self._validate(
            [confirm(), confirm()],
            allow_review=False
        )

        self.assertFalse(verdict["confirmed"])
        self.assertEqual(verdict["stage"], "budget")
        self.assertEqual(len(reviewer.calls), 0)

    def test_bad_evidence_never_reaches_a_reviewer(self):
        challenge, _ = normalize_challenge(
            challenge_args(
                production_quote="fabricated evidence text"
            )
        )

        reviewer = ScriptedReviewer([confirm()])

        with mock.patch.object(
            contract_challenge,
            "call_model",
            reviewer
        ):
            verdict = validate_challenge(
                self._config(),
                self.workspace,
                challenge,
                TASK,
                self.frozen_tests,
                self.production,
                FakeAdapter(),
                [],
                read_file,
                runner=runner(1)
            )

        self.assertFalse(verdict["confirmed"])
        self.assertEqual(verdict["stage"], "evidence")
        self.assertEqual(len(reviewer.calls), 0)

    def test_reviewer_prompt_carries_the_real_material(self):
        _, reviewer = self._validate(
            [confirm(), confirm()]
        )

        prompt = reviewer.calls[0]["prompt"]

        self.assertIn(FAILING_TEST, prompt)
        self.assertIn(PRODUCTION_QUOTE, prompt)
        self.assertIn("Deposit Funds", prompt)
        self.assertIn(
            "is NEVER a defect",
            prompt
        )

    def test_validation_is_recorded_in_history(self):
        config = self._config()

        self._validate(
            [confirm(), confirm()],
            config=config
        )

        events = [
            event["event"]
            for event in read_history(config)
        ]

        self.assertIn(
            "contract_challenge_evidence",
            events
        )
        self.assertIn(
            "contract_challenge_reviewed",
            events
        )

        Path(
            config["history_file"]
        ).unlink(missing_ok=True)


class AdjudicatorPromptTests(unittest.TestCase):
    """
    The adjudicator prompt is load-bearing, and each rule below exists
    because a real reviewer call got the answer wrong without it (see
    tests/manual_eval_contract_challenge.py for the measurements).

    Whether a given local model acts on this guidance is not something a
    deterministic test can prove; that the guidance is present, and that
    the template renders against C# braces, is.
    """

    def setUp(self):
        from core.prompts import load_prompt

        self.prompt = load_prompt(
            "contract-challenge-reviewer.md"
        )

    def test_unimplemented_is_not_a_defect(self):
        """The rule that stops a valid contract being discarded."""

        self.assertIn(
            "is NEVER a defect",
            self.prompt
        )

    def test_contract_is_defined_as_the_test_code(self):
        """
        Without this, a reviewer derives the contradiction and still
        votes REJECT, reasoning that "the tests are defective" is a
        different claim from "the contract is defective" and that some
        other channel should be used. There is no other channel.
        """

        lowered = self.prompt.lower()

        self.assertIn(
            "the frozen test code",
            lowered
        )

        self.assertIn(
            "the same verdict",
            lowered
        )

    def test_per_instance_identity_guidance_is_present(self):
        lowered = self.prompt.lower()

        self.assertIn(
            "per instance",
            lowered
        )

        self.assertIn(
            "do not share it",
            lowered
        )

    def test_hedged_rejections_are_forbidden(self):
        lowered = self.prompt.lower()

        self.assertIn(
            "do not hedge",
            lowered
        )

        self.assertIn(
            "unless",
            lowered
        )

    def test_unseen_code_is_not_a_licence_to_guess(self):
        self.assertIn(
            "AS SHOWN",
            self.prompt
        )

    def test_output_contract_precedes_the_procedure(self):
        """
        A smaller reviewer that reasons at length in the response breaks
        the schema, and a schema-invalid verdict fails closed -- which
        silently disables the whole mechanism. The output contract has
        to be read first.
        """

        self.assertLess(
            self.prompt.index("# Output"),
            self.prompt.index("# Procedure")
        )

    def test_prompt_renders_against_real_csharp(self):
        from core.contract_challenge import (
            challenge_review_prompt,
        )

        challenge, error = normalize_challenge(
            challenge_args()
        )

        self.assertIsNone(error)

        rendered = challenge_review_prompt(
            TASK,
            {"LedgerServiceTests.cs": FROZEN_CONTRACT},
            {"LedgerService.cs": PRODUCTION},
            challenge,
            "exit_code=1\nAssert.True() Failure"
        )

        self.assertIn(
            "_accounts.Add(new Account(name, balance));",
            rendered
        )

        # The JSON examples must survive str.format doubling.
        self.assertIn(
            '{"decision": "CONFIRM"',
            rendered
        )

        self.assertNotIn(
            "{{",
            rendered
        )

    def test_ledger_specific_language_has_not_leaked_in(self):
        lowered = self.prompt.lower()

        for term in (
            "ledger",
            "deposit",
            "account",
            "guid",
            "inventory",
        ):
            self.assertNotIn(
                term,
                lowered,
                f"prompt leaked benchmark-specific term: {term}"
            )


# ---------------------------------------------------------------------
# Implementation phase behaviour
# ---------------------------------------------------------------------

def assistant(tool_calls=None, content=""):
    message = {
        "role": "assistant",
        "content": content
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    return {"message": message}


def tool_call(name, arguments):
    return {
        "function": {
            "name": name,
            "arguments": arguments
        }
    }


IMPLEMENTATION = """
public class LedgerService
{
    private readonly List<Account> _accounts = new();

    public bool CreateAccount(string name, decimal balance)
    {
        _accounts.Add(new Account(name, balance));
        return true;
    }

    public bool Deposit(Guid id, decimal amount) => false;
}
"""


class ImplementationPhaseChallengeTests(WorkspaceCase):
    """
    The phase-level contract: how a challenge interacts with the loop,
    the frozen tests, and the step budget.
    """

    def _run(
        self,
        script,
        verdicts=None,
        config_overrides=None,
        frozen_tests=None
    ):
        state = {}

        overrides = {
            "agentic_max_steps": 12
        }

        overrides.update(
            config_overrides or {}
        )

        config = self._config(
            **overrides
        )

        scripted = list(script)
        calls = {"model": 0}

        def call_model(
            model,
            url,
            context_size,
            messages,
            tools=None
        ):
            calls["model"] += 1
            calls["tools"] = tools

            if scripted:
                return scripted.pop(0)

            return assistant()

        reviewer = ScriptedReviewer(
            verdicts or [confirm(), confirm()]
        )

        with mock.patch.object(
            impl,
            "_call_model",
            call_model
        ), mock.patch.object(
            contract_challenge,
            "call_model",
            reviewer
        ):
            outcome = impl.run_agentic_implementation_phase(
                config,
                self.workspace,
                TASK,
                state,
                [
                    {
                        "path": "LedgerService.cs",
                        "type": "implementation",
                        "reasons": ["deposit"]
                    }
                ],
                "build",
                "test",
                FakeAdapter(),
                [],
                None,
                frozen_tests=(
                    self.frozen_tests
                    if frozen_tests is None
                    else frozen_tests
                ),
                challenge_runner=runner(1)
            )

        self.addCleanup(
            lambda: Path(
                config["history_file"]
            ).unlink(missing_ok=True)
        )

        self.addCleanup(
            lambda: Path(
                config["state_file"]
            ).unlink(missing_ok=True)
        )

        return outcome, reviewer, calls, config

    # -- the shape of the escape hatch ---------------------------------

    def test_tool_is_offered_only_when_a_contract_exists(self):
        _, _, calls, _ = self._run(
            [assistant()],
            frozen_tests={}
        )

        names = [
            tool["function"]["name"]
            for tool in calls["tools"]
        ]

        self.assertNotIn(
            impl.CHALLENGE_TOOL,
            names
        )

        _, _, calls, _ = self._run(
            [assistant()]
        )

        names = [
            tool["function"]["name"]
            for tool in calls["tools"]
        ]

        self.assertIn(
            impl.CHALLENGE_TOOL,
            names
        )

    def test_challenge_tool_can_be_disabled(self):
        _, _, calls, _ = self._run(
            [assistant()],
            config_overrides={
                "contract_challenge_enabled": False
            }
        )

        names = [
            tool["function"]["name"]
            for tool in calls["tools"]
        ]

        self.assertNotIn(
            impl.CHALLENGE_TOOL,
            names
        )

    # -- the Ledger scenario -------------------------------------------

    def _ledger_script(self):
        return [
            assistant(
                [
                    tool_call(
                        "write_file",
                        {
                            "path": "LedgerService.cs",
                            "content": IMPLEMENTATION
                        }
                    )
                ]
            ),
            assistant(
                [
                    tool_call(
                        "run_operation",
                        {"operation": "test"}
                    )
                ]
            ),
            assistant(
                [
                    tool_call(
                        impl.CHALLENGE_TOOL,
                        challenge_args()
                    )
                ]
            ),
        ]

    def test_confirmed_identity_contradiction_stops_implementation(self):
        outcome, reviewer, calls, config = self._run(
            self._ledger_script()
        )

        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )

        self.assertEqual(
            outcome["challenge"]["kind"],
            "object_identity"
        )

        # This is the whole point: three agentic steps, not thirty-one.
        self.assertEqual(outcome["steps"], 3)
        self.assertEqual(calls["model"], 3)

        self.assertEqual(len(reviewer.calls), 2)

    def test_confirmed_challenge_reaches_cross_attempt_memory(self):
        memory = SpecFailureMemory(
            scope=spec_scope_key(
                "specs/001-deposit.md",
                TASK
            )
        )

        outcome, _, _, config = self._run(
            self._ledger_script(),
            config_overrides={
                "spec_memory": memory
            }
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
        self.assertIn(
            FAILING_TEST,
            joined
        )

    def test_frozen_tests_are_never_modified_by_a_challenge(self):
        script = self._ledger_script()

        script.insert(
            0,
            assistant(
                [
                    tool_call(
                        "write_file",
                        {
                            "path": "LedgerServiceTests.cs",
                            "content": "// rewritten by the agent"
                        }
                    )
                ]
            )
        )

        outcome, _, _, _ = self._run(script)

        self.assertEqual(
            (
                Path(self.workspace)
                / "LedgerServiceTests.cs"
            ).read_text(),
            FROZEN_CONTRACT
        )

        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )

    # -- fail-closed / anti-abuse --------------------------------------

    def test_challenge_before_any_implementation_is_refused(self):
        outcome, reviewer, _, _ = self._run(
            [
                assistant(
                    [
                        tool_call(
                            impl.CHALLENGE_TOOL,
                            challenge_args()
                        )
                    ]
                ),
                assistant(),
            ]
        )

        self.assertNotEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )

        self.assertEqual(
            len(reviewer.calls),
            0,
            "an unimplemented feature reached a reviewer"
        )

    def test_challenge_before_running_tests_is_refused(self):
        outcome, reviewer, _, _ = self._run(
            [
                assistant(
                    [
                        tool_call(
                            "write_file",
                            {
                                "path": "LedgerService.cs",
                                "content": IMPLEMENTATION
                            }
                        )
                    ]
                ),
                assistant(
                    [
                        tool_call(
                            impl.CHALLENGE_TOOL,
                            challenge_args()
                        )
                    ]
                ),
                assistant(),
            ]
        )

        self.assertNotEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )
        self.assertEqual(len(reviewer.calls), 0)

    def test_premature_refusal_does_not_lock_out_a_later_refile(self):
        """
        The refusal for "you have not implemented and run the tests
        yet" is about TIMING, not about the claim. Memoising it against
        the challenge fingerprint would permanently block the identical
        -- and by then legitimate -- report, which is precisely the
        Ledger sequence: the agent states the contradiction early, then
        confirms it after implementing.
        """

        script = [
            assistant(
                [
                    tool_call(
                        impl.CHALLENGE_TOOL,
                        challenge_args()
                    )
                ]
            ),
            assistant(
                [
                    tool_call(
                        "write_file",
                        {
                            "path": "LedgerService.cs",
                            "content": IMPLEMENTATION
                        }
                    )
                ]
            ),
            assistant(
                [
                    tool_call(
                        "run_operation",
                        {"operation": "test"}
                    )
                ]
            ),
            assistant(
                [
                    tool_call(
                        impl.CHALLENGE_TOOL,
                        challenge_args()
                    )
                ]
            ),
        ]

        outcome, reviewer, _, _ = self._run(script)

        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )

        self.assertEqual(len(reviewer.calls), 2)

    def test_a_rejected_operation_does_not_count_as_running_tests(self):
        """
        `run_operation` answers "OPERATION REJECTED" without executing
        anything. Accepting that as "I ran the tests" would make the
        prerequisite bypassable with one no-op call.
        """

        outcome, reviewer, _, _ = self._run(
            [
                assistant(
                    [
                        tool_call(
                            "write_file",
                            {
                                "path": "LedgerService.cs",
                                "content": IMPLEMENTATION
                            }
                        )
                    ]
                ),
                assistant(
                    [
                        tool_call(
                            "run_operation",
                            {
                                "operation": "test_filtered"
                            }
                        )
                    ]
                ),
                assistant(
                    [
                        tool_call(
                            impl.CHALLENGE_TOOL,
                            challenge_args()
                        )
                    ]
                ),
                assistant(),
            ]
        )

        self.assertNotEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )
        self.assertEqual(len(reviewer.calls), 0)

    def test_reviewers_see_the_code_the_agent_actually_wrote(self):
        """
        The production context must be read at challenge time. Handing
        reviewers the step-zero snapshot would show them a file with no
        implementation in it, turning "not written yet" into apparent
        evidence -- the main route to discarding a VALID contract.
        """

        _, reviewer, _, _ = self._run(
            self._ledger_script()
        )

        prompt = reviewer.calls[0]["prompt"]

        self.assertIn(
            "public bool Deposit(",
            prompt,
            "the reviewer was shown a stale snapshot of production"
        )

    def test_tool_arguments_delivered_as_a_json_string_are_accepted(self):
        outcome, reviewer, _, _ = self._run(
            [
                *self._ledger_script()[:2],
                assistant(
                    [
                        tool_call(
                            impl.CHALLENGE_TOOL,
                            json.dumps(
                                challenge_args()
                            )
                        )
                    ]
                ),
            ]
        )

        self.assertEqual(
            outcome["status"],
            impl.CONTRACT_CHALLENGED
        )
        self.assertEqual(len(reviewer.calls), 2)

    def test_rejected_challenge_lets_implementation_continue(self):
        script = self._ledger_script()

        script.append(
            assistant(
                [
                    tool_call(
                        "run_operation",
                        {"operation": "test"}
                    )
                ]
            )
        )

        outcome, reviewer, calls, _ = self._run(
            script,
            verdicts=[reject_verdict()]
        )

        # The loop kept going after the rejection instead of stopping.
        self.assertGreater(calls["model"], 3)
        self.assertEqual(
            outcome["status"],
            impl.FAILED
        )

    def test_repeated_identical_challenge_is_not_revalidated(self):
        script = self._ledger_script()

        script.append(
            assistant(
                [
                    tool_call(
                        impl.CHALLENGE_TOOL,
                        challenge_args()
                    )
                ]
            )
        )

        script.append(assistant())

        _, reviewer, _, _ = self._run(
            script,
            verdicts=[reject_verdict()]
        )

        self.assertEqual(
            len(reviewer.calls),
            1,
            "a duplicate challenge spent a second reviewer call"
        )

    def test_submissions_are_bounded(self):
        script = self._ledger_script()[:2]

        # Ten distinct malformed submissions; only the budget stops it.
        for index in range(10):
            script.append(
                assistant(
                    [
                        tool_call(
                            impl.CHALLENGE_TOOL,
                            {
                                "kind": "invalid",
                                "summary": f"attempt {index}"
                            }
                        )
                    ]
                )
            )

        _, _, _, config = self._run(
            script,
            config_overrides={
                "max_contract_challenge_submissions": 2,
                "agentic_max_steps": 14
            }
        )

        filed = [
            event
            for event in read_history(config)
            if event["event"]
            == "contract_challenge_malformed"
        ]

        self.assertEqual(len(filed), 2)

    def test_review_budget_is_bounded_separately(self):
        script = self._ledger_script()

        # A second, genuinely different challenge.
        script.append(
            assistant(
                [
                    tool_call(
                        impl.CHALLENGE_TOOL,
                        challenge_args(
                            kind="api_semantics",
                            contradiction=
                                "A second and entirely different "
                                "contradiction that also cannot hold."
                        )
                    )
                ]
            )
        )

        script.append(assistant())

        _, reviewer, _, _ = self._run(
            script,
            verdicts=[reject_verdict()],
            config_overrides={
                "max_contract_challenges": 1
            }
        )

        self.assertEqual(
            len(reviewer.calls),
            1,
            "review budget did not bound reviewer calls"
        )

    def test_malformed_arguments_are_explained_not_crashed(self):
        outcome, _, calls, _ = self._run(
            [
                assistant(
                    [
                        tool_call(
                            impl.CHALLENGE_TOOL,
                            {"kind": "object_identity"}
                        )
                    ]
                ),
                assistant(),
            ]
        )

        self.assertEqual(
            outcome["status"],
            impl.FAILED
        )
        self.assertEqual(calls["model"], 2)


class StallHintTests(WorkspaceCase):
    """
    Ledger Full #2's real cost was not the diagnosis, it was the 25
    steps of restating it. A deterministic, bounded reminder converts a
    repeated identical failure into a decision.
    """

    def _controller(self, **overrides):
        settings = {
            "hint_after_repeats": 3
        }

        settings.update(overrides)

        return impl.ChallengeController(
            self._config(),
            self.workspace,
            TASK,
            self.frozen_tests,
            Path(self.workspace),
            [
                {
                    "path": "LedgerService.cs",
                    "type": "implementation",
                    "reasons": ["deposit"]
                }
            ],
            FakeAdapter(),
            [],
            4,
            2,
            **settings
        )

    def _fail(self, controller, times, output=None):
        text = output or (
            "exit_code=1\n/repo/LedgerServiceTests.cs(9,3): "
            "error: Assert.True() Failure"
        )

        for _ in range(times):
            controller.note_operation(
                "test",
                text
            )

    def test_no_hint_before_the_threshold(self):
        controller = self._controller()

        self._fail(controller, 2)

        self.assertIsNone(
            controller.stall_hint()
        )

    def test_hint_after_identical_failures_repeat(self):
        controller = self._controller()

        self._fail(controller, 3)

        hint = controller.stall_hint()

        self.assertIsNotNone(hint)
        self.assertIn(
            impl.CHALLENGE_TOOL,
            hint
        )

    def test_changing_failures_do_not_trigger_a_hint(self):
        """
        A genuinely moving failure means the agent is making progress,
        so nothing is nudged. Note the signature deliberately ignores
        numbers: the same assertion failing with different expected
        values IS the same stall, not progress.
        """

        controller = self._controller()

        for name in (
            "CS0103 name does not exist",
            "Assert.True() Failure",
            "NullReferenceException",
            "Assert.Equal() Failure",
            "CS1061 no such member",
            "TimeoutException",
        ):
            controller.note_operation(
                "test",
                f"exit_code=1\nerror: {name}"
            )

            self.assertIsNone(
                controller.stall_hint()
            )

    def test_line_numbers_and_paths_do_not_defeat_the_signature(self):
        controller = self._controller()

        for index in range(3):
            controller.note_operation(
                "test",
                f"exit_code=1\n/tmp/build{index}/T.cs({index},{index}): "
                f"error: Assert.True() Failure in {index}.4s"
            )

        self.assertIsNotNone(
            controller.stall_hint()
        )

    def test_a_pass_resets_the_counter(self):
        controller = self._controller()

        self._fail(controller, 2)

        controller.note_operation(
            "test",
            "exit_code=0\nPassed!"
        )

        self._fail(controller, 2)

        self.assertIsNone(
            controller.stall_hint()
        )

    def test_hints_are_bounded(self):
        controller = self._controller()

        emitted = 0

        for _ in range(30):
            self._fail(controller, 3)

            if controller.stall_hint():
                emitted += 1

        self.assertEqual(
            emitted,
            impl.MAX_CHALLENGE_HINTS
        )

    def test_hint_reaches_the_agent_in_the_real_loop(self):
        script = [
            assistant(
                [
                    tool_call(
                        "write_file",
                        {
                            "path": "LedgerService.cs",
                            "content": IMPLEMENTATION
                        }
                    )
                ]
            )
        ]

        for _ in range(3):
            script.append(
                assistant(
                    [
                        tool_call(
                            "run_operation",
                            {"operation": "test"}
                        )
                    ]
                )
            )

        script.append(assistant())

        state = {}
        config = self._config(agentic_max_steps=8)

        seen = []

        def call_model(
            model,
            url,
            context_size,
            messages,
            tools=None
        ):
            seen.append(
                [
                    message
                    for message in messages
                    if message.get("role") == "user"
                ]
            )

            if script:
                return script.pop(0)

            return assistant()

        with mock.patch.object(
            impl,
            "_call_model",
            call_model
        ), mock.patch.object(
            impl,
            "_run_argv",
            lambda root, argv: "exit_code=1\nerror: Assert.True() Failure"
        ):
            impl.run_agentic_implementation_phase(
                config,
                self.workspace,
                TASK,
                state,
                [
                    {
                        "path": "LedgerService.cs",
                        "type": "implementation",
                        "reasons": ["deposit"]
                    }
                ],
                "build",
                "test",
                FakeAdapter(),
                [],
                None,
                frozen_tests=self.frozen_tests,
                challenge_runner=runner(1)
            )

        self.addCleanup(
            lambda: Path(
                config["history_file"]
            ).unlink(missing_ok=True)
        )

        self.addCleanup(
            lambda: Path(
                config["state_file"]
            ).unlink(missing_ok=True)
        )

        hinted = [
            message
            for turn in seen
            for message in turn
            if "HARNESS NOTICE" in message["content"]
        ]

        self.assertTrue(
            hinted,
            "the stall reminder never reached the agent"
        )


# ---------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------

def git(workspace, *args):
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True
    )


class PipelineReopenTests(unittest.TestCase):
    """
    A confirmed challenge must return control to the Test Contract
    phase -- bounded, once by default -- and a second confirmed
    challenge must fail the attempt cleanly rather than looping.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.workspace = Path(self._tmp.name)

        (
            self.workspace / "Ledger.csproj"
        ).write_text("<Project />")

        (
            self.workspace / "LedgerService.cs"
        ).write_text(PRODUCTION)

        (
            self.workspace / "LedgerServiceTests.cs"
        ).write_text(FROZEN_CONTRACT)

        git(self.workspace, "init", "-q")
        git(self.workspace, "config", "user.email", "h@example.invalid")
        git(self.workspace, "config", "user.name", "Harness")
        git(self.workspace, "add", "-A")
        git(self.workspace, "commit", "-q", "-m", "initial")

        self.state_file = (
            self.workspace.parent / "pipeline-state.json"
        )

        self.history_file = (
            self.workspace.parent / "pipeline-history.jsonl"
        )

        self.addCleanup(
            lambda: self.state_file.unlink(missing_ok=True)
        )

        self.addCleanup(
            lambda: self.history_file.unlink(missing_ok=True)
        )

    def _config(self, **overrides):
        config = {
            "workspace": str(self.workspace),
            "agentic_implementation_enabled": True,
            "state_file": str(self.state_file),
            "history_file": str(self.history_file),
        }

        config.update(overrides)

        return config

    def _run(self, implementation_outcomes, **overrides):
        from core import pipeline

        challenge, _ = normalize_challenge(
            challenge_args()
        )

        contract_calls = []
        red_calls = []

        def planning(*args, **kwargs):
            return {
                "plan": {"tests_required": True},
                "grouped": {},
                "implementation_changes": [
                    {
                        "path": "LedgerService.cs",
                        "type": "implementation",
                        "reasons": ["deposit"]
                    }
                ],
                "test_changes": [
                    {
                        "path": "LedgerServiceTests.cs",
                        "type": "test",
                        "reasons": ["tests"]
                    }
                ],
                "tests_required": True,
            }

        statuses = []

        def test_contract(config, workspace, *args, **kwargs):
            contract_calls.append(
                set(
                    config.get(
                        "forbidden_contract_fingerprints"
                    )
                    or ()
                )
            )

            statuses.append(
                git_status(
                    str(workspace)
                )
            )

            return {
                "frozen_tests": {
                    "LedgerServiceTests.cs": FROZEN_CONTRACT
                },
                "test_snapshot": {
                    "LedgerServiceTests.cs": FROZEN_CONTRACT
                },
            }

        def expected_red(*args, **kwargs):
            red_calls.append(True)
            return True

        outcomes = list(implementation_outcomes)

        def implementation(*args, **kwargs):
            # A real failed attempt leaves production edits behind.
            (
                self.workspace / "LedgerService.cs"
            ).write_text(IMPLEMENTATION)

            (
                self.workspace / "Scratch.cs"
            ).write_text("// scratch\n")

            outcome = outcomes.pop(0)

            if outcome == "challenged":
                return impl.challenged_outcome(
                    challenge,
                    {
                        "confirmed": True,
                        "stage": "review",
                        "reason": "confirmed",
                        "reasons": [
                            "Identity contradiction confirmed."
                        ]
                    }
                )

            return outcome

        memory = SpecFailureMemory(
            scope=spec_scope_key(
                "specs/001.md",
                TASK
            )
        )

        config = self._config(
            spec_memory=memory,
            **overrides
        )

        with mock.patch.object(
            pipeline,
            "run_planning_phase",
            planning
        ), mock.patch.object(
            pipeline,
            "run_test_contract_phase",
            test_contract
        ), mock.patch.object(
            pipeline,
            "run_expected_red_phase",
            expected_red
        ), mock.patch.object(
            pipeline,
            "run_agentic_implementation_phase",
            implementation
        ), mock.patch.object(
            pipeline,
            "run_build_phase",
            lambda *a, **k: True
        ), mock.patch.object(
            pipeline,
            "run_test_phase",
            lambda *a, **k: True
        ), mock.patch.object(
            pipeline,
            "run_review_phase",
            lambda *a, **k: True
        ):
            success = pipeline.run_pipeline(
                config,
                TASK,
                "test"
            )

        return {
            "success": success,
            "contract_calls": contract_calls,
            "red_calls": red_calls,
            "statuses": statuses,
            "memory": memory,
            "config": config,
        }

    def test_confirmed_challenge_reopens_the_test_contract_once(self):
        result = self._run(
            [
                "challenged",
                impl.completed_outcome(steps=4),
            ]
        )

        self.assertTrue(result["success"])

        self.assertEqual(
            len(result["contract_calls"]),
            2,
            "the Test Contract phase was not reopened"
        )

        self.assertEqual(
            len(result["red_calls"]),
            2,
            "Expected RED was not re-confirmed after regeneration"
        )

    def test_disproved_contract_cannot_be_frozen_again(self):
        from core.phases.test_contract_phase import (
            snippet_fingerprint,
        )

        result = self._run(
            [
                "challenged",
                impl.completed_outcome(steps=4),
            ]
        )

        first, second = result["contract_calls"]

        self.assertEqual(first, set())

        self.assertIn(
            snippet_fingerprint(
                FROZEN_CONTRACT
            ),
            second
        )

    def test_reopen_records_the_confirmed_defect_in_memory(self):
        result = self._run(
            [
                "challenged",
                impl.completed_outcome(steps=4),
            ]
        )

        joined = "\n".join(
            result["memory"].lines()
        )

        self.assertIn(
            "contract/challenge_confirmed",
            joined
        )

        events = [
            event["event"]
            for event in read_history(
                result["config"]
            )
        ]

        self.assertIn(
            "test_contract_reopened",
            events
        )

    def test_reopen_discards_work_written_against_the_bad_contract(self):
        """
        Observed from INSIDE the second Test Contract call, which is
        the only point where the reopen boundary is visible: the
        regenerating phase must start from a clean repository, not on
        top of the production work written against the disproved
        contract.
        """

        result = self._run(
            [
                "challenged",
                impl.completed_outcome(steps=4),
            ]
        )

        statuses = result["statuses"]

        self.assertEqual(len(statuses), 2)

        self.assertEqual(
            statuses[0],
            "",
            "the first cycle did not start clean"
        )

        self.assertEqual(
            statuses[1],
            "",
            "the reopened Test Contract phase inherited the "
            "discarded cycle's changes"
        )

    def test_reopen_budget_is_bounded_and_fails_clean(self):
        result = self._run(
            [
                "challenged",
                "challenged",
            ]
        )

        self.assertFalse(result["success"])

        self.assertEqual(
            len(result["contract_calls"]),
            2
        )

        self.assertEqual(
            git_status(str(self.workspace)),
            "",
            "a failed attempt left the repository dirty"
        )

    def test_zero_reopens_configured_fails_on_first_challenge(self):
        result = self._run(
            ["challenged"],
            max_contract_reopens=0
        )

        self.assertFalse(result["success"])

        self.assertEqual(
            len(result["contract_calls"]),
            1
        )

        self.assertEqual(
            git_status(str(self.workspace)),
            ""
        )

    def test_negative_reopen_budget_still_runs_the_phases(self):
        result = self._run(
            ["challenged"],
            max_contract_reopens=-3
        )

        self.assertFalse(result["success"])

        self.assertEqual(
            len(result["contract_calls"]),
            1
        )

    def test_ordinary_failure_still_fails_immediately(self):
        result = self._run(
            [
                impl.failed_outcome("no GREEN"),
            ]
        )

        self.assertFalse(result["success"])

        self.assertEqual(
            len(result["contract_calls"]),
            1,
            "an ordinary failure reopened the contract"
        )

    def test_legacy_boolean_outcome_is_still_understood(self):
        """
        The non-agentic implementation phase still answers a plain
        bool, and must keep working unchanged.
        """

        result = self._run([True])

        self.assertTrue(result["success"])

        # An ordinary failure does not roll back inside the pipeline --
        # the outer SPEC ATTEMPT loop owns that -- so restore the
        # baseline before driving a second pipeline run here.
        from core.repository import rollback_repository

        rollback_repository(
            str(self.workspace)
        )

        result = self._run([False])

        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
