import json
import os
import re
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from core.models import call_model
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


OLLAMA_URL = "http://localhost:11434/api/generate"

IGNORED_DIRS = {
    ".git",
    "bin",
    "obj",
    ".venv",
    "node_modules"
}

SOURCE_EXTENSIONS = (
    ".cs", ".py", ".ts", ".tsx", ".js", ".jsx",
    ".java", ".rs", ".go", ".cpp", ".c", ".h"
)

CONFIG_EXTENSIONS = (
    ".csproj", ".fsproj", ".vbproj",
    ".sln", ".slnx", ".props", ".targets"
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


# ============================================================
# GENERAL
# ============================================================

def load_json(path):
    with open(path) as f:
        return json.load(f)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def compact(text, limit=6000):
    if not text:
        return ""

    if len(text) <= limit:
        return text

    half = limit // 2

    return (
        text[:half]
        + "\n\n...[TRUNCATED BY HARNESS]...\n\n"
        + text[-half:]
    )




def extract_code(text):
    text = text.strip()

    fenced = re.search(
        r"```(?:csharp|cs|python|typescript|javascript|"
        r"java|rust|go|xml|json)?\s*\n?(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL
    )

    if fenced:
        text = fenced.group(1)

    text = re.sub(
        r"^```[A-Za-z0-9_+\-]*\s*",
        "",
        text
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    return text.strip() + "\n"


# ============================================================
# STATE
# ============================================================

def default_state(task):
    return {
        "task": task,
        "phase": "starting",
        "planner_complete": False,
        "tests_generated": False,
        "tests_structurally_valid": False,
        "tests_reviewed": False,
        "tests_frozen": False,
        "expected_red_confirmed": False,
        "implementation_generated": False,
        "build": "unknown",
        "tests": {
            "status": "unknown",
            "passed": None,
            "failed": None
        },
        "review": "pending",
        "updated_at": now_iso()
    }


def save_state(config, state):
    state["updated_at"] = now_iso()

    with open(
        config["state_file"],
        "w"
    ) as f:
        json.dump(
            state,
            f,
            indent=2
        )


def append_history(config, event, data=None):
    entry = {
        "timestamp": now_iso(),
        "event": event,
        "data": data or {}
    }

    with open(
        config["history_file"],
        "a"
    ) as f:
        f.write(
            json.dumps(entry) + "\n"
        )




# ============================================================
# FILE TYPES
# ============================================================

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

    if (
        p.name.lower()
        in CONFIG_FILENAMES
    ):
        return True

    return path.lower().endswith(
        CONFIG_EXTENSIONS
    )


# ============================================================
# PATH RESOLUTION
# ============================================================

def search_symbol(
    workspace,
    files,
    symbol
):
    matches = []

    for path in files:
        if not is_source_file(path):
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

    symbol = Path(requested).stem

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
        m
        for m in matches
        if not is_test_file(m)
    ]

    if len(non_tests) == 1:
        resolved = non_tests[0]

        print(
            "Planner path repaired: "
            f"{requested} -> {resolved}"
        )

        return resolved

    return None


# ============================================================
# PLAN NORMALIZATION
# ============================================================

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
            continue

        reason = change.get(
            "reason",
            ""
        ).strip()

        if not reason:
            continue

        if is_config_file(resolved):
            change_type = "configuration"

            if not normalized[
                "configuration_changes_required"
            ]:
                continue

        elif is_test_file(resolved):
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


def group_changes_by_file(changes):
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

        if (
            change["reason"]
            not in grouped[
                path
            ]["reasons"]
        ):
            grouped[
                path
            ]["reasons"].append(
                change["reason"]
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


# ============================================================
# TEST SNIPPET / MERGE
# ============================================================

def extract_test_method_names(text):
    names = []

    pattern = (
        r"(?:public|private|internal|protected)"
        r"\s+(?:async\s+)?"
        r"(?:void|Task)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
    )

    for name in re.findall(
        pattern,
        text
    ):
        names.append(name)

    return names


def contains_production_redefinition(
    snippet,
    implementation_files
):
    production = set()

    patterns = [
        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\binterface\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\brecord\s+([A-Za-z_][A-Za-z0-9_]*)"
    ]

    for content in (
        implementation_files.values()
    ):
        for pattern in patterns:
            production.update(
                re.findall(
                    pattern,
                    content
                )
            )

    declared = set()

    for pattern in patterns:
        declared.update(
            re.findall(
                pattern,
                snippet
            )
        )

    return sorted(
        production & declared
    )


def validate_test_snippet(
    snippet,
    original_test_content,
    implementation_files
):
    issues = []

    if not snippet.strip():
        issues.append(
            "Generated test snippet is empty."
        )

    if (
        "[Fact]" not in snippet
        and "[Theory]" not in snippet
        and "[Test]" not in snippet
    ):
        issues.append(
            "Generated snippet contains no recognizable test."
        )

    redefined = (
        contains_production_redefinition(
            snippet,
            implementation_files
        )
    )

    for symbol in redefined:
        issues.append(
            "Test snippet illegally redefines "
            f"production symbol: {symbol}"
        )

    existing_names = set(
        extract_test_method_names(
            original_test_content
        )
    )

    generated_names = (
        extract_test_method_names(
            snippet
        )
    )

    for name in generated_names:
        if name in existing_names:
            issues.append(
                f"Generated duplicate test method: {name}"
            )

    if not generated_names:
        issues.append(
            "Could not detect any generated test methods."
        )

    return issues


def find_class_body_end(
    content,
    class_name=None
):
    """
    Finds the closing brace of the selected C# class.

    If class_name is not supplied, use the last class in
    the source file. Good enough for this benchmark and
    deterministic.
    """

    class_matches = list(
        re.finditer(
            r"\bclass\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            content
        )
    )

    if not class_matches:
        return None

    selected = None

    if class_name:
        for match in class_matches:
            if match.group(1) == class_name:
                selected = match
                break

    if selected is None:
        selected = class_matches[-1]

    brace_start = content.find(
        "{",
        selected.end()
    )

    if brace_start < 0:
        return None

    depth = 0

    for index in range(
        brace_start,
        len(content)
    ):
        char = content[index]

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                return index

    return None


def merge_test_snippet(
    original_content,
    snippet
):
    end_index = find_class_body_end(
        original_content
    )

    if end_index is None:
        raise ValueError(
            "Could not find test class closing brace."
        )

    before = original_content[
        :end_index
    ].rstrip()

    after = original_content[
        end_index:
    ]

    indented_snippet = "\n".join(
        (
            "    " + line
            if line.strip()
            else ""
        )
        for line in snippet.strip().splitlines()
    )

    return (
        before
        + "\n\n"
        + indented_snippet
        + "\n"
        + after
    )


# ============================================================
# RED CLASSIFICATION
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
                "Syntax/structural errors detected."
        }

    missing_feature_codes = {
        "CS0103",
        "CS0117",
        "CS1061",
        "CS0246"
    }

    if (
        compiler_codes
        & missing_feature_codes
    ):
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
            "Could not classify."
    }


