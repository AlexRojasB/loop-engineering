import json

from core.context import (
    build_behavior_contract,
    implementation_text,
)
from core.guards import (
    extract_test_method_names,
    production_guard,
    validate_test_snippet,
)
from core.models import call_model
from core.planning import (
    group_changes_by_file,
    normalize_plan,
)
from core.prompts import render_prompt
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
from core.state import (
    append_history,
    default_state,
    save_state,
)
from core.test_merge import merge_test_snippet
from core.utils import (
    compact,
    extract_code,
)
from core.validation import (
    classify_red_state,
    failure_score,
    parse_test_counts,
)


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


def implementation_prompt(
    task,
    file_change,
    current_content
):
    return render_prompt(
        "coder.md",
        behavior_contract=
            build_behavior_contract(
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
        for reason
        in file_change["reasons"]
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
    return render_prompt(
        "reviewer.md",
        task=task,
        plan=json.dumps(
            plan,
            indent=2
        ),
        diff=diff
    )


def run_planning_phase(
    config,
    workspace,
    task,
    state
):
    print()
    print("=" * 60)
    print("PHASE 1 - PLANNING")
    print("=" * 60)

    state["phase"] = "planning"
    save_state(
        config,
        state
    )

    files = discover_files(
        workspace
    )

    planner_result = call_model(
        config,
        config["planner_model"],
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
        return None

    try:
        planner_plan = json.loads(
            planner_result["response"]
        )
    except json.JSONDecodeError:
        print(
            "Planner returned invalid JSON."
        )
        return None

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

    if plan[
        "dependencies_required"
    ]:
        print(
            "Dependency tooling "
            "not implemented yet."
        )
        return None

    grouped = group_changes_by_file(
        plan["changes"]
    )

    implementation_changes = [
        change
        for change in grouped
        if change["type"]
        in (
            "implementation",
            "configuration"
        )
    ]

    test_changes = [
        change
        for change in grouped
        if change["type"] == "test"
    ]

    if not implementation_changes:
        print(
            "No implementation changes planned."
        )
        return None

    if not test_changes:
        print(
            "No test changes planned."
        )
        return None

    return {
        "plan": plan,
        "grouped": grouped,
        "implementation_changes":
            implementation_changes,
        "test_changes":
            test_changes
    }


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
        for change in test_changes
    ]

    test_snapshot = snapshot_files(
        workspace,
        test_paths
    )

    frozen_tests = {}

    for test_change in test_changes:
        path = test_change["path"]

        original = test_snapshot[
            path
        ]

        generated = call_model(
            config,
            config["coder_model"],
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
            generated["response"]
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
                    config["coder_model"],
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
                    revision["response"]
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

            if not review["ok"]:
                continue

            try:
                review_json = json.loads(
                    review["response"]
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
                config["coder_model"],
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

            if revision["ok"]:
                snippet = extract_code(
                    revision["response"]
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

            return None

        final_merged = (
            merge_test_snippet(
                original,
                snippet
            )
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


def run_expected_red_phase(
    config,
    workspace,
    state,
    test_snapshot
):
    print()
    print("=" * 60)
    print("PHASE 3 - EXPECTED RED")
    print("=" * 60)

    result = run_command(
        workspace,
        config[
            "validation"
        ]["test"]
    )

    print(
        compact(
            result["output"]
        )
    )

    classification = (
        classify_red_state(
            result["output"]
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

    if result[
        "exit_code"
    ] == 0:
        print(
            "Tests already pass before "
            "implementation. "
            "Contract may be weak."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )

        return False

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

        return False

    state[
        "expected_red_confirmed"
    ] = True

    save_state(
        config,
        state
    )

    return True


def run_implementation_phase(
    config,
    workspace,
    task,
    state,
    implementation_changes
):
    print()
    print("=" * 60)
    print("PHASE 4 - IMPLEMENTATION")
    print("=" * 60)

    state[
        "phase"
    ] = "implementation"

    save_state(
        config,
        state
    )

    snapshot = snapshot_files(
        workspace,
        [
            change["path"]
            for change
            in implementation_changes
        ]
    )

    for change in implementation_changes:
        path = change["path"]

        current = read_file(
            workspace,
            path
        )

        result = call_model(
            config,
            config["coder_model"],
            implementation_prompt(
                task,
                change,
                current
            )
        )

        if not result["ok"]:
            restore_snapshot(
                workspace,
                snapshot
            )
            return False

        generated = extract_code(
            result["response"]
        )

        issues = production_guard(
            generated
        )

        if issues:
            print()
            print(
                "PRODUCTION GUARD: REJECT"
            )

            for issue in issues:
                print(
                    f"- {issue}"
                )

            restore_snapshot(
                workspace,
                snapshot
            )

            return False

        write_file(
            workspace,
            path,
            generated
        )

    state[
        "implementation_generated"
    ] = True

    save_state(
        config,
        state
    )

    return True


def run_build_phase(
    config,
    workspace,
    task,
    implementation_changes
):
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
            build["output"]
        )
    )

    for attempt in range(
        1,
        config[
            "max_build_repairs"
        ] + 1
    ):
        if build[
            "exit_code"
        ] == 0:
            return True

        print()
        print(
            f"BUILD REPAIR {attempt}"
        )

        snapshot = snapshot_files(
            workspace,
            [
                change["path"]
                for change
                in implementation_changes
            ]
        )

        old_score = failure_score(
            build["output"]
        )

        for change in implementation_changes:
            path = change["path"]

            result = call_model(
                config,
                config["coder_model"],
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    compact(
                        build["output"]
                    )
                )
            )

            if not result["ok"]:
                continue

            generated = extract_code(
                result["response"]
            )

            guard_issues = (
                production_guard(
                    generated
                )
            )

            if guard_issues:
                continue

            write_file(
                workspace,
                path,
                generated
            )

        candidate = run_command(
            workspace,
            config[
                "validation"
            ]["build"]
        )

        print(
            compact(
                candidate["output"]
            )
        )

        if candidate[
            "exit_code"
        ] == 0:
            return True

        new_score = failure_score(
            candidate["output"]
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

    return False


def run_test_phase(
    config,
    workspace,
    task,
    state,
    implementation_changes
):
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
            tests["output"]
        )
    )

    best_score = failure_score(
        tests["output"]
    )

    for attempt in range(
        1,
        config[
            "max_test_repairs"
        ] + 1
    ):
        if tests[
            "exit_code"
        ] == 0:
            break

        print()
        print(
            f"TEST REPAIR {attempt}"
        )

        snapshot = snapshot_files(
            workspace,
            [
                change["path"]
                for change
                in implementation_changes
            ]
        )

        for change in implementation_changes:
            path = change["path"]

            result = call_model(
                config,
                config["coder_model"],
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    compact(
                        tests["output"]
                    )
                )
            )

            if not result["ok"]:
                continue

            generated = extract_code(
                result["response"]
            )

            if production_guard(
                generated
            ):
                continue

            write_file(
                workspace,
                path,
                generated
            )

        candidate = run_command(
            workspace,
            config[
                "validation"
            ]["test"]
        )

        print(
            compact(
                candidate["output"]
            )
        )

        if candidate[
            "exit_code"
        ] == 0:
            tests = candidate
            break

        score = failure_score(
            candidate["output"]
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

    if tests[
        "exit_code"
    ] != 0:
        return False

    counts = parse_test_counts(
        tests["output"]
    )

    state["tests"] = {
        "status": "pass",
        "passed":
            counts["passed"],
        "failed": 0
    }

    save_state(
        config,
        state
    )

    return True


def run_review_phase(
    config,
    workspace,
    task,
    state,
    plan
):
    print()
    print("=" * 60)
    print("PHASE 7 - FINAL REVIEW")
    print("=" * 60)

    diff = git_diff(
        workspace
    )

    result = call_model(
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

    if not result["ok"]:
        print(
            result["error"]
        )
        return False

    try:
        review = json.loads(
            result["response"]
        )
    except json.JSONDecodeError:
        print(
            "Reviewer returned invalid JSON."
        )
        return False

    print(
        json.dumps(
            review,
            indent=2
        )
    )

    if (
        review.get(
            "decision",
            ""
        ).upper()
        != "APPROVE"
    ):
        state[
            "review"
        ] = "reject"

        save_state(
            config,
            state
        )

        return False

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

    return True


def run_pipeline(
    config,
    task,
    version
):
    workspace = config[
        "workspace"
    ]

    if not ensure_clean_baseline(
        workspace
    ):
        return False

    state = default_state(
        task
    )

    save_state(
        config,
        state
    )

    append_history(
        config,
        "run_started",
        {
            "version": version
        }
    )

    print()
    print("=" * 60)
    print(f"AGENT {version}")
    print("=" * 60)

    planning = run_planning_phase(
        config,
        workspace,
        task,
        state
    )

    if not planning:
        return False

    plan = planning["plan"]

    implementation_changes = (
        planning[
            "implementation_changes"
        ]
    )

    test_changes = (
        planning[
            "test_changes"
        ]
    )

    contract = (
        run_test_contract_phase(
            config,
            workspace,
            task,
            state,
            implementation_changes,
            test_changes
        )
    )

    if not contract:
        return False

    if not run_expected_red_phase(
        config,
        workspace,
        state,
        contract[
            "test_snapshot"
        ]
    ):
        return False

    if not run_implementation_phase(
        config,
        workspace,
        task,
        state,
        implementation_changes
    ):
        return False

    if not run_build_phase(
        config,
        workspace,
        task,
        implementation_changes
    ):
        print(
            "Build did not converge."
        )

        git_restore_all(
            workspace
        )

        return False

    state["build"] = "pass"
    save_state(
        config,
        state
    )

    if not run_test_phase(
        config,
        workspace,
        task,
        state,
        implementation_changes
    ):
        print(
            "Tests did not converge."
        )

        git_restore_all(
            workspace
        )

        return False

    if not run_review_phase(
        config,
        workspace,
        task,
        state,
        plan
    ):
        print(
            "Reviewer rejected pipeline."
        )

        return False

    print()
    print("=" * 60)
    print(
        f"FULL AGENT {version} "
        "PIPELINE PASSED"
    )
    print("=" * 60)

    print()
    print(
        "Changes remain uncommitted "
        "for human inspection."
    )

    return True
