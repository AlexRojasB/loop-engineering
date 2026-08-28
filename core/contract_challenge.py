"""
Implementation-time frozen-contract challenge.

The Ledger Full #2 run exposed a structural gap. Spec 001 froze a test
contract that compiled and passed both the structural and the semantic
reviewer, but was semantically impossible: the test asked a service to
act on the identifier of an object the service had never been given.

    var account = new Account("Checking", 100m);
    service.CreateAccount(account.Name, account.Balance);
    Assert.True(service.Deposit(account.Id, 50m));

`CreateAccount` registers a DIFFERENT instance with a new identifier, so
`account.Id` belongs to an object no production implementation could
ever find. The implementation agent diagnosed this correctly at step 6
of 31 -- and then kept going, because frozen tests are immutable and it
had no other move available.

This module gives it one: a bounded, evidence-carrying challenge that
stops implementation and hands the question back to the harness.

Design rules:

- The implementation agent NEVER edits a frozen test. It only files a
  structured report.
- Filing a report does NOT invalidate the contract. Validation is
  independent of the agent that raised it.
- Validation is deterministic first, model-reviewed second, and fails
  closed at every step: anything unproven, unparseable, unreproducible
  or merely unimplemented is REJECTED.
- Everything is bounded: field lengths, cited test count, submissions
  per implementation phase, and contract reopenings per spec attempt.
- Nothing here is language-specific. Test execution goes through the
  LanguageAdapter; evidence provenance is verified by literal
  containment against real repository content.
"""

import hashlib
import json
import re

from core.models import call_model
from core.prompts import render_prompt
from core.repository import run_argv
from core.state import append_history


# ---------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------

CHALLENGE_KINDS = (
    "spec_contradiction",
    "production_behavior",
    "object_identity",
    "api_semantics",
    "invariant",
)

REQUIRED_TEXT_FIELDS = (
    "summary",
    "contradiction",
    "authoritative_requirement",
    "production_path",
    "production_quote",
)

MAX_FIELD_CHARS = 800

MAX_FAILING_TESTS = 5

MIN_CONTRADICTION_CHARS = 40

MIN_QUOTE_CHARS = 12

DEFAULT_MAX_CHALLENGES = 2

DEFAULT_MAX_REOPENS = 1

WHITESPACE = re.compile(r"\s+")


def _clip(value):
    text = WHITESPACE.sub(
        " ",
        str(value or "")
    ).strip()

    if len(text) <= MAX_FIELD_CHARS:
        return text

    return text[:MAX_FIELD_CHARS - 3].rstrip() + "..."


def normalize_challenge(args):
    """
    Turn raw tool arguments into a bounded, well-formed challenge.

    Returns (challenge, error). Exactly one is None. The error text is
    handed straight back to the implementation agent, so it states what
    is missing rather than merely that something is.
    """

    if isinstance(args, (str, bytes)):
        # Some Ollama models/versions deliver tool arguments as a JSON
        # string rather than an object. That is a transport detail, not
        # a defective report.
        try:
            args = json.loads(args)

        except (TypeError, ValueError):
            return (
                None,
                "CHALLENGE REJECTED: arguments must be an object with "
                "the documented fields."
            )

    if not isinstance(args, dict):
        return (
            None,
            "CHALLENGE REJECTED: arguments must be an object."
        )

    kind = str(
        args.get("kind")
        or ""
    ).strip().lower()

    if kind not in CHALLENGE_KINDS:
        return (
            None,
            "CHALLENGE REJECTED: 'kind' must be one of: "
            + ", ".join(CHALLENGE_KINDS)
        )

    raw_tests = args.get(
        "failing_tests"
    )

    if isinstance(raw_tests, str):
        raw_tests = [raw_tests]

    if not isinstance(raw_tests, list):
        return (
            None,
            "CHALLENGE REJECTED: 'failing_tests' must be a list of "
            "test names taken from the frozen contract."
        )

    failing_tests = []

    for item in raw_tests:
        name = _clip(item)

        if name and name not in failing_tests:
            failing_tests.append(name)

    if not failing_tests:
        return (
            None,
            "CHALLENGE REJECTED: name at least one frozen test that "
            "cannot be satisfied."
        )

    if len(failing_tests) > MAX_FAILING_TESTS:
        failing_tests = failing_tests[
            :MAX_FAILING_TESTS
        ]

    challenge = {
        "kind": kind,
        "failing_tests": failing_tests,
    }

    for field in REQUIRED_TEXT_FIELDS:
        challenge[field] = _clip(
            args.get(field)
        )

        if not challenge[field]:
            return (
                None,
                f"CHALLENGE REJECTED: '{field}' is required."
            )

    if len(
        challenge["contradiction"]
    ) < MIN_CONTRADICTION_CHARS:
        return (
            None,
            "CHALLENGE REJECTED: 'contradiction' must explain, "
            "concretely, why no conforming implementation of the "
            "authorized production files can satisfy the cited "
            "test(s)."
        )

    if len(
        challenge["production_quote"]
    ) < MIN_QUOTE_CHARS:
        return (
            None,
            "CHALLENGE REJECTED: 'production_quote' must be a literal "
            "excerpt copied from the file named in 'production_path'."
        )

    return (challenge, None)


