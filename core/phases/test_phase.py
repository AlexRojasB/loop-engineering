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


def run_test_phase(
    config,
    workspace,
    task,
    state,
    grouped_changes,
    implementation_changes
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
        config["validation"]["test"]
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
        config["max_test_repairs"] + 1
    ):
        if tests["exit_code"] == 0:
            break

        print()
        print(
            f"TEST REPAIR {attempt}"
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
            config["validation"]["test"]
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
