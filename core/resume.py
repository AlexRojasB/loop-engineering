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

    if phase not in SAFE_RESUME_PHASES:
        return {
            "can_resume": False,
            "state": state,
            "phase": phase,
            "reason":
                f"Phase is not resumable: {phase}"
        }

    status = git_status(
        workspace
    )

    return {
        "can_resume": True,
        "state": state,
        "phase": phase,
        "git_status": status,
        "reason":
            f"Resume candidate found at phase: {phase}"
    }


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

    lines.append(
        inspection["reason"]
    )

    git_status_value = (
        inspection.get(
            "git_status"
        )
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
