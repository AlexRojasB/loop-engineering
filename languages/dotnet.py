import re

from core.symbols import spec_requests_symbol
from languages.base import LanguageAdapter


class DotNetAdapter(LanguageAdapter):
    name = "dotnet"

    PROJECT_EXTENSIONS = (
        ".csproj",
        ".fsproj",
        ".vbproj"
    )

    SOLUTION_EXTENSIONS = (
        ".sln",
        ".slnx"
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

    SOURCE_EXTENSIONS = (
        ".cs",
        ".fs",
        ".vb"
    )

    BROKEN_TEST_CODES = {
        "CS1001",
        "CS1002",
        "CS1003",
        "CS1022",
        "CS1513",
        "CS1529"
    }

    MISSING_FEATURE_CODES = {
        "CS0103",
        "CS0117",
        "CS0246",
        "CS1061"
    }

    def can_handle(self, files):
        return any(
            path.lower().endswith(
                self.PROJECT_EXTENSIONS
                + self.SOLUTION_EXTENSIONS
            )
            for path in files
        )

    def build_command(
        self,
        workspace_files
    ):
        solution = self._find_solution(
            workspace_files
        )

        if solution:
            return f"dotnet build {solution}"

        project = self._find_project(
            workspace_files
        )

        if project:
            return f"dotnet build {project}"

        return "dotnet build"

    def test_command(
        self,
        workspace_files
    ):
        solution = self._find_solution(
            workspace_files
        )

        if solution:
            return f"dotnet test {solution}"

        test_projects = [
            path
            for path in workspace_files
            if path.lower().endswith(
                self.PROJECT_EXTENSIONS
            )
            and self.is_test_path(path)
        ]

        if test_projects:
            return (
                f"dotnet test "
                f"{test_projects[0]}"
            )

        return "dotnet test"

    def build_argv(
        self,
        workspace_files
    ):
        solution = self._find_solution(
            workspace_files
        )

        if solution:
            return ["dotnet", "build", solution]

        project = self._find_project(
            workspace_files
        )

        if project:
            return ["dotnet", "build", project]

        return ["dotnet", "build"]

    def test_argv(
        self,
        workspace_files,
        filter=None
    ):
        solution = self._find_solution(
            workspace_files
        )

        if solution:
            argv = ["dotnet", "test", solution]

        else:
            test_projects = [
                path
                for path in workspace_files
                if path.lower().endswith(
                    self.PROJECT_EXTENSIONS
                )
                and self.is_test_path(path)
            ]

            if test_projects:
                argv = [
                    "dotnet",
                    "test",
                    test_projects[0]
                ]

            else:
                argv = ["dotnet", "test"]

        if filter:
            argv = argv + ["--filter", filter]

        return argv

    # ------------------------------------------------------------------
    # Contract diagnostic classification
    # ------------------------------------------------------------------
    #
    # Three small, meaning-based code sets. These are C# compiler error
    # codes, not benchmark patterns: each code has one documented
    # meaning, and the meaning is what decides the category.

    # The generated test file is not valid C# at all.
    CONTRACT_SYNTAX_CODES = {
        "CS1001",
        "CS1002",
        "CS1003",
        "CS1022",
        "CS1513",
        "CS1529",
        "CS0102",
        "CS0111",
        "CS0128",
    }

    # "the thing you asked for does not exist (yet)". Legitimate
    # test-first RED *when the current spec requested that symbol*.
    CONTRACT_MISSING_SYMBOL_CODES = {
        "CS0103",
        "CS0117",
        "CS0246",
        "CS1061",
        "CS1501",
        "CS1729",
        "CS1739",
        "CS7036",
    }

    # "you used something that already exists, incorrectly". Never
    # legitimate RED: the requested feature being absent cannot cause a
    # type-conversion or operator error against the existing API.
    CONTRACT_MISUSE_CODES = {
        "CS0019",
        "CS0021",
        "CS0029",
        "CS0030",
        "CS0173",
        "CS0266",
        "CS0428",
        "CS1503",
        "CS1620",
        "CS1662",
    }

    DIAGNOSTIC_PATTERN = re.compile(
        r"error\s+(?P<code>CS\d+)\s*:\s*(?P<message>.*)"
    )

    SYMBOL_PATTERNS = {
        "CS0103": [
            r"The name '(?P<symbol>[^']+)' does not exist",
        ],
        "CS0117": [
            r"does not contain a definition for "
            r"'(?P<symbol>[^']+)'",
        ],
        "CS0246": [
            r"type or namespace name '(?P<symbol>[^']+)'",
        ],
        "CS1061": [
            r"does not contain a definition for "
            r"'(?P<symbol>[^']+)'",
        ],
        "CS1501": [
            r"No overload for method '(?P<symbol>[^']+)'",
        ],
        "CS1729": [
            r"'(?P<symbol>[^']+)' does not contain a constructor",
        ],
        "CS1739": [
            r"best overload for '(?P<symbol>[^']+)'",
        ],
        "CS7036": [
            r"required parameter '[^']+' of "
            r"'(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)",
        ],
    }

    def _diagnostic_symbol(
        self,
        code,
        message
    ):
        for pattern in self.SYMBOL_PATTERNS.get(
            code,
            []
        ):
            match = re.search(
                pattern,
                message
            )

            if match:
                return match.group("symbol")

        return None

    def parse_diagnostics(
        self,
        output
    ):
        """
        Parse compiler diagnostics out of build/test output, de-duplicated
        by (code, symbol, message). MSBuild repeats the same error once
        per project reference; the harness only cares about distinct
        defects.
        """

        seen = set()
        diagnostics = []

        for line in (output or "").splitlines():
            match = self.DIAGNOSTIC_PATTERN.search(
                line
            )

            if not match:
                continue

            code = match.group("code")

            message = match.group(
                "message"
            ).strip()

            # Strip the trailing "[/path/to/Project.csproj]" MSBuild
            # appends, so the same error from two projects collapses.
            message = re.sub(
                r"\s*\[[^\]]*\.(?:cs|fs|vb)proj\]\s*$",
                "",
                message
            )

            symbol = self._diagnostic_symbol(
                code,
                message
            )

            key = (code, symbol, message)

            if key in seen:
                continue

            seen.add(key)

            diagnostics.append(
                {
                    "code": code,
                    "symbol": symbol,
                    "message": message
                }
            )

        return diagnostics

    def classify_contract_diagnostics(
        self,
        output,
        spec_text=""
    ):
        diagnostics = self.parse_diagnostics(
            output
        )

        expected_red = []
        invalid = []
        broken_syntax = []
        unclassified = []
        issues = []

        for diagnostic in diagnostics:
            code = diagnostic["code"]
            symbol = diagnostic["symbol"]

            entry = dict(diagnostic)

            if code in self.CONTRACT_SYNTAX_CODES:
                entry["category"] = "broken_syntax"

                entry["reason"] = (
                    "The generated test code is not "
                    "syntactically valid."
                )

                broken_syntax.append(entry)

                issues.append(
                    f"{code}: {diagnostic['message']}"
                )

                continue

            if code in self.CONTRACT_MISUSE_CODES:
                entry["category"] = "invalid_contract"

                entry["reason"] = (
                    "The test misuses an API that already "
                    "exists. A missing future feature cannot "
                    "cause this diagnostic."
                )

                invalid.append(entry)

                issues.append(
                    f"{code}: {diagnostic['message']} "
                    "- this is a defect in the test setup, "
                    "not a missing future feature. Use the "
                    "existing API exactly as it is declared "
                    "in the current production code."
                )

                continue

            if code in self.CONTRACT_MISSING_SYMBOL_CODES:
                if symbol and spec_requests_symbol(
                    symbol,
                    spec_text
                ):
                    entry["category"] = "expected_red"

                    entry["reason"] = (
                        f"'{symbol}' is requested by the "
                        "current authoritative specification "
                        "and does not exist yet."
                    )

                    expected_red.append(entry)

                    continue

                if symbol is None:
                    entry["category"] = "unclassified"

                    entry["reason"] = (
                        "Missing-symbol diagnostic whose "
                        "symbol could not be extracted."
                    )

                    unclassified.append(entry)

                    continue

                entry["category"] = "unrequested_api"

                entry["reason"] = (
                    f"'{symbol}' is not requested by the "
                    "current authoritative specification."
                )

                invalid.append(entry)

                issues.append(
                    f"{code}: the test references "
                    f"'{symbol}', which the current "
                    "authoritative specification does not "
                    "request and which does not exist in "
                    "production. Do not invent APIs outside "
                    "the current specification."
                )

                continue

            entry["category"] = "unclassified"

            entry["reason"] = (
                "Diagnostic code is not classified by this "
                "adapter."
            )

            unclassified.append(entry)

        if broken_syntax or invalid:
            verdict = "INVALID"

        elif expected_red:
            verdict = "VALID"

        elif unclassified:
            verdict = "UNKNOWN"

        else:
            verdict = "VALID"

        return {
            "supported": True,
            "verdict": verdict,
            "expected_red": expected_red,
            "invalid": invalid,
            "broken_syntax": broken_syntax,
            "unclassified": unclassified,
            "issues": issues,
            "compiles_clean": not diagnostics
        }

    def classify_red_state(
        self,
        output
    ):
        compiler_codes = set(
            re.findall(
                r"\bCS\d+\b",
                output
            )
        )

        if (
            compiler_codes
            & self.BROKEN_TEST_CODES
        ):
            return {
                "classification":
                    "BROKEN_TEST_SUITE",

                "reason":
                    "C# syntax or structural "
                    "errors detected."
            }

        if (
            compiler_codes
            & self.MISSING_FEATURE_CODES
        ):
            return {
                "classification":
                    "EXPECTED_RED",

                "reason":
                    "Tests reference requested "
                    "members/types that are not "
                    "implemented yet."
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
                    "Tests execute but requested "
                    "behavior is not satisfied yet."
            }

        return {
            "classification":
                "UNKNOWN",

            "reason":
                "Could not confidently classify "
                "the .NET test state."
        }

    def extract_failure_paths(
        self,
        output
    ):
        paths = set()

        patterns = [
            (
                r"(/[^\s:(]+?\.cs)"
                r"\(\d+,\d+\)"
            ),
            (
                r"(/[^\s:(]+?\.cs)"
                r":\d+"
            )
        ]

        for pattern in patterns:
            paths.update(
                re.findall(
                    pattern,
                    output
                )
            )

        return sorted(paths)

    def is_config_path(
        self,
        path
    ):
        return path.lower().endswith(
            self.CONFIG_EXTENSIONS
        )

    def _find_solution(
        self,
        files
    ):
        solutions = [
            path
            for path in files
            if path.lower().endswith(
                self.SOLUTION_EXTENSIONS
            )
        ]

        if not solutions:
            return None

        return sorted(
            solutions,
            key=lambda value: (
                value.count("/"),
                len(value)
            )
        )[0]

    def _find_project(
        self,
        files
    ):
        projects = [
            path
            for path in files
            if path.lower().endswith(
                self.PROJECT_EXTENSIONS
            )
            and not self.is_test_path(path)
        ]

        if not projects:
            return None

        return sorted(
            projects,
            key=lambda value: (
                value.count("/"),
                len(value)
            )
        )[0]
