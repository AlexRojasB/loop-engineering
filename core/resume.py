from pathlib import Path

from core.repository import git_status
from core.state import load_state


SAFE_RESUME_PHASES = {
    "planning",
    "tests_frozen",
    "implementation",
    "build",
    "tests",
    "review",
}


def inspect_resume_state(
    config,
    workspace
):
    state = load_state(
        config
    )

    if state is None:
        return {
            "can_resume": False,
            "state": None,
            "phase": None,
            "reason":
                "No persisted agent state exists."
        }

    phase = state.get(
        "phase"
    )

    if phase == "completed":
        return {
            "can_resume": False,
            "state": state,
            "phase": phase,
            "reason":
                "The previous run already completed."
        }

    if phase == "failed":
        return {
            "can_resume": False,
            "state": state,
            "phase": phase,
            "reason":
                "The previous run failed and its "
                "workspace changes were rolled back."
        }

    if phase not in SAFE_RESUME_PHASES:
        return {
            "can_resume": False,
            "state": state,
            "phase": phase,
            "reason":
                f"Phase is not resumable: {phase}"
        }

    persisted_workspace = state.get(
        "workspace"
    )

    current_workspace = str(
        Path(workspace).resolve()
    )

    if (
        persisted_workspace
        and str(
            Path(
                persisted_workspace
            ).resolve()
        )
        != current_workspace
    ):
        return {
            "can_resume": False,
            "state": state,
            "phase": phase,
            "reason":
                "Persisted state belongs to "
                "a different workspace."
        }

    return {
        "can_resume": True,
        "state": state,
        "phase": phase,
        "git_status":
            git_status(
                workspace
            ),
        "reason":
            f"Resume candidate found at phase: "
            f"{phase}"
    }


def validate_resume_request(
    inspection,
    selected_source
):
    if not inspection[
        "can_resume"
    ]:
        return inspection

    state = inspection[
        "state"
    ]

    persisted_source = state.get(
        "selected_source"
    )

    if (
        persisted_source
        and selected_source
        and persisted_source
        != selected_source
    ):
        result = dict(
            inspection
        )

        result[
            "can_resume"
        ] = False

        result[
            "reason"
        ] = (
            "The requested project source differs "
            "from the persisted work item. "
            f"Persisted: {persisted_source}; "
            f"requested: {selected_source}"
        )

        return result

    phase = state.get(
        "phase"
    )

    if (
        phase != "planning"
        and not state.get(
            "plan"
        )
    ):
        result = dict(
            inspection
        )

        result[
            "can_resume"
        ] = False

        result[
            "reason"
        ] = (
            "Persisted state has no execution plan. "
            "Safe resume is not possible."
        )

        return result

    return inspection


def format_resume_report(
    inspection
):
    lines = [
        f"Resume available: "
        f"{inspection['can_resume']}"
    ]

    phase = inspection.get(
        "phase"
    )

    if phase:
        lines.append(
            f"Persisted phase: {phase}"
        )

    state = inspection.get(
        "state"
    )

    if state:
        selected_source = state.get(
            "selected_source"
        )

        if selected_source:
            lines.append(
                "Persisted source: "
                f"{selected_source}"
            )

    lines.append(
        inspection[
            "reason"
        ]
    )

    git_status_value = inspection.get(
        "git_status"
    )

    if git_status_value is not None:
        lines.append(
            "Git working tree:"
        )

        if git_status_value.strip():
            lines.append(
                git_status_value.rstrip()
            )
        else:
            lines.append(
                "clean"
            )

    return "\n".join(
        lines
    )


def rebuild_execution_plan(
    state
):
    grouped = state.get(
        "grouped_changes",
        []
    )

    implementation_changes = [
        change
        for change in grouped
        if change.get(
            "type"
        )
        in (
            "implementation",
            "configuration"
        )
    ]

    test_changes = [
        change
        for change in grouped
        if change.get(
            "type"
        ) == "test"
    ]

    return {
        "plan":
            state.get(
                "plan"
            ),

        "grouped":
            grouped,

        "implementation_changes":
            implementation_changes,

        "test_changes":
            test_changes
    }
