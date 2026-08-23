import re
from pathlib import Path


# ============================================================
# TEST RESULT PARSING
# ============================================================

def parse_test_counts(output):
    match = re.search(
        r"Failed:\s*(\d+),\s*"
        r"Passed:\s*(\d+)",
        output
    )

    if not match:
        return {
            "failed": None,
            "passed": None
        }

    return {
        "failed": int(match.group(1)),
        "passed": int(match.group(2))
    }


# ============================================================
# FAILURE SCORING
# ============================================================

def failure_score(output):
    compiler_errors = len(
        re.findall(
            r"\berror\s+"
            r"(?:CS|MSB|NU|NETSDK)\d+",
            output,
            flags=re.IGNORECASE
        )
    )

    failed_tests = len(
        set(
            re.findall(
                r"Failed\s+"
                r"([A-Za-z0-9_.]+Tests\."
                r"[A-Za-z0-9_]+)",
                output
            )
        )
    )

    generic_errors = len(
        [
            line
            for line in output.splitlines()
            if "error" in line.lower()
        ]
    )

    return (
        compiler_errors * 100
        + failed_tests * 10
        + generic_errors
    )


# ============================================================
# RED STATE CLASSIFICATION
# ============================================================

def classify_red_state(output):
    broken_codes = {
        "CS1001",
        "CS1002",
        "CS1003",
        "CS1022",
        "CS1513",
        "CS1529"
    }

    compiler_codes = set(
        re.findall(
            r"\bCS\d+\b",
            output
        )
    )

    if compiler_codes & broken_codes:
        return {
            "classification":
                "BROKEN_TEST_SUITE",
            "reason":
                "Syntax or structural errors detected."
        }

    missing_feature_codes = {
        "CS0103",
        "CS0117",
        "CS1061",
        "CS0246"
    }

    if compiler_codes & missing_feature_codes:
        return {
            "classification":
                "EXPECTED_RED",
            "reason":
                "Tests reference requested feature "
                "that does not exist yet."
        }

    if (
        "Assert." in output
        or "[FAIL]" in output
        or "Failed!" in output
    ):
        return {
            "classification":
                "EXPECTED_RED",
            "reason":
                "Tests execute but feature behavior "
                "is not satisfied yet."
        }

    return {
        "classification":
            "UNKNOWN",
        "reason":
            "Could not confidently classify state."
    }


# ============================================================
# FAILURE OWNERSHIP
# ============================================================

def extract_failure_paths(output):
    """
    Extract source file paths mentioned by compiler/test output.
    """

    paths = set()

    patterns = [
        r"(/[^\s:(]+?\.(?:cs|py|ts|tsx|js|jsx|java|rs|go|cpp|c|h))"
        r"\(\d+,\d+\)",

        r"(/[^\s:(]+?\.(?:cs|py|ts|tsx|js|jsx|java|rs|go|cpp|c|h))"
        r":\d+"
    ]

    for pattern in patterns:
        for match in re.findall(
            pattern,
            output
        ):
            paths.add(match)

    return sorted(paths)


def classify_failure_owner(
    output,
    workspace,
    grouped_changes
):
    """
    Determine which logical area owns the failure.

    Returns:
        test
        implementation
        configuration
        mixed
        unknown
    """

    mentioned = extract_failure_paths(
        output
    )

    if not mentioned:
        return {
            "owner": "unknown",
            "paths": []
        }

    root = Path(
        workspace
    ).resolve()

    owners = set()
    matched_paths = []

    for absolute in mentioned:
        try:
            relative = str(
                Path(absolute)
                .resolve()
                .relative_to(root)
            )
        except Exception:
            continue

        matched_paths.append(relative)

        matched_change = None

        for change in grouped_changes:
            if change["path"] == relative:
                matched_change = change
                break

        if matched_change:
            owners.add(
                matched_change["type"]
            )
            continue

        lower = relative.lower()

        if (
            "test" in lower
            or "spec" in lower
        ):
            owners.add("test")

        elif lower.endswith(
            (
                ".csproj",
                ".fsproj",
                ".vbproj",
                ".sln",
                ".slnx",
                ".props",
                ".targets",
                "package.json",
                "pyproject.toml"
            )
        ):
            owners.add(
                "configuration"
            )

        else:
            owners.add(
                "implementation"
            )

    if not owners:
        owner = "unknown"

    elif len(owners) == 1:
        owner = next(
            iter(owners)
        )

    else:
        owner = "mixed"

    return {
        "owner": owner,
        "paths": matched_paths
    }


# ============================================================
# ROUTING DECISION
# ============================================================

def choose_repair_targets(
    output,
    workspace,
    grouped_changes,
    tests_frozen
):
    ownership = (
        classify_failure_owner(
            output,
            workspace,
            grouped_changes
        )
    )

    owner = ownership[
        "owner"
    ]

    if (
        owner == "test"
        and tests_frozen
    ):
        return {
            "action":
                "reject_frozen_test_contract",
            "targets": [],
            "ownership":
                ownership
        }

    if owner == "implementation":
        targets = [
            change
            for change
            in grouped_changes
            if change["type"]
            == "implementation"
        ]

        return {
            "action":
                "repair",
            "targets":
                targets,
            "ownership":
                ownership
        }

    if owner == "configuration":
        targets = [
            change
            for change
            in grouped_changes
            if change["type"]
            == "configuration"
        ]

        return {
            "action":
                "repair",
            "targets":
                targets,
            "ownership":
                ownership
        }

    if owner == "mixed":
        targets = [
            change
            for change
            in grouped_changes
            if change["type"]
            != "test"
        ]

        return {
            "action":
                "repair",
            "targets":
                targets,
            "ownership":
                ownership
        }

    return {
        "action":
            "repair",
        "targets": [
            change
            for change
            in grouped_changes
            if change["type"]
            != "test"
        ],
        "ownership":
            ownership
    }
