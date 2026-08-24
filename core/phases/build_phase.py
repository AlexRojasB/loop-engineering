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
from core.utils import (
    compact,
    extract_code,
)
from core.validation import failure_score


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
        frozen_tests="",
        failure=failure
    )


def run_build_phase(
    config,
    workspace,
    task,
    implementation_changes,
    build_command
):
    print()
    print("=" * 60)
    print("PHASE 5 - BUILD")
    print("=" * 60)

    build = run_command(
        workspace,
        build_command
    )

    print(
        compact(
            build["output"]
        )
    )

    for attempt in range(
        1,
        config["max_build_repairs"] + 1
    ):
        if build["exit_code"] == 0:
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
            build_command
        )

        print(
            compact(
                candidate["output"]
            )
        )

        if candidate["exit_code"] == 0:
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