# ============================================================
# PROMPTS
# ============================================================

def planner_prompt(task, files):
    listing = "\n".join(files)

    return f"""
You are the planning agent.

TASK:

{task}

REAL FILES:

{listing}

Rules:

- Do not invent paths.
- Classes may share files.
- Every modification requires a reason.
- Configuration changes must be justified.
- Dependencies must be declared.
- Plan only.

Return JSON only:

{{
  "read_files": [],

  "changes": [
    {{
      "path": "existing/path",
      "type":
        "implementation | test | configuration",
      "reason": "why this file changes"
    }}
  ],

  "configuration_changes_required": false,

  "dependencies_required": [],

  "coder_instruction": "..."
}}
"""


def implementation_text(
    implementation_files
):
    result = ""

    for path, content in (
        implementation_files.items()
    ):
        result += f"""
===== PRODUCTION: {path} =====
{content}
===== END =====
"""

    return result


def test_snippet_prompt(
    task,
    implementation_files,
    current_test_content
):
    return f"""
You are writing ONLY NEW TEST METHODS for a TDD workflow.

TASK:

{task}

CURRENT PRODUCTION:

{implementation_text(implementation_files)}

EXISTING TEST FILE:

{current_test_content}

Generate ONLY the new test methods required for the task.

IMPORTANT:

- Do NOT output the complete test file.
- Do NOT output namespace declarations.
- Do NOT output using statements.
- Do NOT output a test class declaration.
- Do NOT output production classes.
- Do NOT output production enums.
- Do NOT output production interfaces.
- Do NOT implement the requested feature inside tests.
- Preserve the existing xUnit style.
- The production feature does NOT exist yet.
- It is expected that the new tests initially fail.

Return only one or more C# [Fact] test methods.

No Markdown.
No explanation.
"""