def challenge_fingerprint(challenge):
    """
    Identity of a challenge, so the same one filed twice inside one
    implementation phase is recognised without spending reviewer calls
    on it again.
    """

    payload = json.dumps(
        {
            "kind": challenge["kind"],
            "failing_tests": sorted(
                challenge["failing_tests"]
            ),
            "contradiction":
                challenge["contradiction"].lower(),
        },
        sort_keys=True
    )

    return hashlib.sha256(
        payload.encode(
            "utf-8",
            "replace"
        )
    ).hexdigest()


def format_challenge(challenge):
    return (
        f"kind: {challenge['kind']}\n"
        f"failing tests: "
        + ", ".join(
            challenge["failing_tests"]
        )
        + f"\nsummary: {challenge['summary']}\n"
        f"authoritative requirement: "
        f"{challenge['authoritative_requirement']}\n"
        f"production evidence "
        f"({challenge['production_path']}): "
        f"{challenge['production_quote']}\n"
        f"contradiction: {challenge['contradiction']}"
    )


def challenge_memory_entry(challenge):
    """
    One condensed line for cross-attempt failure memory.
    """

    return (
        "A confirmed contract challenge ("
        f"{challenge['kind']}) proved the frozen contract "
        "unsatisfiable. Tests: "
        + ", ".join(
            challenge["failing_tests"]
        )
        + ". "
        + challenge["contradiction"]
    )


# ---------------------------------------------------------------------
# Deterministic evidence gate
# ---------------------------------------------------------------------

def _normalized(text):
    return WHITESPACE.sub(
        " ",
        text or ""
    ).strip().lower()


def _bounded(output, limit=8000):
    output = output or ""

    if len(output) <= limit:
        return output

    half = limit // 2

    return (
        output[:half]
        + "\n...[TRUNCATED]...\n"
        + output[-half:]
    )


def _frozen_contract_text(frozen_tests):
    if not frozen_tests:
        return ""

    return "\n".join(
        str(content)
        for content in frozen_tests.values()
    )


