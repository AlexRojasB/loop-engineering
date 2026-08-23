import json

from core.models import call_model
from core.planning import (
    group_changes_by_file,
    normalize_plan,
)
from core.prompts import render_prompt
from core.repository import discover_files
from core.state import (
    append_history,
    save_state,
)


def planner_prompt(
    task,
    files
):
    return render_prompt(
        "planner.md",
        task=task,
        files="\n".join(files)
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
            planner_result[
                "response"
            ]
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
        for change
        in grouped
        if change["type"]
        in (
            "implementation",
            "configuration"
        )
    ]

    test_changes = [
        change
        for change
        in grouped
        if change["type"]
        == "test"
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
        "plan":
            plan,

        "grouped":
            grouped,

        "implementation_changes":
            implementation_changes,

        "test_changes":
            test_changes
    }
