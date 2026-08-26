import json

from core.context import implementation_text
from core.guards import (
    extract_test_method_names,
    validate_test_snippet,
)
from core.models import call_model
from core.prompts import render_prompt
from core.repository import (
    read_file,
    restore_snapshot,
    snapshot_files,
    write_file,
)
from core.state import (
    append_history,
    mark_phase_completed,
    mark_phase_started,
    save_state,
)
from core.test_merge import merge_test_snippet
from core.utils import extract_code


def test_snippet_prompt(
    task,
    implementation_files,
    current_test_content
):
    return render_prompt(
        "test-generator.md",
        task=task,
        production=implementation_text(
            implementation_files
        ),
        existing_tests=current_test_content
    )


def _format_issue_item(issue):
    if isinstance(issue, str):
        return issue

    return json.dumps(issue)


def format_prior_issues(rejection_memory):
    """
    Render accumulated per-contract rejection memory as prose for a
    prompt, framed as concerns to re-evaluate against the CURRENT
    contract rather than defects that are still assumed to hold.
    """

    lines = []

    for entry in (rejection_memory or []):
        for issue in entry.get("issues", []):
            lines.append(
                f"- [{entry['reviewer']} reviewer, "
                f"attempt {entry['attempt']}] "
                f"{_format_issue_item(issue)}"
            )

    if not lines:
        return (
            "(none raised yet in this Test Contract run)"
        )

    return "\n".join(lines)


VALID_DECISIONS = {"APPROVE", "REJECT"}


def validate_reviewer_schema(parsed):
    """
    A syntactically valid JSON object is NOT automatically a valid
    reviewer verdict. Confirm the top-level shape before trusting it:

        {"decision": "APPROVE" | "REJECT", "issues": [...]}

    Returns None when valid, or a short human-readable reason when
    not (e.g. a model that wraps its answer in a chat-style envelope
    such as {"role": "...", "content": "..."}).
    """

    if not isinstance(parsed, dict):
        return "response is not a JSON object"

    decision = parsed.get("decision")

    if (
        not isinstance(decision, str)
        or decision.upper() not in VALID_DECISIONS
    ):
        return "missing or invalid 'decision' field"

    issues = parsed.get("issues")

    if not isinstance(issues, list):
        return "missing or invalid 'issues' field (must be a list)"

    return None


def normalize_reviewer_decision(review_json):
    """
    A reviewer that returns APPROVE alongside a non-empty issues list
    has contradicted itself. Treat that as REJECT regardless of the
    literal decision field, so a self-contradictory verdict can never
    freeze a contract.
    """

    decision = review_json.get(
        "decision",
        ""
    ).upper()

    issues = review_json.get(
        "issues",
        []
    )

    if (
        decision == "APPROVE"
        and issues
    ):
        decision = "REJECT"

    return decision, issues


def test_snippet_revision_prompt(
    task,
    implementation_files,
    original_test_content,
    snippet,
    issues,
    prior_issues=None
):
    return render_prompt(
        "test-revision.md",
        task=task,
        production=implementation_text(
            implementation_files
        ),
        existing_tests=original_test_content,
        snippet=snippet,
        issues=json.dumps(
            issues,
            indent=2
        ),
        prior_issues=format_prior_issues(
            prior_issues
        )
    )


def test_review_prompt(
    task,
    implementation_files,
    merged_test_content,
    prior_issues=None
):
    return render_prompt(
        "test-reviewer.md",
        task=task,
        production=implementation_text(
            implementation_files
        ),
        tests=merged_test_content,
        prior_issues=format_prior_issues(
            prior_issues
        )
    )


def semantic_test_review_prompt(
    task,
    implementation_files,
    merged_test_content,
    prior_issues=None
):
    return render_prompt(
        "test-semantic-reviewer.md",
        task=task,
        production=implementation_text(
            implementation_files
        ),
        tests=merged_test_content,
        prior_issues=format_prior_issues(
            prior_issues
        )
    )