def evidence_gate(
    workspace,
    challenge,
    frozen_tests,
    adapter,
    repository_files,
    read_file,
    runner=None
):
    """
    Cheap, deterministic, language-agnostic admissibility check.

    Proves -- before any model is asked anything -- that the challenge
    is about real code and a real, reproducible failure:

    1. every cited test name actually occurs in the frozen contract;
    2. the cited production file exists and is readable;
    3. the quoted production evidence really occurs in that file
       (provenance: fabricated evidence cannot pass);
    4. the cited failure reproduces right now.

    Returns {"ok": bool, "reason": str|None, "output": str}. Fails
    closed: an adapter that cannot run tests, or a runner that raises,
    yields ok=False.
    """

    contract_text = _normalized(
        _frozen_contract_text(
            frozen_tests
        )
    )

    if not contract_text:
        return {
            "ok": False,
            "reason":
                "No frozen test contract is associated with this "
                "implementation phase, so there is nothing to "
                "challenge.",
            "output": ""
        }

    missing = [
        name
        for name in challenge["failing_tests"]
        if _normalized(name)
        not in contract_text
    ]

    if missing:
        return {
            "ok": False,
            "reason":
                "These cited test names do not appear in the frozen "
                "contract: "
                + ", ".join(missing)
                + ". Cite the exact test names from the frozen test "
                "file.",
            "output": ""
        }

    cited_path = challenge[
        "production_path"
    ]

    # The cited path selects which file's full text the adjudicator is
    # shown as "Existing Production Code". It must not be the contract
    # under dispute, and must not be a test file: a report is about
    # production behavior, and the frozen contract is already supplied
    # to the reviewer separately.
    if cited_path in (frozen_tests or {}):
        return {
            "ok": False,
            "reason":
                "'production_path' names the frozen test file. Cite "
                "the production code whose behavior contradicts the "
                "test.",
            "output": ""
        }

    try:
        is_test = bool(
            adapter.is_test_path(
                cited_path
            )
        )

    except Exception:
        is_test = False

    if is_test:
        return {
            "ok": False,
            "reason":
                f"'production_path' ({cited_path}) is a test file. "
                "Cite the production code whose behavior contradicts "
                "the test.",
            "output": ""
        }

    try:
        production = read_file(
            workspace,
            cited_path
        )

    except Exception as exc:
        return {
            "ok": False,
            "reason":
                "The cited production file could not be read "
                f"({type(exc).__name__}): {cited_path}",
            "output": ""
        }

    if (
        _normalized(
            challenge["production_quote"]
        )
        not in _normalized(production)
    ):
        return {
            "ok": False,
            "reason":
                "'production_quote' does not occur in "
                f"{cited_path}. Evidence must be copied literally "
                "from real repository content.",
            "output": ""
        }

    execute = runner or run_argv

    # The production code must BUILD before its behavior can be
    # evidence of anything. A half-written implementation that does not
    # compile makes the whole suite red, which would otherwise satisfy
    # a naive "the tests fail" check with the agent's own breakage. A
    # contract that cannot even compile is a different problem, and the
    # deterministic contract compilation gate rejects that one before
    # freezing.
    build_argv = None

    try:
        build_argv = adapter.build_argv(
            repository_files
        )

    except Exception:
        build_argv = None

    if build_argv:
        try:
            build = execute(
                workspace,
                build_argv
            )

        except Exception as exc:
            return {
                "ok": False,
                "reason":
                    "The project build could not be run, so the "
                    "claimed failure cannot be attributed to the "
                    f"contract ({type(exc).__name__}: {exc}).",
                "output": ""
            }

        if build.get("exit_code") != 0:
            return {
                "ok": False,
                "reason":
                    "The project does not currently build. Fix the "
                    "build first: a compilation failure in your own "
                    "production code is not evidence about the "
                    "contract.",
                "output": _bounded(
                    build.get("output")
                )
            }

    # Reproduction must be ATTRIBUTABLE to the cited test, so a
    # filtered run is required where the adapter supports one. A
    # full-suite fallback is accepted only if the captured output names
    # the cited test, because during a test-first implementation phase
    # the suite is red by construction.
    cited = challenge[
        "failing_tests"
    ][0]

    argv = None
    filtered = False

    try:
        argv = adapter.test_argv(
            repository_files,
            filter=cited
        )

        filtered = argv is not None

    except Exception:
        argv = None
        filtered = False

    if argv is None:
        try:
            argv = adapter.test_argv(
                repository_files
            )

        except Exception:
            argv = None

    if not argv:
        return {
            "ok": False,
            "reason":
                "This project's language adapter cannot execute "
                "tests, so the claimed failure cannot be "
                "reproduced independently.",
            "output": ""
        }

    try:
        result = execute(
            workspace,
            argv
        )

    except Exception as exc:
        return {
            "ok": False,
            "reason":
                "The cited failure could not be reproduced "
                f"({type(exc).__name__}: {exc}).",
            "output": ""
        }

    output = _bounded(
        result.get("output")
    )

    if result.get("exit_code") == 0:
        return {
            "ok": False,
            "reason":
                "The cited test currently PASSES. A contract can "
                "only be challenged over a failure that reproduces.",
            "output": output
        }

    if (
        _normalized(cited)
        not in _normalized(output)
    ):
        return {
            "ok": False,
            "reason":
                "The reproduced failure does not name the cited test "
                f"({cited}), so it cannot be attributed to it. "
                + (
                    "Run that test and report the failure it actually "
                    "produces."
                    if filtered
                    else "This project's test runner could not be "
                    "scoped to a single test, and the suite-wide "
                    "output does not identify the cited test."
                ),
            "output": output
        }

    return {
        "ok": True,
        "reason": None,
        "output": output,
        "cited_content": production
    }


# ---------------------------------------------------------------------
# Independent model review
# ---------------------------------------------------------------------

VALID_CHALLENGE_DECISIONS = {
    "CONFIRM",
    "REJECT",
}


def validate_challenge_verdict_schema(parsed):
    if not isinstance(parsed, dict):
        return "response is not a JSON object"

    decision = parsed.get("decision")

    if (
        not isinstance(decision, str)
        or decision.upper()
        not in VALID_CHALLENGE_DECISIONS
    ):
        return "missing or invalid 'decision' field"

    reasons = parsed.get("reasons")

    if not isinstance(reasons, list):
        return (
            "missing or invalid 'reasons' field "
            "(must be a list)"
        )

    return None