def test_snippet_revision_prompt(
    task,
    implementation_files,
    original_test_content,
    snippet,
    issues
):
    return f"""
Correct these NEW TEST METHODS.

TASK:

{task}

CURRENT PRODUCTION:

{implementation_text(implementation_files)}

EXISTING TEST FILE:

{original_test_content}

CURRENT NEW TEST METHODS:

{snippet}

ISSUES:

{json.dumps(issues, indent=2)}

Return ONLY corrected new [Fact] test methods.

Do not return the full test file.
Do not redefine production types.
Do not declare a class.
Do not add using statements.
Do not implement production behavior.
No Markdown.
No explanation.
"""


def test_review_prompt(
    task,
    implementation_files,
    merged_test_content
):
    return f"""
You are reviewing a FUTURE TEST CONTRACT in a TDD workflow.

TASK:

{task}

CURRENT PRODUCTION:

{implementation_text(implementation_files)}

FULL TEST FILE AFTER DETERMINISTIC MERGE:

{merged_test_content}

The new production feature DOES NOT EXIST YET.

That is intentional.

DO NOT reject because:
- RefundOrder does not exist yet
- Refunded does not exist yet
- tests currently fail
- tests currently fail to compile because the new API is missing

Review ONLY test correctness.

Verify:

1. Each new test maps to an actual requirement.
2. Arrange represents the described state.
3. Act invokes the intended feature.
4. Assertions match the business rule.
5. Existing tests remain intact.
6. No production type was redefined.
7. No unrelated behavior was invented.
8. Successful refund verifies Paid -> Refunded.
9. Pending cannot be refunded.
10. Cancelled cannot be refunded.
11. Already Refunded cannot be refunded.
12. Unknown Guid returns false.

Return JSON only:

{{
  "decision": "APPROVE or REJECT",
  "issues": []
}}
"""


def build_behavior_contract(task, file_change):
    reasons = "\n".join(
        f"- {r}"
        for r in file_change["reasons"]
    )

    return f"""
TASK:
{task}

APPROVED BEHAVIOR FOR THIS PRODUCTION FILE:
{reasons}

GENERAL REQUIREMENTS:
- Preserve all existing behavior not changed by the task.
- Do not introduce test framework code into production.
- Do not add dependencies unless explicitly approved.
"""


def production_guard(content):
    forbidden_patterns = {
        "using Xunit": r"\\busing\\s+Xunit\\s*;",
        "[Fact]": r"\\[Fact(?:Attribute)?\\]",
        "[Theory]": r"\\[Theory(?:Attribute)?\\]",
        "Assert": r"\\bAssert\\.",
        "test class": r"\\b(?:class|record)\\s+\\w*Tests\\b"
    }

    issues = []

    for label, pattern in forbidden_patterns.items():
        if re.search(pattern, content):
            issues.append(
                f"Production output contains test artifact: {label}"
            )

    return issues


