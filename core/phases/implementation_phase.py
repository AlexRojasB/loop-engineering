from core.context import build_behavior_contract
from core.guards import production_guard
from core.models import call_model
from core.prompts import render_prompt
from core.repository import (
    read_file,
    restore_snapshot,
    snapshot_files,
    write_file,
)
from core.state import save_state
from core.utils import extract_code


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

    state["phase"] = "implementation"

    save_state(
        config,
        state
    )

    snapshot = snapshot_files(
        workspace,
        [
            change["path"]
            for change in implementation_changes
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