def review_production_context(
    production,
    challenge,
    cited_content=None
):
    """
    What the adjudicator gets to see.

    The authorized production files, plus the file the challenge cites
    as evidence. The cited file matters: in a multi-file project the
    decisive fact (an identifier generated in a constructor, say) often
    lives in a file that is not itself an implementation target, and an
    adjudicator that cannot see it can only reject for lack of evidence.
    """

    files = dict(production or {})

    path = (challenge or {}).get(
        "production_path"
    )

    if (
        path
        and cited_content is not None
        and path not in files
    ):
        files[path] = cited_content

    if not files:
        return "(no production files available)"

    return "\n\n".join(
        f"--- {path} ---\n{content}"
        for path, content
        in sorted(
            files.items()
        )
    )


def challenge_review_prompt(
    task,
    frozen_tests,
    production,
    challenge,
    evidence_output,
    cited_content=None
):
    return render_prompt(
        "contract-challenge-reviewer.md",
        task=task,
        tests=_frozen_contract_text(
            frozen_tests
        ),
        production=review_production_context(
            production,
            challenge,
            cited_content
        ),
        challenge=format_challenge(
            challenge
        ),
        evidence=evidence_output
            or "(no captured output)"
    )


def _resolve_challenge_verdict(
    config,
    model,
    prompt,
    label,
    think,
    num_ctx,
    num_predict
):
    """
    One independent reviewer call.

    Fails closed in every degenerate case: a failed call, a truncated
    verdict, unparseable JSON or the wrong schema all become REJECT.
    A frozen contract is never discarded on the strength of a verdict
    the harness could not fully read. Deliberately no schema-repair
    round trip: repair exists to rescue an APPROVE-style gate, whereas
    here an unreadable verdict already means "keep the contract".
    """

    review = call_model(
        config,
        model,
        prompt,
        json_mode=True,
        think=think,
        num_ctx=num_ctx,
        num_predict=num_predict
    )

    if not review["ok"]:
        return {
            "decision": "REJECT",
            "reasons": [
                "Challenge reviewer call failed: "
                f"{review.get('error')}"
            ],
            "status": "call_failed"
        }

    if review.get("truncated"):
        return {
            "decision": "REJECT",
            "reasons": [
                "Challenge reviewer verdict was truncated "
                f"(done_reason={review.get('done_reason')})."
            ],
            "status": "truncated"
        }

    try:
        parsed = json.loads(
            review["response"]
        )

    except (TypeError, ValueError):
        return {
            "decision": "REJECT",
            "reasons": [
                "Challenge reviewer response was not valid JSON."
            ],
            "status": "invalid"
        }

    schema_issue = (
        validate_challenge_verdict_schema(
            parsed
        )
    )

    if schema_issue is not None:
        return {
            "decision": "REJECT",
            "reasons": [
                "Challenge reviewer response did not match the "
                f"required schema ({schema_issue})."
            ],
            "status": "invalid"
        }

    decision = parsed[
        "decision"
    ].upper()

    reasons = [
        str(reason)
        for reason in parsed["reasons"]
    ]

    print()
    print(
        f"CHALLENGE REVIEW ({label}): {decision}"
    )

    for reason in reasons:
        print(f"- {reason}")

    return {
        "decision": decision,
        "reasons": reasons,
        "status": "ok",
        "thinking": review.get(
            "thinking"
        )
    }


