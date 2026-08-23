import re


# ============================================================
# GENERIC SYMBOL EXTRACTION
# ============================================================

def extract_declared_symbols(content):
    patterns = [
        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\binterface\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\brecord\s+([A-Za-z_][A-Za-z0-9_]*)"
    ]

    symbols = set()

    for pattern in patterns:
        symbols.update(
            re.findall(
                pattern,
                content
            )
        )

    return symbols


def extract_test_method_names(text):
    pattern = (
        r"(?:public|private|internal|protected)"
        r"\s+(?:async\s+)?"
        r"(?:void|Task)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
    )

    return re.findall(
        pattern,
        text
    )


# ============================================================
# PRODUCTION SURFACE
# ============================================================

def extract_public_members(content):
    """
    Lightweight language-specific approximation for C#.

    Returns public methods/properties visible to tests.

    This is intentionally conservative.
    Later this belongs in the .NET language adapter.
    """

    members = set()

    method_pattern = re.compile(
        r"\bpublic\s+"
        r"(?:static\s+)?"
        r"(?:async\s+)?"
        r"[A-Za-z0-9_<>,?\[\].]+\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
    )

    property_pattern = re.compile(
        r"\bpublic\s+"
        r"[A-Za-z0-9_<>,?\[\].]+\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"\{"
    )

    members.update(
        method_pattern.findall(
            content
        )
    )

    members.update(
        property_pattern.findall(
            content
        )
    )

    return members


def production_symbols(
    implementation_files
):
    symbols = set()

    for content in (
        implementation_files.values()
    ):
        symbols.update(
            extract_declared_symbols(
                content
            )
        )

    return symbols


def production_public_members(
    implementation_files
):
    members = set()

    for content in (
        implementation_files.values()
    ):
        members.update(
            extract_public_members(
                content
            )
        )

    return members


# ============================================================
# TEST SNIPPET GUARD
# ============================================================

def contains_production_redefinition(
    snippet,
    implementation_files
):
    production = production_symbols(
        implementation_files
    )

    declared = (
        extract_declared_symbols(
            snippet
        )
    )

    return sorted(
        production & declared
    )


def detect_suspicious_private_access(
    snippet
):
    """
    Detect direct member access such as:

        service._orders
        foo._cache
        sut._items

    Tests should normally use the public contract.

    Reflection-based access is not blocked here because
    it would need separate policy handling.
    """

    pattern = re.compile(
        r"\b[A-Za-z_][A-Za-z0-9_]*\."
        r"(_[A-Za-z_][A-Za-z0-9_]*)\b"
    )

    return sorted(
        set(
            pattern.findall(
                snippet
            )
        )
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

    private_accesses = (
        detect_suspicious_private_access(
            snippet
        )
    )

    for member in private_accesses:
        issues.append(
            "Test snippet accesses a likely private "
            f"production member directly: {member}"
        )

    return issues


# ============================================================
# PRODUCTION GUARD
# ============================================================

def production_guard(content):
    forbidden_patterns = {
        "using Xunit":
            r"\busing\s+Xunit\s*;",

        "[Fact]":
            r"\[Fact(?:Attribute)?\]",

        "[Theory]":
            r"\[Theory(?:Attribute)?\]",

        "Assert":
            r"\bAssert\.",

        "test class":
            r"\b(?:class|record)\s+"
            r"[A-Za-z_][A-Za-z0-9_]*Tests\b"
    }

    issues = []

    for label, pattern in (
        forbidden_patterns.items()
    ):
        if re.search(
            pattern,
            content
        ):
            issues.append(
                "Production output contains "
                f"test artifact: {label}"
            )

    return issues