def implementation_prompt(
    task,
    file_change,
    current_content,
    frozen_tests
):
    behavior_contract = build_behavior_contract(
        task,
        file_change
    )

    return f"""
You are modifying a PRODUCTION source file.

BEHAVIOR CONTRACT:

{behavior_contract}

TARGET:

{file_change["path"]}

CURRENT PRODUCTION FILE:

{current_content}

CRITICAL BOUNDARY RULES:

- This is production code, NOT test code.
- NEVER output using Xunit.
- NEVER output [Fact], [Theory], Assert.*, or test classes.
- Do not copy any testing-framework syntax.
- Do not implement tests in this file.
- Preserve the current language and architecture.
- Implement only the requested production behavior.
- Return the COMPLETE production file.
- No Markdown.
- No explanation.
"""


def repair_prompt(
    task,
    file_change,
    current_content,
    failure
):
    reasons = "\n".join(
        f"- {r}"
        for r
        in file_change["reasons"]
    )

    return f"""
Repair production code.

TASK:

{task}

TARGET:

{file_change["path"]}

REQUIREMENTS:

{reasons}

CURRENT CONTENT:

{current_content}

VALIDATION FAILURE:

{failure}

Tests are frozen.

Fix production code only.

Preserve existing correct behavior.

Do not add dependencies.

Return complete corrected target file only.

No Markdown.
No explanation.
"""


def reviewer_prompt(
    task,
    plan,
    diff
):
    return f"""
You are the final reviewer.

TASK:

{task}

PLAN:

{json.dumps(plan, indent=2)}

GIT DIFF:

{diff}

VALIDATION:

BUILD: PASS
TESTS: PASS
TEST CONTRACT REVIEWED: YES
TESTS FROZEN: YES

Return JSON only:

{{
  "decision": "APPROVE or REJECT",
  "issues": []
}}
"""


# ============================================================
# VALIDATION HELPERS
# ============================================================

def failure_score(output):
    compiler = len(
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

    return (
        compiler * 100
        + failed_tests * 10
    )


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
        "failed":
            int(match.group(1)),
        "passed":
            int(match.group(2))
    }


# ============================================================
# MAIN
# ============================================================