def review_challenge(
    config,
    task,
    frozen_tests,
    production,
    challenge,
    evidence_output,
    cited_content=None
):
    """
    Two independent reviews of the SAME challenge, both of which must
    CONFIRM before a frozen contract may be reopened.

    Mirrors the confirmation rule the Test Contract phase already uses
    before freezing: one stochastic verdict is never enough to move a
    gate. Here the asymmetry matters even more, because a wrongly
    confirmed challenge throws away a contract that reviewers already
    approved.
    """

    model = config.get(
        "challenge_reviewer_model",
        config.get(
            "semantic_reviewer_model",
            config.get(
                "test_reviewer_model"
            )
        )
    )

    if not model:
        return {
            "confirmed": False,
            "reviews": [],
            "reasons": [
                "No reviewer model is configured, so a challenge "
                "cannot be independently validated."
            ]
        }

    think = config.get(
        "challenge_reviewer_thinking",
        config.get(
            "semantic_reviewer_thinking",
            False
        )
    )

    num_ctx = config.get(
        "reviewer_context_size",
        16384
    )

    num_predict = config.get(
        "reviewer_output_tokens",
        2048
    )

    prompt = challenge_review_prompt(
        task,
        frozen_tests,
        production,
        challenge,
        evidence_output,
        cited_content=cited_content
    )

    # Adjudication prompts carry the whole frozen contract plus
    # production code, and a thinking reviewer needs longer on them than
    # an ordinary call. A timeout fails closed -- correct, but it would
    # silently disable the mechanism, so give this reviewer its own
    # budget rather than inheriting the general one.
    timeout = config.get(
        "challenge_reviewer_timeout_seconds"
    )

    if timeout:
        config = dict(config)
        config["model_timeout_seconds"] = int(
            timeout
        )

    reviews = []
    reasons = []

    for label in (
        "independent",
        "confirmation",
    ):
        verdict = _resolve_challenge_verdict(
            config,
            model,
            prompt,
            label,
            think,
            num_ctx,
            num_predict
        )

        reviews.append(
            {
                "reviewer": label,
                **verdict
            }
        )

        reasons.extend(
            verdict["reasons"]
        )

        if verdict["decision"] != "CONFIRM":
            return {
                "confirmed": False,
                "reviews": reviews,
                "reasons": reasons
            }

    return {
        "confirmed": True,
        "reviews": reviews,
        "reasons": reasons
    }


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------

def validate_challenge(
    config,
    workspace,
    challenge,
    task,
    frozen_tests,
    production,
    adapter,
    repository_files,
    read_file,
    runner=None,
    allow_review=True
):
    """
    Full independent validation of one filed challenge.

    Deterministic evidence first (cheap, unfakeable), independent model
    review second (expensive, judgment). Returns:

        {
            "confirmed": bool,
            "stage": "evidence" | "review",
            "reason": str,            # agent-facing explanation
            "reasons": [str, ...],
            "evidence_output": str
        }
    """

    gate = evidence_gate(
        workspace,
        challenge,
        frozen_tests,
        adapter,
        repository_files,
        read_file,
        runner=runner
    )

    append_history(
        config,
        "contract_challenge_evidence",
        {
            "kind": challenge["kind"],
            "failing_tests":
                challenge["failing_tests"],
            "ok": gate["ok"],
            "reason": gate["reason"]
        }
    )

    if not gate["ok"]:
        return {
            "confirmed": False,
            "stage": "evidence",
            "reason": gate["reason"],
            "reasons": [
                gate["reason"]
            ],
            "evidence_output":
                gate["output"]
        }

    if not allow_review:
        # Evidence is admissible but the independent-review budget for
        # this implementation phase is spent. Fail closed: the contract
        # stands.
        reason = (
            "The independent contract-review budget for this "
            "implementation attempt is exhausted, so this report "
            "cannot be validated. The frozen contract stands."
        )

        append_history(
            config,
            "contract_challenge_review_budget_exhausted",
            {
                "kind": challenge["kind"],
                "failing_tests":
                    challenge["failing_tests"]
            }
        )

        return {
            "confirmed": False,
            "stage": "budget",
            "reason": reason,
            "reasons": [reason],
            "evidence_output": gate["output"]
        }

    review = review_challenge(
        config,
        task,
        frozen_tests,
        production,
        challenge,
        gate["output"],
        cited_content=gate.get(
            "cited_content"
        )
    )

    append_history(
        config,
        "contract_challenge_reviewed",
        {
            "kind": challenge["kind"],
            "failing_tests":
                challenge["failing_tests"],
            "confirmed": review["confirmed"],
            "reviews": [
                {
                    "reviewer": item["reviewer"],
                    "decision": item["decision"],
                    "status": item["status"]
                }
                for item in review["reviews"]
            ],
            "reasons": review["reasons"]
        }
    )

    if review["confirmed"]:
        return {
            "confirmed": True,
            "stage": "review",
            "reason":
                "Independent review confirmed the frozen contract is "
                "inconsistent. Implementation stops; the Test "
                "Contract will be reopened.",
            "reasons": review["reasons"],
            "evidence_output": gate["output"]
        }

    return {
        "confirmed": False,
        "stage": "review",
        "reason":
            "Independent review did NOT confirm this challenge. The "
            "frozen contract stands. Reasons: "
            + "; ".join(
                review["reasons"]
            ),
        "reasons": review["reasons"],
        "evidence_output": gate["output"]
    }
