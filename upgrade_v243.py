from pathlib import Path

path = Path("agent_v2.py")
text = path.read_text()

old = '''def implementation_prompt(
    task,
    file_change,
    current_content,
    frozen_tests
):
    reasons = "\\n".join(
        f"- {r}"
        for r
        in file_change["reasons"]
    )

    tests_text = ""

    for path, content in (
        frozen_tests.items()
    ):
        tests_text += f"""
===== FROZEN TESTS: {path} =====
{content}
===== END =====
"""

    return f"""
Implement production code against frozen tests.

TASK:

{task}

TARGET:

{file_change["path"]}

REQUIREMENTS:

{reasons}

CURRENT PRODUCTION:

{current_content}

FROZEN TEST CONTRACT:

{tests_text}

Rules:

- Modify only production code.
- Preserve existing behavior.
- Satisfy the frozen contract.
- Do not add dependencies.
- Return complete target file.
- No Markdown.
- No explanation.
"""
'''

new = '''def build_behavior_contract(task, file_change):
    reasons = "\\n".join(
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
        "using Xunit": r"\\\\busing\\\\s+Xunit\\\\s*;",
        "[Fact]": r"\\\\[Fact(?:Attribute)?\\\\]",
        "[Theory]": r"\\\\[Theory(?:Attribute)?\\\\]",
        "Assert": r"\\\\bAssert\\\\.",
        "test class": r"\\\\b(?:class|record)\\\\s+\\\\w*Tests\\\\b"
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
'''

if old not in text:
    raise SystemExit(
        "Could not find implementation_prompt block. "
        "Make sure agent_v2.py is V2.4.2."
    )

text = text.replace(old, new)

old_write = '''        write_file(
            workspace,
            path,
            extract_code(
                result[
                    "response"
                ]
            )
        )
'''

new_write = '''        generated_content = extract_code(
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
'''

if old_write not in text:
    raise SystemExit(
        "Could not find initial implementation write block."
    )

text = text.replace(
    old_write,
    new_write,
    1
)

text = text.replace(
    'print("AGENT V2.4.2")',
    'print("AGENT V2.4.3")'
)

path.write_text(text)

print("agent_v2.py upgraded to V2.4.3")
