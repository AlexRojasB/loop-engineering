import re

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
