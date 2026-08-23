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


def test_snippet_revision_prompt(
    task,
    implementation_files,
    original_test_content,
    snippet,
    issues
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
        )
    )


def test_review_prompt(
    task,
    implementation_files,
    merged_test_content
):
    return render_prompt(
        "test-reviewer.md",
        task=task,
        production=implementation_text(
            implementation_files
        ),
        tests=merged_test_content
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

    state[
        "phase"
    ] = "test_generation"

    save_state(
        config,
        state
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
                        issues
                    )
                )

                if not revision[
                    "ok"
                ]:
                    continue

                snippet = extract_code(
                    revision[
                        "response"
                    ]
                )

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
                    merged
                ),
                json_mode=True
            )

            if not review[
                "ok"
            ]:
                continue

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

            if (
                review_json.get(
                    "decision",
                    ""
                ).upper()
                == "APPROVE"
            ):
                approved = True
                break

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
                    review_json.get(
                        "issues",
                        []
                    )
                )
            )

            if revision[
                "ok"
            ]:
                snippet = extract_code(
                    revision[
                        "response"
                    ]
                )

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

    state[
        "phase"
    ] = "tests_frozen"

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
