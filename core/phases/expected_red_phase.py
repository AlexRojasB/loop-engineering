import json

from core.repository import (
    restore_snapshot,
    run_command,
)
from core.state import (
    append_history,
    save_state,
)
from core.utils import compact
from core.validation import classify_red_state


def run_expected_red_phase(
    config,
    workspace,
    state,
    test_snapshot,
    test_command
):
    print()
    print("=" * 60)
    print("PHASE 3 - EXPECTED RED")
    print("=" * 60)

    result = run_command(
        workspace,
        test_command
    )

    print(
        compact(
            result["output"]
        )
    )

    classification = classify_red_state(
        result["output"]
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
        classification
    )

    if result["exit_code"] == 0:
        print(
            "Tests already pass before implementation. "
            "Contract may be weak."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )

        return False

    if (
        classification["classification"]
        == "BROKEN_TEST_SUITE"
    ):
        print(
            "Broken generated test suite."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )

        return False

    state[
        "expected_red_confirmed"
    ] = True

    save_state(
        config,
        state
    )

    return True