def schema_repair_prompt(malformed_response):
    return render_prompt(
        "test-review-schema-repair.md",
        malformed_response=malformed_response
    )


def _resolve_reviewer_verdict(
    config,
    model,
    prompt,
    reviewer_label,
    path,
    attempt,
    think,
    num_ctx,
    num_predict
):
    """
    Run one reviewer call end to end: dispatch, thinking/termination
    logging, truncation handling, and JSON schema validation with one
    bounded format-repair attempt if the response is a complete but
    schema-invalid JSON object (e.g. a chat-style envelope instead of
    {"decision": ..., "issues": [...]}).

    Returns a dict:

        {"status": "call_failed"}                       transient
        {"status": "truncated"}                          discard, retry
        {"status": "invalid"}                             discard, retry
        {"status": "ok", "decision": ..., "issues": [...]}

    Every non-"ok" status must be treated identically by the caller:
    do not approve, do not add to rejection_memory, retry safely.
    Schema repair never re-reviews the contract and never counts as a
    generation attempt or an independent reviewer vote — it is called
    from within the SAME attempt this helper was invoked for.
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
        return {"status": "call_failed"}

    if review.get("thinking"):
        append_history(
            config,
            "test_review_reasoning",
            {
                "file": path,
                "attempt": attempt,
                "reviewer": reviewer_label,
                "thinking": review["thinking"],
                "done_reason": review.get("done_reason")
            }
        )

    if review.get("truncated"):
        print(
            "REVIEWER OUTPUT TRUNCATED "
            f"(done_reason={review.get('done_reason')}): "
            f"{reviewer_label} verdict is incomplete, "
            "discarding and retrying."
        )
        return {"status": "truncated"}

    try:
        parsed = json.loads(review["response"])

    except json.JSONDecodeError:
        return {"status": "call_failed"}

    schema_issue = validate_reviewer_schema(parsed)

    if schema_issue is None:
        print(
            json.dumps(
                parsed,
                indent=2
            )
        )

        decision, issues = normalize_reviewer_decision(parsed)

        return {
            "status": "ok",
            "decision": decision,
            "issues": issues
        }

    # Complete, non-truncated response, but the wrong shape (for
    # example a chat-style {"role": ..., "content": ...} envelope
    # instead of {"decision": ..., "issues": [...]}). Preserve it for
    # observability, then attempt exactly one bounded format repair
    # with the SAME model — never a re-review, never recursive.
    print(
        f"REVIEWER RESPONSE SCHEMA INVALID ({schema_issue}): "
        f"{reviewer_label} verdict does not match the required "
        "schema. Attempting one bounded format repair."
    )

    append_history(
        config,
        "reviewer_schema_invalid",
        {
            "file": path,
            "attempt": attempt,
            "reviewer": reviewer_label,
            "raw_response": review["response"],
            "reason": schema_issue,
            "done_reason": review.get("done_reason")
        }
    )

    repair = call_model(
        config,
        model,
        schema_repair_prompt(review["response"]),
        json_mode=True,
        think=False,
        num_ctx=num_ctx,
        num_predict=num_predict
    )

    repair_outcome = "call_failed"
    repaired_decision = None
    repaired_issues = None

    if not repair["ok"]:
        repair_outcome = "call_failed"

    elif repair.get("truncated"):
        repair_outcome = "truncated"

    else:
        try:
            repaired = json.loads(repair["response"])
            repair_schema_issue = validate_reviewer_schema(repaired)

        except json.JSONDecodeError:
            repair_schema_issue = "response is not valid JSON"
            repaired = None

        if repair_schema_issue is None:
            repair_outcome = "ok"

            repaired_decision, repaired_issues = (
                normalize_reviewer_decision(repaired)
            )

        else:
            repair_outcome = f"still_invalid: {repair_schema_issue}"

    append_history(
        config,
        "reviewer_schema_repair",
        {
            "file": path,
            "attempt": attempt,
            "reviewer": reviewer_label,
            "repair_response": repair.get("response"),
            "outcome": repair_outcome,
            "done_reason": repair.get("done_reason")
        }
    )

    if repair_outcome == "ok":
        print(
            "SCHEMA REPAIR SUCCEEDED for "
            f"{reviewer_label} reviewer: recovered "
            f"decision={repaired_decision!r}."
        )
        print(
            json.dumps(
                {
                    "decision": repaired_decision,
                    "issues": repaired_issues
                },
                indent=2
            )
        )

        return {
            "status": "ok",
            "decision": repaired_decision,
            "issues": repaired_issues
        }

    print(
        f"SCHEMA REPAIR FAILED for {reviewer_label} reviewer "
        f"({repair_outcome}): discarding original verdict, "
        "retrying."
    )

    return {"status": "invalid"}


def run_test_contract_phase(
    config,
    workspace,
    task,
    state,
    implementation_changes,
    test_changes
):
    print()
    print("=" * 60)
    print(
        "PHASE 2 - TEST SNIPPET GENERATION"
    )
    print("=" * 60)

    mark_phase_started(
        config,
        state,
        "test_contract"
    )

    implementation_context = {}

    for change in implementation_changes:
        implementation_context[
            change["path"]
        ] = read_file(
            workspace,
            change["path"]
        )

    test_paths = [
        change["path"]
        for change
        in test_changes
    ]

    test_snapshot = snapshot_files(
        workspace,
        test_paths
    )

    # Reviewer prompts (production + full merged test file + prior
    # issues) can exceed Ollama's small default context window,
    # silently truncating the verdict mid-generation. Give reviewer
    # calls specifically an explicit, bounded budget rather than
    # relying on the server default.
    reviewer_num_ctx = config.get(
        "reviewer_context_size",
        16384
    )

    reviewer_num_predict = config.get(
        "reviewer_output_tokens",
        2048
    )

    frozen_tests = {}

    for test_change in test_changes:
        path = test_change[
            "path"
        ]

        original = test_snapshot[
            path
        ]

        generated = call_model(
            config,
            config[
                "coder_model"
            ],
            test_snippet_prompt(
                task,
                implementation_context,
                original
            )
        )

        if not generated["ok"]:
            restore_snapshot(
                workspace,
                test_snapshot
            )
            return None

        snippet = extract_code(
            generated[
                "response"
            ]
        )

        approved = False

        # Per-contract rejection memory: every issue raised by the
        # structural reviewer, semantic reviewer, or semantic
        # confirmation for THIS test_change, across all attempts in
        # THIS run_test_contract_phase call. Not persisted beyond it.
        rejection_memory = []

        def revise(issues_for_this_revision):
            revision = call_model(
                config,
                config[
                    "coder_model"
                ],
                test_snippet_revision_prompt(
                    task,
                    implementation_context,
                    original,
                    snippet,
                    issues_for_this_revision,
                    prior_issues=rejection_memory
                )
            )

            if revision["ok"]:
                return extract_code(
                    revision["response"]
                )

            return snippet

        for attempt in range(
            1,
            config[
                "max_test_generation_attempts"
            ] + 1
        ):
            print()
            print(
                "Test snippet attempt "
                f"{attempt}: {path}"
            )

            issues = (
                validate_test_snippet(
                    snippet,
                    original,
                    implementation_context
                )
            )

            if issues:
                print(
                    "SNIPPET GUARD: REJECT"
                )

                for issue in issues:
                    print(
                        f"- {issue}"
                    )

                snippet = revise(issues)

                continue

            try:
                merged = merge_test_snippet(
                    original,
                    snippet
                )

            except Exception as exc:
                print(
                    f"MERGE ERROR: {exc}"
                )
                continue

            structural_outcome = _resolve_reviewer_verdict(
                config,
                config["test_reviewer_model"],
                test_review_prompt(
                    task,
                    implementation_context,
                    merged,
                    prior_issues=rejection_memory
                ),
                "structural",
                path,
                attempt,
                config.get(
                    "test_reviewer_thinking",
                    False
                ),
                reviewer_num_ctx,
                reviewer_num_predict
            )

            if structural_outcome["status"] != "ok":
                continue

            structural_decision = structural_outcome["decision"]
            structural_issues = structural_outcome["issues"]

            if structural_decision != "APPROVE":
                rejection_memory.append(
                    {
                        "attempt": attempt,
                        "reviewer": "structural",
                        "issues": structural_issues
                    }
                )

                snippet = revise(structural_issues)

                continue

            semantic_model = config.get(
                "semantic_reviewer_model",
                config.get(
                    "escalation_model",
                    config[
                        "test_reviewer_model"
                    ]
                )
            )

            print()
            print(
                "Semantic contract audit:"
            )

            semantic_outcome = _resolve_reviewer_verdict(
                config,
                semantic_model,
                semantic_test_review_prompt(
                    task,
                    implementation_context,
                    merged,
                    prior_issues=rejection_memory
                ),
                "semantic",
                path,
                attempt,
                config.get(
                    "semantic_reviewer_thinking",
                    False
                ),
                reviewer_num_ctx,
                reviewer_num_predict
            )

            if semantic_outcome["status"] != "ok":
                continue

            semantic_decision = semantic_outcome["decision"]
            semantic_issues = semantic_outcome["issues"]

            if semantic_decision != "APPROVE":
                rejection_memory.append(
                    {
                        "attempt": attempt,
                        "reviewer": "semantic",
                        "issues": semantic_issues
                    }
                )

                snippet = revise(semantic_issues)

                continue

            if not rejection_memory:
                # Clean first-time APPROVE: nothing in this run has
                # ever been rejected for this test_change, so a
                # single semantic APPROVE is sufficient.
                approved = True
                break

            # This test_change was rejected at least once earlier in
            # this run. A single stochastic APPROVE is not enough on
            # its own — require one independent confirming review of
            # the SAME current contract before freezing it. This does
            # NOT consume its own attempt slot; it is validation of
            # the current attempt, not a new generation attempt.
            print()
            print(
                "Semantic confirmation audit:"
            )

            confirmation_outcome = _resolve_reviewer_verdict(
                config,
                semantic_model,
                semantic_test_review_prompt(
                    task,
                    implementation_context,
                    merged,
                    prior_issues=rejection_memory
                ),
                "semantic_confirmation",
                path,
                attempt,
                config.get(
                    "semantic_reviewer_thinking",
                    False
                ),
                reviewer_num_ctx,
                reviewer_num_predict
            )

            if confirmation_outcome["status"] != "ok":
                continue

            confirmation_decision = confirmation_outcome["decision"]
            confirmation_issues = confirmation_outcome["issues"]

            if confirmation_decision == "APPROVE":
                approved = True
                break

            rejection_memory.append(
                {
                    "attempt": attempt,
                    "reviewer": "semantic_confirmation",
                    "issues": confirmation_issues
                }
            )

            snippet = revise(confirmation_issues)

        if not approved:
            print(
                "Test contract "
                "was not approved."
            )

            restore_snapshot(
                workspace,
                test_snapshot
            )

            append_history(
                config,
                "test_contract_rejected",
                {
                    "file": path
                }
            )

            return None

        final_merged = merge_test_snippet(
            original,
            snippet
        )

        write_file(
            workspace,
            path,
            final_merged
        )

        frozen_tests[
            path
        ] = final_merged

        append_history(
            config,
            "test_contract_approved",
            {
                "file": path,
                "new_tests":
                    extract_test_method_names(
                        snippet
                    )
            }
        )

    state[
        "tests_generated"
    ] = True

    state[
        "tests_structurally_valid"
    ] = True

    state[
        "tests_reviewed"
    ] = True

    state[
        "tests_frozen"
    ] = True

    mark_phase_completed(
        config,
        state,
        "test_contract"
    )

    state["phase"] = "tests_frozen"
    state["current_phase"] = "tests_frozen"
    state["phase_status"] = "completed"

    save_state(
        config,
        state
    )

    return {
        "frozen_tests":
            frozen_tests,

        "test_snapshot":
            test_snapshot
    }
