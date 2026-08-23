def format_file_block(
    label,
    path,
    content
):
    return f"""
===== {label}: {path} =====
{content}
===== END {label} =====
"""


def implementation_text(
    implementation_files
):
    text = ""

    for path, content in (
        implementation_files.items()
    ):
        text += format_file_block(
            "PRODUCTION",
            path,
            content
        )

    return text


def test_contract_text(
    test_files
):
    text = ""

    for path, content in (
        test_files.items()
    ):
        text += format_file_block(
            "FROZEN TEST CONTRACT",
            path,
            content
        )

    return text


def build_behavior_contract(
    task,
    file_change
):
    reasons = "\n".join(
        f"- {reason}"
        for reason in file_change[
            "reasons"
        ]
    )

    return f"""
TASK:
{task}

APPROVED BEHAVIOR FOR THIS FILE:
{reasons}

GENERAL CONSTRAINTS:
- Preserve existing behavior not changed by the task.
- Do not introduce test-framework code into production.
- Do not add dependencies unless explicitly approved.
- Modify only behavior required by the task.
""".strip()


def build_planner_context(
    task,
    files
):
    listing = "\n".join(
        files
    )

    return {
        "task": task,
        "repository_files": listing
    }


def build_test_generator_context(
    task,
    implementation_files,
    existing_test_content
):
    return {
        "task": task,
        "production":
            implementation_text(
                implementation_files
            ),
        "existing_tests":
            existing_test_content
    }


def build_test_reviewer_context(
    task,
    implementation_files,
    merged_test_content
):
    return {
        "task": task,
        "production":
            implementation_text(
                implementation_files
            ),
        "test_contract":
            merged_test_content
    }


def build_coder_context(
    task,
    file_change,
    current_content
):
    return {
        "task": task,
        "target":
            file_change["path"],
        "behavior_contract":
            build_behavior_contract(
                task,
                file_change
            ),
        "current_content":
            current_content
    }


def build_repair_context(
    task,
    file_change,
    current_content,
    validation_output
):
    return {
        "task": task,
        "target":
            file_change["path"],
        "requirements":
            "\n".join(
                f"- {reason}"
                for reason
                in file_change[
                    "reasons"
                ]
            ),
        "current_content":
            current_content,
        "validation_output":
            validation_output
    }


def build_reviewer_context(
    task,
    plan,
    diff,
    validation_summary=None
):
    return {
        "task": task,
        "plan": plan,
        "diff": diff,
        "validation":
            validation_summary
            or {
                "build": "PASS",
                "tests": "PASS"
            }
    }


def compact_context_value(
    value,
    limit=6000
):
    if not isinstance(
        value,
        str
    ):
        return value

    if len(value) <= limit:
        return value

    half = limit // 2

    return (
        value[:half]
        + "\n\n...[CONTEXT TRUNCATED]...\n\n"
        + value[-half:]
    )


def compact_context(
    context,
    limit_per_value=6000
):
    return {
        key:
            compact_context_value(
                value,
                limit_per_value
            )
        for key, value
        in context.items()
    }


def build_project_planner_context(
    task,
    project_context,
    repository_files
):
    authoritative = []

    for item in project_context.get(
        "authoritative_context",
        []
    ):
        authoritative.append(
            {
                "path": item["path"],
                "category": item["category"],
                "content": item["content"]
            }
        )

    supporting = []

    for item in project_context.get(
        "supporting_context",
        []
    ):
        supporting.append(
            {
                "path": item["path"],
                "category": item["category"],
                "content": item["content"]
            }
        )

    return {
        "task": task,
        "repository_files":
            repository_files,
        "authoritative_context":
            authoritative,
        "supporting_context":
            supporting
    }
