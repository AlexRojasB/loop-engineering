"""
Expected RED classification.

The pipeline must distinguish three very different reasons a test run is
not green before implementation:

    EXPECTED_RED       the requested future API/behaviour is missing
    INVALID_CONTRACT   the generated tests themselves are wrong
    BROKEN_TEST_SUITE  the generated tests are not even valid code

Classification is driven by the language adapter's contract-diagnostic
hook when the adapter provides one, so the knowledge of what a given
compiler code means stays inside that adapter. Everything else falls back
to the pre-existing generic heuristics, unchanged.
"""

from core.validation import classify_red_state as generic_classify_red_state


BEHAVIOUR_FAILURE_MARKERS = (
    "Assert.",
    "[FAIL]",
    "Failed!",
)


def _summarize(diagnostics, limit=4):
    parts = []

    for diagnostic in diagnostics[:limit]:
        parts.append(
            f"{diagnostic['code']} "
            f"{diagnostic.get('symbol') or ''}".strip()
        )

    if len(diagnostics) > limit:
        parts.append(
            f"(+{len(diagnostics) - limit} more)"
        )

    return ", ".join(parts)


def classify_expected_red(
    output,
    adapter=None,
    spec_text=""
):
    """
    Classify a pre-implementation validation result.

    Returns the existing {"classification", "reason"} shape, plus an
    optional "diagnostics" key carrying the adapter's structured
    breakdown for observability and failure memory.
    """

    report = None

    if adapter is not None:
        hook = getattr(
            adapter,
            "classify_contract_diagnostics",
            None
        )

        if callable(hook):
            report = hook(
                output,
                spec_text
            )

    if report and report.get("supported"):
        if report.get("broken_syntax"):
            return {
                "classification":
                    "BROKEN_TEST_SUITE",

                "reason":
                    "Generated tests are not syntactically "
                    "valid: "
                    + _summarize(
                        report["broken_syntax"]
                    ),

                "diagnostics": report
            }

        if report.get("invalid"):
            return {
                "classification":
                    "INVALID_CONTRACT",

                "reason":
                    "Validation failure proves the generated "
                    "test contract itself is invalid: "
                    + _summarize(
                        report["invalid"]
                    ),

                "diagnostics": report
            }

        if report.get("expected_red"):
            return {
                "classification":
                    "EXPECTED_RED",

                "reason":
                    "Tests reference API requested by the "
                    "current authoritative specification "
                    "that does not exist yet: "
                    + _summarize(
                        report["expected_red"]
                    ),

                "diagnostics": report
            }

    # No structured verdict. Behaviour-level failures (tests compile and
    # run, assertions fail) are the other legitimate RED.
    if any(
        marker in (output or "")
        for marker in BEHAVIOUR_FAILURE_MARKERS
    ):
        return {
            "classification":
                "EXPECTED_RED",

            "reason":
                "Tests execute but requested behavior is "
                "not satisfied yet.",

            "diagnostics": report
        }

    if adapter is not None:
        legacy = adapter.classify_red_state(
            output
        )

        if legacy:
            result = dict(legacy)
            result["diagnostics"] = report
            return result

    result = dict(
        generic_classify_red_state(
            output
        )
    )

    result["diagnostics"] = report

    return result
