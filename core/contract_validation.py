"""
Deterministic pre-freeze contract validation.

Before an expensive semantic model reviews a candidate test contract, the
harness compiles it and asks the language adapter to classify whatever
diagnostics come back. That answers, cheaply and deterministically,
whether the failure is:

    A) the requested future API not existing yet   -> legitimate RED
    B) proof the generated contract itself is wrong -> reject now

Category B never reaches the semantic reviewer, which is both a
correctness win (the reviewer catches this only unreliably) and the
single largest Test Contract latency saving available.

The validation is fail-open by design: when the adapter cannot classify
(no hook, unknown diagnostics, build tooling unavailable) the contract
proceeds to the existing reviewers exactly as before. It only ever
short-circuits on positive evidence of a defect.
"""

from core.repository import run_argv


UNSUPPORTED = {
    "supported": False,
    "verdict": "UNSUPPORTED",
    "issues": [],
    "output": ""
}


def adapter_supports_validation(adapter):
    if adapter is None:
        return False

    hook = getattr(
        adapter,
        "classify_contract_diagnostics",
        None
    )

    if not callable(hook):
        return False

    probe = hook("", "")

    return bool(
        probe
        and probe.get("supported")
    )


def validate_candidate_contract(
    workspace,
    adapter,
    repository_files,
    spec_text,
    runner=None
):
    """
    Compile the repository with the candidate contract already written to
    disk, then classify the diagnostics.

    Returns:

        {
            "supported": bool,
            "verdict": "VALID" | "INVALID" | "UNKNOWN" | "UNSUPPORTED",
            "issues": [...],
            "report": <adapter report or None>,
            "output": <raw build output>
        }
    """

    if not adapter_supports_validation(
        adapter
    ):
        return dict(UNSUPPORTED)

    runner = runner or run_argv

    try:
        argv = adapter.build_argv(
            repository_files
        )

    except Exception as exc:
        return {
            "supported": False,
            "verdict": "UNSUPPORTED",
            "issues": [],
            "report": None,
            "output":
                f"build command unavailable: {exc}"
        }

    try:
        result = runner(
            workspace,
            argv
        )

    except Exception as exc:
        # Missing toolchain, timeout, permission error: never fail the
        # contract on infrastructure problems.
        return {
            "supported": False,
            "verdict": "UNSUPPORTED",
            "issues": [],
            "report": None,
            "output":
                f"build could not be executed: "
                f"{type(exc).__name__}: {exc}"
        }

    output = result.get(
        "output",
        ""
    )

    report = adapter.classify_contract_diagnostics(
        output,
        spec_text
    )

    if not report or not report.get("supported"):
        return {
            "supported": False,
            "verdict": "UNSUPPORTED",
            "issues": [],
            "report": report,
            "output": output
        }

    return {
        "supported": True,
        "verdict": report["verdict"],
        "issues": report.get("issues", []),
        "report": report,
        "output": output
    }
