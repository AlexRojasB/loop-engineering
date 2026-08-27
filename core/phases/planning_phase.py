import json

from core.context import build_project_planner_context
from core.models import call_model
from core.planning import (
    group_changes_by_file,
    normalize_plan,
)
from core.prompts import render_prompt
from core.repository import discover_files
from core.state import (
    append_history,
    mark_phase_completed,
    mark_phase_started,
    save_state,
)


def planner_prompt(
    task,
    files,
    project_context
):
    context = build_project_planner_context(
        task,
        project_context,
        files
    )

    authoritative_text = "\n\n".join(
        (
            f"Source: {item['path']}\n"
            f"Category: {item['category']}\n"
            f"{item['content']}"
        )
        for item in context[
            "authoritative_context"
        ]
    )

    supporting_text = "\n\n".join(
        (
            f"Source: {item['path']}\n"
            f"Category: {item['category']}\n"
            f"{item['content']}"
        )
        for item in context[
            "supporting_context"
        ]
    )

    if not authoritative_text:
        authoritative_text = (
            "No additional authoritative "
            "project context."
        )

    if not supporting_text:
        supporting_text = (
            "No supporting project context."
        )

    return render_prompt(
        "planner.md",
        task=context["task"],
        authoritative_context=
            authoritative_text,
        supporting_context=
            supporting_text,
        files="\n".join(
            context[
                "repository_files"
            ]
        )
    )


def run_planning_phase(
    config,
    workspace,
    task,
    state,
    project_context,
    isolation=None
):
    print()
    print("=" * 60)
    print("PHASE 1 - PLANNING")
    print("=" * 60)

    mark_phase_started(
        config,
        state,
        "planning"
    )

    files = discover_files(
        workspace,
        isolation=isolation
    )

    planner_result = call_model(
        config,
        config["planner_model"],
        planner_prompt(
            task,
            files,
            project_context
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

    mark_phase_completed(
        config,
        state,
        "planning"
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

    state["plan"] = plan
    state["grouped_changes"] = grouped

    save_state(
        config,
        state
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
        if change["type"] == "test"
    ]

    if not implementation_changes:
        print(
            "No implementation changes planned."
        )
        return None

    tests_required = bool(
        plan.get(
            "tests_required",
            True
        )
    )

    if (
        not test_changes
        and tests_required
    ):
        print(
            "Tests are required but "
            "no test changes were planned."
        )
        return None

    if (
        test_changes
        and not tests_required
    ):
        print(
            "Planner proposed test changes; "
            "forcing tests_required=True."
        )
        tests_required = True
        plan["tests_required"] = True

    if not tests_required:
        print(
            "No new test contract required "
            "for this structural change."
        )

    return {
        "plan":
            plan,

        "grouped":
            grouped,

        "implementation_changes":
            implementation_changes,

        "test_changes":
            test_changes,

        "tests_required":
            tests_required
    }
