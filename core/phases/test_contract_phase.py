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

            review = call_model(
                config,
                config[
                    "test_reviewer_model"
                ],
                test_review_prompt(
                    task,
                    implementation_context,
                    merged,
                    prior_issues=rejection_memory
                ),
                json_mode=True,
                think=config.get(
                    "test_reviewer_thinking",
                    False
                )
            )

            if not review[
                "ok"
            ]:
                continue

            if review.get(
                "thinking"
            ):
                append_history(
                    config,
                    "test_review_reasoning",
                    {
                        "file": path,
                        "attempt": attempt,
                        "reviewer": "structural",
                        "thinking":
                            review["thinking"]
                    }
                )

            try:
                review_json = json.loads(
                    review[
                        "response"
                    ]
                )

            except json.JSONDecodeError:
                continue

            print(
                json.dumps(
                    review_json,
                    indent=2
                )
            )

            structural_decision, structural_issues = (
                normalize_reviewer_decision(
                    review_json
                )
            )

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

            semantic_review = call_model(
                config,
                semantic_model,
                semantic_test_review_prompt(
                    task,
                    implementation_context,
                    merged,
                    prior_issues=rejection_memory
                ),
                json_mode=True,
                think=config.get(
                    "semantic_reviewer_thinking",
                    False
                )
            )

            if not semantic_review["ok"]:
                continue

            if semantic_review.get(
                "thinking"
            ):
                append_history(
                    config,
                    "test_review_reasoning",
                    {
                        "file": path,
                        "attempt": attempt,
                        "reviewer": "semantic",
                        "thinking":
                            semantic_review["thinking"]
                    }
                )

            try:
                semantic_json = json.loads(
                    semantic_review[
                        "response"
                    ]
                )

            except json.JSONDecodeError:
                continue

            print()
            print(
                "Semantic contract audit:"
            )
            print(
                json.dumps(
                    semantic_json,
                    indent=2
                )
            )

            semantic_decision, semantic_issues = (
                normalize_reviewer_decision(
                    semantic_json
                )
            )

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
            confirmation = call_model(
                config,
                semantic_model,
                semantic_test_review_prompt(
                    task,
                    implementation_context,
                    merged,
                    prior_issues=rejection_memory
                ),
                json_mode=True,
                think=config.get(
                    "semantic_reviewer_thinking",
                    False
                )
            )

            if not confirmation["ok"]:
                continue

            if confirmation.get(
                "thinking"
            ):
                append_history(
                    config,
                    "test_review_reasoning",
                    {
                        "file": path,
                        "attempt": attempt,
                        "reviewer": "semantic_confirmation",
                        "thinking":
                            confirmation["thinking"]
                    }
                )

            try:
                confirmation_json = json.loads(
                    confirmation[
                        "response"
                    ]
                )

            except json.JSONDecodeError:
                continue

            print()
            print(
                "Semantic confirmation audit:"
            )
            print(
                json.dumps(
                    confirmation_json,
                    indent=2
                )
            )

            confirmation_decision, confirmation_issues = (
                normalize_reviewer_decision(
                    confirmation_json
                )
            )

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
