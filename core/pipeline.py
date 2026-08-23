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
from core.resume import (
    inspect_resume_state,
    rebuild_execution_plan,
    validate_resume_request,
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
from languages import detect_adapter
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

    repository_files = discover_files(
        workspace
    )

    adapter = detect_adapter(
        repository_files
    )

    build_command = adapter.build_command(
        repository_files
    )

    test_command = adapter.test_command(
        repository_files
    )

    print(
        f"Detected language adapter: "
        f"{adapter.name}"
    )

    print(
        f"Build command: {build_command}"
    )

    print(
        f"Test command: {test_command}"
    )

    resume_requested = bool(
        config.get(
            "resume",
            False
        )
    )

    if resume_requested:
        inspection = inspect_resume_state(
            config,
            workspace
        )

        inspection = validate_resume_request(
            inspection,
            config.get(
                "selected_source"
            )
        )

        if not inspection[
            "can_resume"
        ]:
            print()
            print(
                "RESUME REJECTED"
            )
            print(
                inspection[
                    "reason"
                ]
            )
            return False

        state = inspection[
            "state"
        ]

        print()
        print(
            "Resuming persisted execution."
        )
        print(
            f"Resume phase: "
            f"{state.get('phase')}"
        )

        append_history(
            config,
            "run_resumed",
            {
                "version": version,
                "phase":
                    state.get(
                        "phase"
                    )
            }
        )

    else:
        if not ensure_clean_baseline(
            workspace
        ):
            return False

        state = default_state(
            task
        )

        state["workspace"] = workspace
        state["selected_source"] = config.get(
            "selected_source"
        )
        state["agent_version"] = version

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
        state,
        config.get(
            "project_context",
            {}
        )
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

    if (
        resume_requested
        and resume_phase
        in (
            "tests_frozen",
            "implementation",
            "build",
            "tests",
            "review",
        )
    ):
        contract = None

    else:
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

    if (
        not resume_requested
        or resume_phase
        in (
            None,
            "planning",
            "tests_frozen",
        )
    ):
        test_snapshot = (
            contract[
                "test_snapshot"
            ]
            if contract
            else {}
        )

        if not run_expected_red_phase(
            config,
            workspace,
            state,
            test_snapshot,
            test_command
        ):
            return False

    if (
        not resume_requested
        or resume_phase
        in (
            None,
            "planning",
            "tests_frozen",
        )
    ):
        if not run_implementation_phase(
            config,
            workspace,
            task,
            state,
            implementation_changes
        ):
            return False

    else:
        print()
        print(
            "Preserving existing production "
            "changes from interrupted run."
        )

    if not run_build_phase(
        config,
        workspace,
        task,
        implementation_changes,
        build_command
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
        implementation_changes,
        test_command
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
