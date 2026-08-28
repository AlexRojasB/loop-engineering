from time import perf_counter

from core.phases.build_phase import run_build_phase
from core.phases.expected_red_phase import run_expected_red_phase
from core.phases.implementation_phase import run_implementation_phase
from core.phases.agentic_implementation_phase import run_agentic_implementation_phase
from core.phases.planning_phase import run_planning_phase
from core.phases.review_phase import run_review_phase
from core.phases.test_contract_phase import run_test_contract_phase
from core.phases.test_phase import run_test_phase

from core.contract_challenge import (
    DEFAULT_MAX_REOPENS,
    challenge_memory_entry,
)
from core.isolation import WorkIsolation
from core.phases.agentic_implementation_phase import (
    COMPLETED,
    CONTRACT_CHALLENGED,
    normalize_implementation_outcome,
)
from core.phases.test_contract_phase import snippet_fingerprint
from core.repository import (
    discover_files,
    ensure_clean_baseline,
    restore_snapshot,
    rollback_repository,
)
from core.spec_memory import record_spec_failure

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

    isolation = WorkIsolation.from_dict(
        config.get(
            "isolation"
        )
    )

    if isolation.active:
        print()
        print(
            isolation.describe()
        )

    repository_files = discover_files(
        workspace,
        isolation=isolation
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

    # A resumed run never established a clean baseline, so it must not
    # delete untracked files it cannot attribute to itself.
    baseline_verified = False

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

        # This run proved the repository was clean before touching it,
        # so any untracked file present later was created by this
        # attempt and may be removed on rollback.
        baseline_verified = True

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
                ),
                isolation
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
    # PHASES 2-4 - Test Contract / Expected RED / Implementation
    #
    # These three phases are one BOUNDED CYCLE, not a straight line.
    #
    # The implementation phase can now return a third answer: a
    # CONFIRMED contract challenge. That means independent validation
    # reproduced a cited failure and two independent reviewers agreed
    # the frozen contract itself cannot be satisfied by any correct
    # implementation. Continuing would spend the rest of the attempt
    # against a contract already proved impossible (Ledger Full #2:
    # correctly diagnosed at agentic step 6, abandoned at step 31), so
    # control returns to the Test Contract phase.
    #
    # Strictly bounded and fail-closed:
    # - only a CONFIRMED challenge reopens anything;
    # - at most max_contract_reopens reopenings per spec attempt;
    # - the confirmed defect goes into cross-attempt memory, and the
    #   exact frozen contract that was disproved is forbidden, so the
    #   next contract cannot be a reformat of the same one;
    # - once the reopen budget is spent, the attempt fails normally and
    #   the outer SPEC ATTEMPT loop takes over with memory intact.
    # --------------------------------------------------------

    max_contract_reopens = max(
        0,
        int(
            config.get(
                "max_contract_reopens",
                DEFAULT_MAX_REOPENS
            )
        )
    )

    # At least one cycle always runs: a misconfigured negative reopen
    # budget must disable reopening, never skip the phases.
    max_contract_cycles = (
        max_contract_reopens + 1
    )

    forbidden_fingerprints = set(
        config.get(
            "forbidden_contract_fingerprints"
        )
        or ()
    )

    contract = None

    for contract_cycle in range(
        1,
        max_contract_cycles + 1
    ):
        first_cycle = contract_cycle == 1

        cycle_resume = (
            resume_requested
            and first_cycle
        )

        if not first_cycle:
            print()
            print("=" * 60)
            print(
                "TEST CONTRACT REOPENED "
                f"({contract_cycle}/"
                f"{max_contract_cycles})"
            )
            print("=" * 60)

            repository_files = discover_files(
                workspace,
                isolation=isolation
            )

        config[
            "forbidden_contract_fingerprints"
        ] = set(
            forbidden_fingerprints
        )

        # ----------------------------------------------------
        # PHASE 2 - Test contract
        # ----------------------------------------------------

        if tests_required:
            if (
                not cycle_resume
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
                        test_changes,
                        adapter,
                        repository_files
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

        # ----------------------------------------------------
        # PHASE 3 - Expected RED
        # ----------------------------------------------------

        if tests_required:
            if (
                not cycle_resume
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
                        test_command,
                        adapter,
                        task
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

        # ----------------------------------------------------
        # PHASE 4 - Implementation
        # ----------------------------------------------------

        implementation_incomplete = (
            cycle_resume
            and resume_phase
            == "implementation"
            and resume_phase_status
            != "completed"
        )

        if (
            not cycle_resume
            or resume_phase
            in (
                "planning",
                "tests_frozen",
            )
            or implementation_incomplete
        ):
            frozen_tests = (
                contract.get(
                    "frozen_tests"
                )
                if contract
                else None
            )

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
                            test_command,
                            adapter,
                            repository_files,
                            isolation,
                            frozen_tests=frozen_tests
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

            outcome = (
                normalize_implementation_outcome(
                    implementation_result
                )
            )

        else:
            print()
            print(
                "Preserving existing production "
                "changes from interrupted run."
            )

            outcome = {
                "status": COMPLETED
            }

        if outcome["status"] == COMPLETED:
            break

        if (
            outcome["status"]
            != CONTRACT_CHALLENGED
        ):
            return finish(False)

        # --- confirmed contract challenge -------------------

        challenge = outcome[
            "challenge"
        ]

        print()
        print("=" * 60)
        print(
            "FROZEN CONTRACT CHALLENGE CONFIRMED"
        )
        print("=" * 60)

        for reason in (
            outcome["verdict"].get(
                "reasons"
            )
            or []
        ):
            print(f"- {reason}")

        if contract:
            for content in (
                contract.get(
                    "frozen_tests"
                )
                or {}
            ).values():
                forbidden_fingerprints.add(
                    snippet_fingerprint(
                        content
                    )
                )

        # Survives the outer SPEC ATTEMPT restore, so a later attempt
        # never regenerates the disproved contract from scratch.
        record_spec_failure(
            config,
            "contract/challenge_confirmed",
            challenge_memory_entry(
                challenge
            )
        )

        if contract_cycle >= max_contract_cycles:
            print()
            print(
                "Contract reopen budget exhausted. "
                "Failing this spec attempt."
            )

            residual = rollback_repository(
                workspace,
                clean_untracked=baseline_verified
            )

            if residual.strip():
                print(
                    "WARNING: repository is still not clean "
                    "after rollback:"
                )
                print(residual)

            mark_run_failed(
                config,
                state,
                "Frozen test contract was challenged and "
                "confirmed inconsistent, and the contract reopen "
                "budget is exhausted.",
                rolled_back=True
            )

            return finish(False)

        append_history(
            config,
            "test_contract_reopened",
            {
                "cycle": contract_cycle,
                "challenge": challenge
            }
        )

        # Discard everything this cycle produced: the disproved frozen
        # contract AND the production work written against it.
        rollback_repository(
            workspace,
            clean_untracked=baseline_verified
        )

        if contract:
            restore_snapshot(
                workspace,
                contract[
                    "test_snapshot"
                ]
            )

        contract = None

        # A reopen is a fresh forward run, never a resume.
        resume_requested = False
        resume_phase = None
        resume_phase_status = None

        state["tests_frozen"] = False
        state["tests_reviewed"] = False
        state["tests_generated"] = False
        state["expected_red_confirmed"] = False

        save_state(
            config,
            state
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

        rollback_repository(
            workspace,
            clean_untracked=baseline_verified
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

        rollback_repository(
            workspace,
            clean_untracked=baseline_verified
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
