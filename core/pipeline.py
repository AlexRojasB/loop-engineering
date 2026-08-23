import json

from core.phases.planning_phase import run_planning_phase
from core.phases.test_contract_phase import run_test_contract_phase
from core.phases.expected_red_phase import run_expected_red_phase
from core.phases.implementation_phase import run_implementation_phase
from core.phases.build_phase import run_build_phase
from core.phases.review_phase import run_review_phase
from core.phases.test_phase import run_test_phase

from core.context import (
    build_behavior_contract,
    implementation_text,
)
from core.guards import (
    extract_test_method_names,
    production_guard,
    validate_test_snippet,
)
from core.models import call_model
from core.planning import (
    group_changes_by_file,
    normalize_plan,
)
from core.prompts import render_prompt
from core.repository import (
    discover_files,
    ensure_clean_baseline,
    git_diff,
    git_restore_all,
    read_file,
    restore_snapshot,
    run_command,
    snapshot_files,
    write_file,
)
from core.state import (
    append_history,
    default_state,
    save_state,
)
from core.test_merge import merge_test_snippet
from core.utils import (
    compact,
    extract_code,
)
from core.validation import (
    classify_red_state,
    failure_score,
    parse_test_counts,
)













def run_pipeline(
    config,
    task,
    version
):
    workspace = config[
        "workspace"
    ]

    if not ensure_clean_baseline(
        workspace
    ):
        return False

    state = default_state(
        task
    )

    save_state(
        config,
        state
    )

    append_history(
        config,
        "run_started",
        {
            "version": version
        }
    )

    print()
    print("=" * 60)
    print(f"AGENT {version}")
    print("=" * 60)

    planning = run_planning_phase(
        config,
        workspace,
        task,
        state
    )

    if not planning:
        return False

    plan = planning["plan"]

    implementation_changes = (
        planning[
            "implementation_changes"
        ]
    )

    test_changes = (
        planning[
            "test_changes"
        ]
    )

    contract = (
        run_test_contract_phase(
            config,
            workspace,
            task,
            state,
            implementation_changes,
            test_changes
        )
    )

    if not contract:
        return False

    if not run_expected_red_phase(
        config,
        workspace,
        state,
        contract[
            "test_snapshot"
        ]
    ):
        return False

    if not run_implementation_phase(
        config,
        workspace,
        task,
        state,
        implementation_changes
    ):
        return False

    if not run_build_phase(
        config,
        workspace,
        task,
        implementation_changes
    ):
        print(
            "Build did not converge."
        )

        git_restore_all(
            workspace
        )

        return False

    state["build"] = "pass"
    save_state(
        config,
        state
    )

    if not run_test_phase(
        config,
        workspace,
        task,
        state,
        planning["grouped"],
        implementation_changes
    ):
        print(
            "Tests did not converge."
        )

        git_restore_all(
            workspace
        )

        return False

    if not run_review_phase(
        config,
        workspace,
        task,
        state,
        plan
    ):
        print(
            "Reviewer rejected pipeline."
        )

        return False

    print()
    print("=" * 60)
    print(
        f"FULL AGENT {version} "
        "PIPELINE PASSED"
    )
    print("=" * 60)

    print()
    print(
        "Changes remain uncommitted "
        "for human inspection."
    )

    return True