def main():
    config = load_json(
        "agent-v2-config.json"
    )

    workspace = config[
        "workspace"
    ]

    if not ensure_clean_baseline(
        workspace
    ):
        return

    with open("TASK.md") as f:
        task = f.read()

    state = default_state(task)

    save_state(
        config,
        state
    )

    append_history(
        config,
        "run_started"
    )

    files = discover_files(
        workspace
    )

    print()
    print("=" * 60)
    print("AGENT V2.4.3")
    print("=" * 60)

    # ========================================================
    # PLANNING
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 1 - PLANNING")
    print("=" * 60)

    state["phase"] = "planning"
    save_state(
        config,
        state
    )

    planner_result = call_model(
        config,
        config[
            "planner_model"
        ],
        planner_prompt(
            task,
            files
        ),
        json_mode=True
    )

    if not planner_result["ok"]:
        print(
            planner_result["error"]
        )
        return

    try:
        planner_plan = json.loads(
            planner_result[
                "response"
            ]
        )
    except json.JSONDecodeError:
        print(
            "Planner returned invalid JSON."
        )
        return

    plan = normalize_plan(
        workspace,
        files,
        planner_plan
    )

    print(
        json.dumps(
            plan,
            indent=2
        )
    )

    state[
        "planner_complete"
    ] = True

    save_state(
        config,
        state
    )

    append_history(
        config,
        "plan_created",
        plan
    )

    if (
        plan[
            "dependencies_required"
        ]
    ):
        print(
            "Dependency tools not implemented yet."
        )
        return

    grouped = group_changes_by_file(
        plan["changes"]
    )

    implementation_changes = [
        c
        for c in grouped
        if c["type"]
        in (
            "implementation",
            "configuration"
        )
    ]

    test_changes = [
        c
        for c in grouped
        if c["type"] == "test"
    ]

    if not implementation_changes:
        print(
            "No implementation changes planned."
        )
        return

    if not test_changes:
        print(
            "No tests planned."
        )
        return

    implementation_context = {}

    for change in (
        implementation_changes
    ):
        implementation_context[
            change["path"]
        ] = read_file(
            workspace,
            change["path"]
        )

    # ========================================================
    # TEST SNIPPETS
    # ========================================================

    print()
    print("=" * 60)
    print(
        "PHASE 2 - TEST SNIPPET GENERATION"
    )
    print("=" * 60)

    state["phase"] = (
        "test_generation"
    )

    save_state(
        config,
        state
    )

    test_paths = [
        c["path"]
        for c in test_changes
    ]

    test_snapshot = (
        snapshot_files(
            workspace,
            test_paths
        )
    )

    frozen_tests = {}

    for test_change in test_changes:
        path = test_change[
            "path"
        ]

        original = test_snapshot[
            path
        ]

        generated = call_model(
            config,
            config[
                "coder_model"
            ],
            test_snippet_prompt(
                task,
                implementation_context,
                original
            )
        )

        if not generated["ok"]:
            restore_snapshot(
                workspace,
                test_snapshot
            )
            return

        snippet = extract_code(
            generated[
                "response"
            ]
        )

        approved = False

        for attempt in range(
            1,
            config[
                "max_test_generation_attempts"
            ] + 1
        ):
            print()
            print(
                f"Test snippet attempt "
                f"{attempt}: {path}"
            )

            issues = (
                validate_test_snippet(
                    snippet,
                    original,
                    implementation_context
                )
            )

            if issues:
                print(
                    "SNIPPET GUARD: REJECT"
                )

                for issue in issues:
                    print(
                        f"- {issue}"
                    )

                revision = call_model(
                    config,
                    config[
                        "coder_model"
                    ],
                    test_snippet_revision_prompt(
                        task,
                        implementation_context,
                        original,
                        snippet,
                        issues
                    )
                )

                if not revision["ok"]:
                    continue

                snippet = extract_code(
                    revision[
                        "response"
                    ]
                )

                continue

            try:
                merged = merge_test_snippet(
                    original,
                    snippet
                )
            except Exception as exc:
                print(
                    "MERGE ERROR:"
                )
                print(exc)
                continue

            review = call_model(
                config,
                config[
                    "test_reviewer_model"
                ],
                test_review_prompt(
                    task,
                    implementation_context,
                    merged
                ),
                json_mode=True
            )

            if not review["ok"]:
                continue

            try:
                review_json = json.loads(
                    review[
                        "response"
                    ]
                )
            except json.JSONDecodeError:
                continue

            print(
                json.dumps(
                    review_json,
                    indent=2
                )
            )

            if (
                review_json.get(
                    "decision",
                    ""
                ).upper()
                == "APPROVE"
            ):
                approved = True
                break

            review_issues = (
                review_json.get(
                    "issues",
                    []
                )
            )

            revision = call_model(
                config,
                config[
                    "coder_model"
                ],
                test_snippet_revision_prompt(
                    task,
                    implementation_context,
                    original,
                    snippet,
                    review_issues
                )
            )

            if not revision["ok"]:
                continue

            snippet = extract_code(
                revision[
                    "response"
                ]
            )

        if not approved:
            print()
            print(
                "Test contract was not approved."
            )

            restore_snapshot(
                workspace,
                test_snapshot
            )

            append_history(
                config,
                "test_contract_rejected",
                {
                    "file": path
                }
            )

            return

        final_merged = merge_test_snippet(
            original,
            snippet
        )

        write_file(
            workspace,
            path,
            final_merged
        )

        frozen_tests[
            path
        ] = final_merged

        append_history(
            config,
            "test_contract_approved",
            {
                "file": path,
                "new_tests":
                    extract_test_method_names(
                        snippet
                    )
            }
        )

    state[
        "tests_generated"
    ] = True

    state[
        "tests_structurally_valid"
    ] = True

    state[
        "tests_reviewed"
    ] = True

    state[
        "tests_frozen"
    ] = True

    state[
        "phase"
    ] = "tests_frozen"

    save_state(
        config,
        state
    )

    # ========================================================
    # EXPECTED RED
    # ========================================================

    print()
    print("=" * 60)
    print(
        "PHASE 3 - EXPECTED RED"
    )
    print("=" * 60)

    red = run_command(
        workspace,
        config[
            "validation"
        ]["test"]
    )

    print(
        compact(
            red[
                "output"
            ]
        )
    )

    classification = (
        classify_red_state(
            red[
                "output"
            ]
        )
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

    if red[
        "exit_code"
    ] == 0:
        print(
            "Tests already pass before "
            "implementation. Contract may be weak."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )
        return

    if (
        classification[
            "classification"
        ]
        == "BROKEN_TEST_SUITE"
    ):
        print(
            "Broken generated test suite."
        )

        restore_snapshot(
            workspace,
            test_snapshot
        )
        return

    state[
        "expected_red_confirmed"
    ] = True

    save_state(
        config,
        state
    )

    # ========================================================
    # IMPLEMENTATION
    # ========================================================

    print()
    print("=" * 60)
    print(
        "PHASE 4 - IMPLEMENTATION"
    )
    print("=" * 60)

    state["phase"] = (
        "implementation"
    )

    save_state(
        config,
        state
    )

    implementation_snapshot = (
        snapshot_files(
            workspace,
            [
                c["path"]
                for c in
                implementation_changes
            ]
        )
    )

    for change in (
        implementation_changes
    ):
        path = change[
            "path"
        ]

        current = read_file(
            workspace,
            path
        )

        result = call_model(
            config,
            config[
                "coder_model"
            ],
            implementation_prompt(
                task,
                change,
                current,
                frozen_tests
            )
        )

        if not result["ok"]:
            restore_snapshot(
                workspace,
                implementation_snapshot
            )
            return

        generated_content = extract_code(
            result[
                "response"
            ]
        )

        guard_issues = production_guard(
            generated_content
        )

        if guard_issues:
            print()
            print(
                "PRODUCTION GUARD: REJECT"
            )

            for issue in guard_issues:
                print(
                    f"- {issue}"
                )

            append_history(
                config,
                "production_guard_rejected",
                {
                    "file": path,
                    "issues": guard_issues
                }
            )

            restore_snapshot(
                workspace,
                implementation_snapshot
            )

            print(
                "Production generation rejected "
                "before build."
            )

            return

        write_file(
            workspace,
            path,
            generated_content
        )

    state[
        "implementation_generated"
    ] = True

    save_state(
        config,
        state
    )

    # ========================================================
    # BUILD
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 5 - BUILD")
    print("=" * 60)

    build = run_command(
        workspace,
        config[
            "validation"
        ]["build"]
    )

    print(
        compact(
            build[
                "output"
            ]
        )
    )

    for attempt in range(
        1,
        config[
            "max_build_repairs"
        ] + 1
    ):
        if (
            build[
                "exit_code"
            ] == 0
        ):
            break

        print()
        print(
            f"BUILD REPAIR {attempt}"
        )

        snapshot = (
            snapshot_files(
                workspace,
                [
                    c["path"]
                    for c in
                    implementation_changes
                ]
            )
        )

        old_score = failure_score(
            build[
                "output"
            ]
        )

        for change in (
            implementation_changes
        ):
            path = change[
                "path"
            ]

            repair = call_model(
                config,
                config[
                    "coder_model"
                ],
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    compact(
                        build[
                            "output"
                        ]
                    )
                )
            )

            if repair["ok"]:
                write_file(
                    workspace,
                    path,
                    extract_code(
                        repair[
                            "response"
                        ]
                    )
                )

        candidate = run_command(
            workspace,
            config[
                "validation"
            ]["build"]
        )

        print(
            compact(
                candidate[
                    "output"
                ]
            )
        )

        if (
            candidate[
                "exit_code"
            ] == 0
        ):
            build = candidate
            break

        new_score = failure_score(
            candidate[
                "output"
            ]
        )

        if new_score < old_score:
            print(
                "Build progress detected."
            )

            build = candidate

        else:
            print(
                "No build progress. "
                "Rolling back."
            )

            restore_snapshot(
                workspace,
                snapshot
            )

    if (
        build[
            "exit_code"
        ] != 0
    ):
        print(
            "Build did not converge."
        )

        git_restore_all(
            workspace
        )
        return

    state["build"] = "pass"

    save_state(
        config,
        state
    )

    # ========================================================
    # TESTS
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 6 - TESTS")
    print("=" * 60)

    tests = run_command(
        workspace,
        config[
            "validation"
        ]["test"]
    )

    print(
        compact(
            tests[
                "output"
            ]
        )
    )

    best_score = failure_score(
        tests[
            "output"
        ]
    )

    for attempt in range(
        1,
        config[
            "max_test_repairs"
        ] + 1
    ):
        if (
            tests[
                "exit_code"
            ] == 0
        ):
            break

        print()
        print(
            f"TEST REPAIR {attempt}"
        )

        snapshot = (
            snapshot_files(
                workspace,
                [
                    c["path"]
                    for c in
                    implementation_changes
                ]
            )
        )

        for change in (
            implementation_changes
        ):
            path = change[
                "path"
            ]

            repair = call_model(
                config,
                config[
                    "coder_model"
                ],
                repair_prompt(
                    task,
                    change,
                    read_file(
                        workspace,
                        path
                    ),
                    compact(
                        tests[
                            "output"
                        ]
                    )
                )
            )

            if repair["ok"]:
                write_file(
                    workspace,
                    path,
                    extract_code(
                        repair[
                            "response"
                        ]
                    )
                )

        candidate = run_command(
            workspace,
            config[
                "validation"
            ]["test"]
        )

        print(
            compact(
                candidate[
                    "output"
                ]
            )
        )

        if (
            candidate[
                "exit_code"
            ] == 0
        ):
            tests = candidate
            break

        score = failure_score(
            candidate[
                "output"
            ]
        )

        if score < best_score:
            print(
                "Test progress detected."
            )

            tests = candidate
            best_score = score

        else:
            print(
                "No test progress. "
                "Rolling back."
            )

            restore_snapshot(
                workspace,
                snapshot
            )

    if (
        tests[
            "exit_code"
        ] != 0
    ):
        print(
            "Tests did not converge."
        )

        git_restore_all(
            workspace
        )
        return

    counts = parse_test_counts(
        tests[
            "output"
        ]
    )

    state[
        "tests"
    ] = {
        "status": "pass",
        "passed":
            counts[
                "passed"
            ],
        "failed": 0
    }

    save_state(
        config,
        state
    )

    # ========================================================
    # REVIEW
    # ========================================================

    print()
    print("=" * 60)
    print("PHASE 7 - FINAL REVIEW")
    print("=" * 60)

    diff = git_diff(
        workspace
    )

    review = call_model(
        config,
        config[
            "reviewer_model"
        ],
        reviewer_prompt(
            task,
            plan,
            compact(
                diff,
                12000
            )
        ),
        json_mode=True
    )

    if not review["ok"]:
        print(
            review["error"]
        )
        return

    try:
        review_json = json.loads(
            review[
                "response"
            ]
        )
    except json.JSONDecodeError:
        print(
            "Reviewer returned invalid JSON."
        )
        return

    print(
        json.dumps(
            review_json,
            indent=2
        )
    )

    if (
        review_json.get(
            "decision",
            ""
        ).upper()
        == "APPROVE"
    ):
        state[
            "review"
        ] = "approve"

        state[
            "phase"
        ] = "completed"

        save_state(
            config,
            state
        )

        append_history(
            config,
            "pipeline_completed",
            {
                "review": "approve"
            }
        )

        print()
        print("=" * 60)
        print(
            "FULL AGENT V2.4.2 "
            "PIPELINE PASSED"
        )
        print("=" * 60)

        print()
        print(
            "Changes remain uncommitted "
            "for human inspection."
        )

    else:
        state[
            "review"
        ] = "reject"

        save_state(
            config,
            state
        )

        print(
            "Reviewer rejected implementation."
        )


if __name__ == "__main__":
    main()
