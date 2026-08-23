import json
import os
import re
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from core.models import call_model
from core.test_merge import merge_test_snippet
from core.utils import (
    compact,
    extract_code,
    load_json,
)
from core.prompts import render_prompt
from core.context import (
    build_behavior_contract,
    implementation_text,
)
from core.validation import (
    classify_red_state,
    failure_score,
    parse_test_counts,
)
from core.guards import (
    extract_test_method_names,
    production_guard,
    validate_test_snippet,
)
from core.planning import (
    group_changes_by_file,
    normalize_plan,
)
from core.state import (
    append_history,
    default_state,
    save_state,
)
from core.repository import (
    discover_files,
    ensure_clean_baseline,
    git_diff,
    git_restore_all,
    read_file,
    restore_snapshot,
    run_command,
    snapshot_files,
    write_file,
)


OLLAMA_URL = "http://localhost:11434/api/generate"

IGNORED_DIRS = {
    ".git",
    "bin",
    "obj",
    ".venv",
    "node_modules"
}

SOURCE_EXTENSIONS = (
    ".cs", ".py", ".ts", ".tsx", ".js", ".jsx",
    ".java", ".rs", ".go", ".cpp", ".c", ".h"
)

CONFIG_EXTENSIONS = (
    ".csproj", ".fsproj", ".vbproj",
    ".sln", ".slnx", ".props", ".targets"
)

CONFIG_FILENAMES = {
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "dockerfile"
}


# ============================================================
# GENERAL
# ============================================================


def now_iso():
    return datetime.now(timezone.utc).isoformat()




# ============================================================
# TEST SNIPPET / MERGE
# ============================================================






# ============================================================
# RED CLASSIFICATION
# ============================================================


# ============================================================
# PROMPTS
# ============================================================

def planner_prompt(task, files):
    return render_prompt(
        "planner.md",
        task=task,
        files="\n".join(files)
    )

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
        issues=str(issues)
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

def implementation_prompt(
    task,
    file_change,
    current_content,
    frozen_tests
):
    return render_prompt(
        "coder.md",
        behavior_contract=build_behavior_contract(
            task,
            file_change
        ),
        target=file_change["path"],
        current_content=current_content
    )

def repair_prompt(
    task,
    file_change,
    current_content,
    failure
):
    requirements = "\n".join(
        f"- {reason}"
        for reason in file_change["reasons"]
    )

    return render_prompt(
        "repair.md",
        task=task,
        target=file_change["path"],
        requirements=requirements,
        current_content=current_content,
        failure=failure
    )

def reviewer_prompt(
    task,
    plan,
    diff
):
    import json

    return render_prompt(
        "reviewer.md",
        task=task,
        plan=json.dumps(
            plan,
            indent=2
        ),
        diff=diff
    )

# ============================================================
# VALIDATION HELPERS
# ============================================================



# ============================================================
# MAIN
# ============================================================

