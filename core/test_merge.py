def find_class_body_end(
    content,
    class_name=None
):
    """
    Find the closing brace of a C# class.

    If class_name is omitted, the last class declaration
    in the file is selected.

    This implementation is intentionally simple and will
    later move behind the .NET language adapter.
    """

    import re

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
    snippet,
    class_name=None
):
    end_index = find_class_body_end(
        original_content,
        class_name=class_name
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
        for line
        in snippet.strip().splitlines()
    )

    return (
        before
        + "\n\n"
        + indented_snippet
        + "\n"
        + after
    )
