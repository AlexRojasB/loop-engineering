from core.guards import production_guard
from core.models import call_model
from core.prompts import render_prompt
from core.repository import (
    read_file,
    restore_snapshot,
    run_command,
    snapshot_files,
    write_file,
)
from core.state import save_state
from core.utils import (
    compact,
    extract_code,
)
from core.validation import (
    choose_repair_targets,
    failure_score,
    parse_test_counts,
)


def repair_prompt(
    task,
    file_change,
    current_content,
    failure,
    frozen_tests
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
        frozen_tests=frozen_tests,
        failure=failure
    )


def run_test_phase(
    config,
    workspace,
    task,
    state,
    grouped_changes,
    implementation_changes,
    test_command
):
    print()
    print("=" * 60)
    print("PHASE 6 - TESTS")
    print("=" * 60)

    state["phase"] = "tests"

    save_state(
        config,
        state
    )

    tests = run_command(
        workspace,
        test_command
    )

    print(
        compact(
            tests["output"]
        )
    )

    best_score = failure_score(
        tests["output"]
    )

    frozen_test_blocks = []

    for change in grouped_changes:
        if change["type"] != "test":
            continue

        frozen_test_blocks.append(
            "===== FROZEN TEST FILE: "
            f"{change['path']} =====\n"
            + read_file(
                workspace,
                change["path"]
            )
        )

    frozen_tests = "\n\n".join(
        frozen_test_blocks
    )

    print()
    print("===== FROZEN TEST CONTRACT =====")
    print(frozen_tests)
    print("===== END FROZEN TEST CONTRACT =====")
    print()

    repair_history = []

    for attempt in range(
        1,
        config["max_test_repairs"] + 1
    ):
        if tests["exit_code"] == 0:
            break

        print()
        print(
            f"TEST REPAIR {attempt}"
        )

        escalation_after = config.get(
            "escalation_after_test_repairs",
            2
        )

        if attempt > escalation_after:
            repair_model = config.get(
                "escalation_model",
                config["coder_model"]
            )

            repair_role = "ESCALATION"

        else:
            repair_model = config[
                "coder_model"
            ]

            repair_role = "CODER"

        print(
            f"Repair role: {repair_role}"
        )
        print(
            f"Repair model: {repair_model}"
        )

        routing = choose_repair_targets(
            tests["output"],
            workspace,
            grouped_changes,
            tests_frozen=bool(
                state.get(
                    "tests_frozen",
                    False
                )
            )
        )

        print(
            "Failure owner:",
            routing["ownership"]["owner"]
        )

        if routing[
            "ownership"
        ]["paths"]:
            print(
                "Failure paths:"
            )

            for path in routing[
                "ownership"
            ]["paths"]:
                print(
                    f"- {path}"
                )

        if (
            routing["action"]
            == "reject_frozen_test_contract"
        ):
            print()
            print(
                "FROZEN TEST CONTRACT ERROR"
            )
            print(
                "Validation failure originates "
                "from the frozen test contract."
            )
            print(
                "Production repair will NOT run."
            )

            state[
                "last_error"
            ] = (
                "Frozen test contract caused "
                "validation failure."
            )

            save_state(
                config,
                state
            )

            return False

        repair_targets = routing.get(
            "targets",
            []
        )

        if not repair_targets:
            print(
                "No valid repair targets."
            )

            return False

        print(
            "Repair targets:"
        )

        for change in repair_targets:
            print(
                f"- {change['path']}"
            )

        target_paths = [
            change["path"]
            for change
            in repair_targets
        ]

        snapshot = snapshot_files(
            workspace,
            target_paths
        )

        repaired_any = False

        for change in repair_targets:
            if change["type"] == "test":
                continue

            path = change["path"]

            repair_failure = compact(
                tests["output"]
            )

            if repair_history:
                repair_failure += (
                    "\n\n===== PREVIOUS FAILED REPAIR ATTEMPTS =====\n"
                    + "\n\n".join(repair_history)
                    + "\n\nIMPORTANT:\n"
                    "The approaches above did not improve the test result. "
                    "Do not repeat the same implementation. Re-evaluate the "
                    "frozen test contract and the production state-transition "
                    "logic before producing a new repair."
                )

            if repair_role == "ESCALATION":
                repair_failure = (
                    "ESCALATED REPAIR.\n\n"
                    "Previous production repair attempts "
                    "did not reduce this failure.\n"
                    "Reason carefully about the business "
                    "state transition and identify the "
                    "root cause before producing the "
                    "complete corrected file.\n"
                    "Do not remove unrelated production "
                    "code or the executable entry point."
                    "\n\n"
                    + repair_failure
                )

            result = call_model(
                config,
                repair_model,
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    repair_failure,
                    compact(
                        frozen_tests,
                        10000
                    )
                )
            )

            if not result["ok"]:
                continue

            generated = extract_code(
                result["response"]
            )

            attempted_content = generated

            print()
            print("===== GENERATED TEST REPAIR =====")
            print(f"Attempt: {attempt}")
            print(f"Role: {repair_role}")
            print(f"Model: {repair_model}")
            print(f"Target: {path}")
            print("--------------------------------")
            print(generated)
            print("===== END GENERATED TEST REPAIR =====")
            print()

            guard_issues = production_guard(
                generated
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

                continue

            write_file(
                workspace,
                path,
                generated
            )

            repaired_any = True

        if not repaired_any:
            print(
                "No production repair "
                "was successfully generated."
            )

            restore_snapshot(
                workspace,
                snapshot
            )

            return False

        candidate = run_command(
            workspace,
            test_command
        )

        print(
            compact(
                candidate["output"]
            )
        )

        if candidate["exit_code"] == 0:
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

            repair_history.append(
                "Attempt "
                + str(attempt)
                + " produced this validation result:\n"
                + compact(
                    candidate["output"],
                    4000
                )
                + "\n\nProduction code attempted:\n"
                + compact(
                    attempted_content,
                    5000
                )
            )

            restore_snapshot(
                workspace,
                snapshot
            )

    if tests["exit_code"] != 0:
        return False

    counts = parse_test_counts(
        tests["output"]
    )

    state["tests"] = {
        "status": "pass",
        "passed": counts["passed"],
        "failed": 0
    }

    save_state(
        config,
        state
    )

    return True
