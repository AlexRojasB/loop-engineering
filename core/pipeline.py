from time import perf_counter

from core.phases.build_phase import run_build_phase
from core.phases.expected_red_phase import run_expected_red_phase
from core.phases.implementation_phase import run_implementation_phase
from core.phases.agentic_implementation_phase import run_agentic_implementation_phase
from core.phases.planning_phase import run_planning_phase
from core.phases.review_phase import run_review_phase
from core.phases.test_contract_phase import run_test_contract_phase
from core.phases.test_phase import run_test_phase

from core.repository import (
    discover_files,
    ensure_clean_baseline,
    git_restore_all,
)

from core.resume import (
    inspect_resume_state,
    rebuild_execution_plan,
    validate_resume_request,
)

from core.state import (
    append_history,
    default_state,
    mark_phase_completed,
    mark_phase_started,
    mark_run_failed,
    phase_status,
    save_state,
)

from languages import detect_adapter


def run_pipeline(
    config,
    task,
    version
):
    workspace = config["workspace"]

    pipeline_started = perf_counter()
    phase_timings = {}

    def timed_phase(name, func):
        started = perf_counter()

        try:
            return func()
        finally:
            phase_timings[name] = (
                phase_timings.get(name, 0.0)
                + perf_counter()
                - started
            )

    def print_timing_summary():
        total = perf_counter() - pipeline_started

        print()
        print("=" * 60)
        print("TIMING SUMMARY")
        print("=" * 60)

        labels = [
            ("planning", "Planning"),
            ("test_contract", "Test Contract"),
            ("expected_red", "Expected Red"),
            ("implementation", "Implementation"),
            ("build", "Build"),
            ("tests", "Tests"),
            ("review", "Final Review"),
        ]

        for key, label in labels:
            if key in phase_timings:
                print(
                    f"{label:<24}"
                    f"{phase_timings[key]:>10.2f}s"
                )

        print("-" * 60)
        print(
            f"{'Total':<24}"
            f"{total:>10.2f}s"
        )
        print("=" * 60)

    def finish(success):
        print_timing_summary()
        return success

    # --------------------------------------------------------
    # Repository / language discovery
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # New run vs resume
    # --------------------------------------------------------

    resume_requested = bool(
        config.get(
            "resume",
            False
        )
    )

    resume_phase = None

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
            print("RESUME REJECTED")
            print(
                inspection[
                    "reason"
                ]
            )

            return finish(False)

        state = inspection[
            "state"
        ]

        resume_phase = state.get(
            "current_phase",
            state.get(
                "phase"
            )
        )

        resume_phase_status = state.get(
            "phase_status",
            "completed"
        )

        print()
        print(
            "Resuming persisted execution."
        )

        print(
            f"Resume phase: "
            f"{resume_phase}"
        )

        print(
            f"Resume phase status: "
            f"{resume_phase_status}"
        )

        append_history(
            config,
            "run_resumed",
            {
                "version": version,
                "phase": resume_phase
            }
        )

    else:
        resume_phase_status = None

        if not ensure_clean_baseline(
            workspace
        ):
            return finish(False)

        state = default_state(
            task
        )

        state["workspace"] = workspace

        state["selected_source"] = (
            config.get(
                "selected_source"
            )
        )

        state["agent_version"] = (
            version
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

    # --------------------------------------------------------
    # PHASE 1 - Planning
    # --------------------------------------------------------

    if (
        resume_requested
        and resume_phase
        != "planning"
    ):
        planning = rebuild_execution_plan(
            state
        )

        print()
        print(
            "Using persisted execution plan."
        )

    else:
        planning = timed_phase(
            "planning",
            lambda: run_planning_phase(
                config,
                workspace,
                task,
                state,
                config.get(
                    "project_context",
                    {}
                )
            )
        )

        if not planning:
            return finish(False)

    plan = planning[
        "plan"
    ]

    grouped_changes = planning[
        "grouped"
    ]

    implementation_changes = planning[
        "implementation_changes"
    ]

    test_changes = planning[
        "test_changes"
    ]

    tests_required = planning.get(
        "tests_required",
        plan.get(
            "tests_required",
            True
        )
    )

    if not plan:
        print(
            "Persisted execution plan "
            "is missing."
        )
        return finish(False)

    # --------------------------------------------------------
    # PHASE 2 - Test contract
    # --------------------------------------------------------

    contract = None

    if tests_required:
        if (
            not resume_requested
            or resume_phase
            == "planning"
        ):
            contract = timed_phase(
                "test_contract",
                lambda: run_test_contract_phase(
                    config,
                    workspace,
                    task,
                    state,
                    implementation_changes,
                    test_changes
                )
            )

            if not contract:
                return finish(False)

        else:
            print()
            print(
                "Preserving frozen test contract "
                "from interrupted run."
            )

    else:
        print()
        print("=" * 60)
        print("PHASE 2 - TEST CONTRACT SKIPPED")
        print("=" * 60)
        print(
            "Structural change: no new "
            "test contract required."
        )

    # --------------------------------------------------------
    # PHASE 3 - Expected RED
    # --------------------------------------------------------

    if tests_required:
        if (
            not resume_requested
            or resume_phase
            in (
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

            if not timed_phase(
                "expected_red",
                lambda: run_expected_red_phase(
                    config,
                    workspace,
                    state,
                    test_snapshot,
                    test_command
                )
            ):
                return finish(False)

        else:
            print()
            print(
                "Expected RED was already passed "
                "in the interrupted run."
            )

    else:
        print()
        print("=" * 60)
        print("PHASE 3 - EXPECTED RED SKIPPED")
        print("=" * 60)
        print(
            "Existing regression tests will "
            "still run after implementation."
        )

    # --------------------------------------------------------
    # PHASE 4 - Implementation
    # --------------------------------------------------------

    implementation_incomplete = (
        resume_requested
        and resume_phase
        == "implementation"
        and resume_phase_status
        != "completed"
    )

    if (
        not resume_requested
        or resume_phase
        in (
            "planning",
            "tests_frozen",
        )
        or implementation_incomplete
    ):
        if config.get(
            "agentic_implementation_enabled",
            False
        ):
            implementation_result = timed_phase(
                "implementation",
                lambda:
                    run_agentic_implementation_phase(
                        config,
                        workspace,
                        task,
                        state,
                        implementation_changes,
                        build_command,
                        test_command
                    )
            )

        else:
            implementation_result = timed_phase(
                "implementation",
                lambda:
                    run_implementation_phase(
                        config,
                        workspace,
                        task,
                        state,
                        implementation_changes
                    )
            )

        if not implementation_result:
            return finish(False)

    else:
        print()
        print(
            "Preserving existing production "
            "changes from interrupted run."
        )

    # --------------------------------------------------------
    # PHASE 5 - Build
    # --------------------------------------------------------

    mark_phase_started(
        config,
        state,
        "build"
    )

    if not timed_phase(
        "build",
        lambda: run_build_phase(
            config,
            workspace,
            task,
            implementation_changes,
            build_command
        )
    ):
        print(
            "Build did not converge."
        )

        git_restore_all(
            workspace
        )

        mark_run_failed(
            config,
            state,
            "Build did not converge.",
            rolled_back=True
        )

        return finish(False)

    state["build"] = "pass"

    mark_phase_completed(
        config,
        state,
        "build"
    )

    # --------------------------------------------------------
    # PHASE 6 - Tests
    # --------------------------------------------------------

    mark_phase_started(
        config,
        state,
        "tests"
    )

    if not timed_phase(
        "tests",
        lambda: run_test_phase(
            config,
            workspace,
            task,
            state,
            grouped_changes,
            implementation_changes,
            test_command
        )
    ):
        print(
            "Tests did not converge."
        )

        git_restore_all(
            workspace
        )

        mark_run_failed(
            config,
            state,
            "Tests did not converge.",
            rolled_back=True
        )

        return finish(False)

    mark_phase_completed(
        config,
        state,
        "tests"
    )

    # --------------------------------------------------------
    # PHASE 7 - Final review
    # --------------------------------------------------------

    mark_phase_started(
        config,
        state,
        "review"
    )

    if not timed_phase(
        "review",
        lambda: run_review_phase(
            config,
            workspace,
            task,
            state,
            plan
        )
    ):
        print(
            "Reviewer rejected pipeline."
        )

        return finish(False)

    mark_phase_completed(
        config,
        state,
        "review"
    )

    state["phase"] = "completed"
    state["current_phase"] = "completed"
    state["phase_status"] = "completed"

    save_state(
        config,
        state
    )

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

    return finish(True)
