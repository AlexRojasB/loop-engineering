import json

from core.models import call_model
from core.prompts import render_prompt
from core.repository import git_diff
from core.state import (
    append_history,
    save_state,
)
from core.utils import compact


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
        config["reviewer_model"],
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
        state["review"] = "reject"

        save_state(
            config,
            state
        )

        return False

    state["review"] = "approve"
    state["phase"] = "completed"

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
