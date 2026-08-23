import re
from pathlib import Path

from core.repository import read_file


SOURCE_EXTENSIONS = (
    ".cs",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".rs",
    ".go",
    ".cpp",
    ".c",
    ".h"
)

CONFIG_EXTENSIONS = (
    ".csproj",
    ".fsproj",
    ".vbproj",
    ".sln",
    ".slnx",
    ".props",
    ".targets"
)

CONFIG_FILENAMES = {
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "dockerfile"
}


def is_source_file(path):
    return path.lower().endswith(
        SOURCE_EXTENSIONS
    )


def is_test_file(path):
    lower = path.lower()

    return (
        "test" in lower
        or "spec" in lower
    )


def is_config_file(path):
    p = Path(path)

    if p.name.lower() in CONFIG_FILENAMES:
        return True

    return path.lower().endswith(
        CONFIG_EXTENSIONS
    )


def search_symbol(
    workspace,
    files,
    symbol
):
    matches = []

    for path in files:
        if not is_source_file(
            path
        ):
            continue

        try:
            content = read_file(
                workspace,
                path
            )
        except Exception:
            continue

        if (
            symbol.lower()
            in content.lower()
        ):
            matches.append(path)

    return matches


def repair_planner_path(
    workspace,
    files,
    requested
):
    if requested in files:
        return requested

    symbol = Path(
        requested
    ).stem

    matches = search_symbol(
        workspace,
        files,
        symbol
    )

    if len(matches) == 1:
        resolved = matches[0]

        print(
            "Planner path repaired: "
            f"{requested} -> {resolved}"
        )

        return resolved

    non_tests = [
        match
        for match in matches
        if not is_test_file(match)
    ]

    if len(non_tests) == 1:
        resolved = non_tests[0]

        print(
            "Planner path repaired: "
            f"{requested} -> {resolved}"
        )

        return resolved

    return None


def normalize_plan(
    workspace,
    files,
    planner_plan
):
    normalized = {
        "read_files": [],
        "changes": [],

        "configuration_changes_required":
            bool(
                planner_plan.get(
                    "configuration_changes_required",
                    False
                )
            ),

        "dependencies_required":
            planner_plan.get(
                "dependencies_required",
                []
            ),

        "coder_instruction":
            planner_plan.get(
                "coder_instruction",
                ""
            )
    }

    for requested in planner_plan.get(
        "read_files",
        []
    ):
        resolved = repair_planner_path(
            workspace,
            files,
            requested
        )

        if (
            resolved
            and resolved
            not in normalized[
                "read_files"
            ]
        ):
            normalized[
                "read_files"
            ].append(resolved)

    for change in planner_plan.get(
        "changes",
        []
    ):
        requested = change.get(
            "path",
            ""
        )

        resolved = repair_planner_path(
            workspace,
            files,
            requested
        )

        if not resolved:
            print(
                "Planner change ignored: "
                f"unresolved path {requested}"
            )
            continue

        reason = change.get(
            "reason",
            ""
        ).strip()

        if not reason:
            print(
                "Planner change ignored: "
                f"missing reason for {resolved}"
            )
            continue

        if is_config_file(
            resolved
        ):
            change_type = "configuration"

            if not normalized[
                "configuration_changes_required"
            ]:
                print(
                    "Policy rejected "
                    "configuration change: "
                    f"{resolved}"
                )
                continue

        elif is_test_file(
            resolved
        ):
            change_type = "test"

        else:
            change_type = change.get(
                "type",
                "implementation"
            )

        normalized[
            "changes"
        ].append(
            {
                "path": resolved,
                "type": change_type,
                "reason": reason
            }
        )

    return normalized


def group_changes_by_file(
    changes
):
    grouped = {}

    for change in changes:
        path = change["path"]

        if path not in grouped:
            grouped[path] = {
                "path": path,
                "type":
                    change["type"],
                "reasons": []
            }

        reason = change["reason"]

        if (
            reason
            not in grouped[
                path
            ]["reasons"]
        ):
            grouped[
                path
            ]["reasons"].append(
                reason
            )

        if change["type"] == "test":
            grouped[
                path
            ]["type"] = "test"

        elif (
            change["type"]
            == "configuration"
        ):
            grouped[
                path
            ]["type"] = (
                "configuration"
            )

    return list(
        grouped.values()
    )
