import json

from core.repository import (
    restore_snapshot,
    run_command,
)
from core.state import (
    append_history,
    mark_phase_completed,
    mark_phase_started,
    save_state,
)
from core.red_state import classify_expected_red
from core.spec_memory import record_spec_failure
from core.utils import compact


INVALID_CONTRACT_CLASSIFICATIONS = {
    "BROKEN_TEST_SUITE",
    "INVALID_CONTRACT",
}


def run_expected_red_phase(
    config,
    workspace,
    state,
    test_snapshot,
    test_command,
    adapter=None,
    task=""
):
    print()
    print("=" * 60)
    print("PHASE 3 - EXPECTED RED")
    print("=" * 60)

    mark_phase_started(
        config,
        state,
        "expected_red"
    )

    result = run_command(
        workspace,
        test_command
    )

    print(
        compact(
            result["output"]
        )
    )

    classification = classify_expected_red(
        result["output"],
        adapter=adapter,
        spec_text=task
    )

    # The structured adapter report is useful for history but far too
    # verbose for the console.
    diagnostics = classification.pop(
        "diagnostics",
        None
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
        {
            **classification,
            "diagnostics": diagnostics
        }
    )

    if result["exit_code"] == 0:
        print(
            "Tests already pass before implementation. "
            "Contract may be weak."
        )

        record_spec_failure(
            config,
            "expected_red",
            "A previous contract compiled and passed before "
            "any implementation existed, so it did not test "
            "the requested new behavior. Assert the requested "
            "behavior directly."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )

        return False

    if (
        classification["classification"]
        in INVALID_CONTRACT_CLASSIFICATIONS
    ):
        print(
            classification["reason"]
        )

        for issue in (
            (diagnostics or {}).get("issues", [])
        )[:4]:
            record_spec_failure(
                config,
                "expected_red",
                issue
            )

        record_spec_failure(
            config,
            "expected_red",
            "A previous contract failed Expected RED as "
            f"{classification['classification']}: "
            + classification["reason"]
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )

        return False

    state[
        "expected_red_confirmed"
    ] = True

    mark_phase_completed(
        config,
        state,
        "expected_red"
    )

    return True