def main():
    config = load_json(
        "config.json"
    )

    workspace = config[
        "workspace"
    ]

    if not ensure_clean_baseline(
        workspace
    ):
        return

    with open("TASK.md") as f:
        task = f.read()

    state = default_state(task)

    save_state(
        config,
        state
    )

    append_history(
        config,
        "run_started"
    )

    files = discover_files(
        workspace
    )

    print()
    print("=" * 60)
    print("AGENT V2.4.3")
    print("=" * 60)

    # ========================================================
    # PLANNING
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 1 - PLANNING")
    print("=" * 60)

    state["phase"] = "planning"
    save_state(
        config,
        state
    )

    planner_result = call_model(
        config,
        config[
            "planner_model"
        ],
        planner_prompt(
            task,
            files
        ),
        json_mode=True
    )

    if not planner_result["ok"]:
        print(
            planner_result["error"]
        )
        return

    try:
        planner_plan = json.loads(
            planner_result[
                "response"
            ]
        )
    except json.JSONDecodeError:
        print(
            "Planner returned invalid JSON."
        )
        return

    plan = normalize_plan(
        workspace,
        files,
        planner_plan
    )

    print(
        json.dumps(
            plan,
            indent=2
        )
    )

    state[
        "planner_complete"
    ] = True

    save_state(
        config,
        state
    )

    append_history(
        config,
        "plan_created",
        plan
    )

    if (
        plan[
            "dependencies_required"
        ]
    ):
        print(
            "Dependency tools not implemented yet."
        )
        return

    grouped = group_changes_by_file(
        plan["changes"]
    )

    implementation_changes = [
        c
        for c in grouped
        if c["type"]
        in (
            "implementation",
            "configuration"
        )
    ]

    test_changes = [
        c
        for c in grouped
        if c["type"] == "test"
    ]

    if not implementation_changes:
        print(
            "No implementation changes planned."
        )
        return

    if not test_changes:
        print(
            "No tests planned."
        )
        return

    implementation_context = {}

    for change in (
        implementation_changes
    ):
        implementation_context[
            change["path"]
        ] = read_file(
            workspace,
            change["path"]
        )

    # ========================================================
    # TEST SNIPPETS
    # ========================================================

    print()
    print("=" * 60)
    print(
        "PHASE 2 - TEST SNIPPET GENERATION"
    )
    print("=" * 60)

    state["phase"] = (
        "test_generation"
    )

    save_state(
        config,
        state
    )

    test_paths = [
        c["path"]
        for c in test_changes
    ]

    test_snapshot = (
        snapshot_files(
            workspace,
            test_paths
        )
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
            return

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
                f"Test snippet attempt "
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

                if not revision["ok"]:
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
                    "MERGE ERROR:"
                )
                print(exc)
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

            if not review["ok"]:
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

            review_issues = (
                review_json.get(
                    "issues",
                    []
                )
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
                    review_issues
                )
            )

            if not revision["ok"]:
                continue

            snippet = extract_code(
                revision[
                    "response"
                ]
            )

        if not approved:
            print()
            print(
                "Test contract was not approved."
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

            return

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

    # ========================================================
    # EXPECTED RED
    # ========================================================

    print()
    print("=" * 60)
    print(
        "PHASE 3 - EXPECTED RED"
    )
    print("=" * 60)

    red = run_command(
        workspace,
        config[
            "validation"
        ]["test"]
    )

    print(
        compact(
            red[
                "output"
            ]
        )
    )

    classification = (
        classify_red_state(
            red[
                "output"
            ]
        )
    )

    print()
    print(
        json.dumps(
            classification,
            indent=2
        )
    )

    append_history(
        config,
        "red_state_classified",
        classification
    )

    if red[
        "exit_code"
    ] == 0:
        print(
            "Tests already pass before "
            "implementation. Contract may be weak."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )
        return

    if (
        classification[
            "classification"
        ]
        == "BROKEN_TEST_SUITE"
    ):
        print(
            "Broken generated test suite."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )
        return

    state[
        "expected_red_confirmed"
    ] = True

    save_state(
        config,
        state
    )

    # ========================================================
    # IMPLEMENTATION
    # ========================================================

    print()
    print("=" * 60)
    print(
        "PHASE 4 - IMPLEMENTATION"
    )
    print("=" * 60)

    state["phase"] = (
        "implementation"
    )

    save_state(
        config,
        state
    )

    implementation_snapshot = (
        snapshot_files(
            workspace,
            [
                c["path"]
                for c in
                implementation_changes
            ]
        )
    )

    for change in (
        implementation_changes
    ):
        path = change[
            "path"
        ]

        current = read_file(
            workspace,
            path
        )

        result = call_model(
            config,
            config[
                "coder_model"
            ],
            implementation_prompt(
                task,
                change,
                current,
                frozen_tests
            )
        )

        if not result["ok"]:
            restore_snapshot(
                workspace,
                implementation_snapshot
            )
            return

        generated_content = extract_code(
            result[
                "response"
            ]
        )

        guard_issues = production_guard(
            generated_content
        )

        if guard_issues:
            print()
            print(
                "PRODUCTION GUARD: REJECT"
            )

            for issue in guard_issues:
                print(
                    f"- {issue}"
                )

            append_history(
                config,
                "production_guard_rejected",
                {
                    "file": path,
                    "issues": guard_issues
                }
            )

            restore_snapshot(
                workspace,
                implementation_snapshot
            )

            print(
                "Production generation rejected "
                "before build."
            )

            return

        write_file(
            workspace,
            path,
            generated_content
        )

    state[
        "implementation_generated"
    ] = True

    save_state(
        config,
        state
    )

    # ========================================================
    # BUILD
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 5 - BUILD")
    print("=" * 60)

    build = run_command(
        workspace,
        config[
            "validation"
        ]["build"]
    )

    print(
        compact(
            build[
                "output"
            ]
        )
    )

    for attempt in range(
        1,
        config[
            "max_build_repairs"
        ] + 1
    ):
        if (
            build[
                "exit_code"
            ] == 0
        ):
            break

        print()
        print(
            f"BUILD REPAIR {attempt}"
        )

        snapshot = (
            snapshot_files(
                workspace,
                [
                    c["path"]
                    for c in
                    implementation_changes
                ]
            )
        )

        old_score = failure_score(
            build[
                "output"
            ]
        )

        for change in (
            implementation_changes
        ):
            path = change[
                "path"
            ]

            repair = call_model(
                config,
                config[
                    "coder_model"
                ],
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    compact(
                        build[
                            "output"
                        ]
                    )
                )
            )

            if repair["ok"]:
                write_file(
                    workspace,
                    path,
                    extract_code(
                        repair[
                            "response"
                        ]
                    )
                )

        candidate = run_command(
            workspace,
            config[
                "validation"
            ]["build"]
        )

        print(
            compact(
                candidate[
                    "output"
                ]
            )
        )

        if (
            candidate[
                "exit_code"
            ] == 0
        ):
            build = candidate
            break

        new_score = failure_score(
            candidate[
                "output"
            ]
        )

        if new_score < old_score:
            print(
                "Build progress detected."
            )

            build = candidate

        else:
            print(
                "No build progress. "
                "Rolling back."
            )

            restore_snapshot(
                workspace,
                snapshot
            )

    if (
        build[
            "exit_code"
        ] != 0
    ):
        print(
            "Build did not converge."
        )

        git_restore_all(
            workspace
        )
        return

    state["build"] = "pass"

    save_state(
        config,
        state
    )

    # ========================================================
    # TESTS
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 6 - TESTS")
    print("=" * 60)

    tests = run_command(
        workspace,
        config[
            "validation"
        ]["test"]
    )

    print(
        compact(
            tests[
                "output"
            ]
        )
    )

    best_score = failure_score(
        tests[
            "output"
        ]
    )

    for attempt in range(
        1,
        config[
            "max_test_repairs"
        ] + 1
    ):
        if (
            tests[
                "exit_code"
            ] == 0
        ):
            break

        print()
        print(
            f"TEST REPAIR {attempt}"
        )

        snapshot = (
            snapshot_files(
                workspace,
                [
                    c["path"]
                    for c in
                    implementation_changes
                ]
            )
        )

        for change in (
            implementation_changes
        ):
            path = change[
                "path"
            ]

            repair = call_model(
                config,
                config[
                    "coder_model"
                ],
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    compact(
                        tests[
                            "output"
                        ]
                    )
                )
            )

            if repair["ok"]:
                write_file(
                    workspace,
                    path,
                    extract_code(
                        repair[
                            "response"
                        ]
                    )
                )

        candidate = run_command(
            workspace,
            config[
                "validation"
            ]["test"]
        )

        print(
            compact(
                candidate[
                    "output"
                ]
            )
        )

        if (
            candidate[
                "exit_code"
            ] == 0
        ):
            tests = candidate
            break

        score = failure_score(
            candidate[
                "output"
            ]
        )

        if score < best_score:
            print(
                "Test progress detected."
            )

            tests = candidate
            best_score = score

        else:
            print(
                "No test progress. "
                "Rolling back."
            )

            restore_snapshot(
                workspace,
                snapshot
            )

    if (
        tests[
            "exit_code"
        ] != 0
    ):
        print(
            "Tests did not converge."
        )

        git_restore_all(
            workspace
        )
        return

    counts = parse_test_counts(
        tests[
            "output"
        ]
    )

    state[
        "tests"
    ] = {
        "status": "pass",
        "passed":
            counts[
                "passed"
            ],
        "failed": 0
    }

    save_state(
        config,
        state
    )

    # ========================================================
    # REVIEW
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 7 - FINAL REVIEW")
    print("=" * 60)

    diff = git_diff(
        workspace
    )

    review = call_model(
        config,
        config[
            "reviewer_model"
        ],
        reviewer_prompt(
            task,
            plan,
            compact(
                diff,
                12000
            )
        ),
        json_mode=True
    )

    if not review["ok"]:
        print(
            review["error"]
        )
        return

    try:
        review_json = json.loads(
            review[
                "response"
            ]
        )
    except json.JSONDecodeError:
        print(
            "Reviewer returned invalid JSON."
        )
        return

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
        state[
            "review"
        ] = "approve"

        state[
            "phase"
        ] = "completed"

        save_state(
            config,
            state
        )

        append_history(
            config,
            "pipeline_completed",
            {
                "review": "approve"
            }
        )

        print()
        print("=" * 60)
        print(
            "FULL AGENT V2.4.2 "
            "PIPELINE PASSED"
        )
        print("=" * 60)

        print()
        print(
            "Changes remain uncommitted "
            "for human inspection."
        )

    else:
        state[
            "review"
        ] = "reject"

        save_state(
            config,
            state
        )

        print(
            "Reviewer rejected implementation."
        )


if __name__ == "__main__":
    main()
