"""
Deterministic regression tests for the structured agentic command tool.

Covers the fix for the concrete limitation found in the Inventory
benchmark: the agent needed filtered test execution
(`dotnet test <solution> --filter <filter>`), but the old tool only
accepted an exact-string allow-list of full shell command strings run
via subprocess shell=True.

This replaces that allow-list with a structured `run_operation` tool
(operation + optional filter) whose argv is constructed entirely by the
harness/LanguageAdapter — no shell is invoked for build/test/git
operations, so shell metacharacters in a filter value can never become
shell syntax.

No Ollama/model service and no real dotnet toolchain are required. A
fake LanguageAdapter (a tiny Python script) stands in for `dotnet`/`git`
so these tests actually execute real subprocesses and prove there is no
shell interpretation, without depending on the .NET SDK being installed.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.phases.agentic_implementation_phase import (  # noqa: E402
    SUPPORTED_OPERATIONS,
    _execute_tool,
    _resolve_operation_argv,
    _run_operation,
    _tools,
)
from languages.base import LanguageAdapter  # noqa: E402
from languages.dotnet import DotNetAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeAdapter:
    """
    Stands in for a real LanguageAdapter using a Python subprocess
    instead of dotnet, so these tests run real, unmocked processes
    without requiring the .NET SDK.
    """

    def build_argv(self, workspace_files):
        return [sys.executable, "-c", "print('BUILD_OK')"]

    def test_argv(self, workspace_files, filter=None):
        script = (
            "import sys; "
            "print('TEST_OK'); "
            "print(repr(sys.argv[1:]))"
        )
        argv = [sys.executable, "-c", script]

        if filter:
            argv = argv + [filter]

        return argv


class NoFilterSupportAdapter(FakeAdapter):
    """A language adapter that cannot express a structured test filter."""

    def test_argv(self, workspace_files, filter=None):
        if filter:
            return None

        return super().test_argv(workspace_files)


class MinimalGenericAdapter(LanguageAdapter):
    """Exercises LanguageAdapter's generic argv fallback (goal 8)."""

    name = "minimal"

    def can_handle(self, files):
        return True

    def build_command(self, workspace_files):
        return "make build"

    def test_command(self, workspace_files):
        return "make test"


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

class ToolSchemaTests(unittest.TestCase):
    def test_run_operation_replaces_run_command(self):
        names = [t["function"]["name"] for t in _tools()]
        self.assertIn("run_operation", names)
        self.assertNotIn("run_command", names)

    def test_operation_is_a_closed_enum_not_a_free_string(self):
        tool = next(
            t for t in _tools()
            if t["function"]["name"] == "run_operation"
        )
        operation_schema = tool[
            "function"
        ]["parameters"]["properties"]["operation"]

        self.assertEqual(
            set(operation_schema["enum"]),
            set(SUPPORTED_OPERATIONS)
        )
        self.assertEqual(
            tool["function"]["parameters"]["required"],
            ["operation"]
        )

    def test_filter_argument_exists_and_is_a_plain_string(self):
        tool = next(
            t for t in _tools()
            if t["function"]["name"] == "run_operation"
        )
        filter_schema = tool[
            "function"
        ]["parameters"]["properties"]["filter"]

        self.assertEqual(filter_schema["type"], "string")


# ---------------------------------------------------------------------------
# Operation resolution (pure, no subprocess)
# ---------------------------------------------------------------------------

class ResolveOperationArgvTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()

    def test_build_is_allowed(self):
        argv, error = _resolve_operation_argv(
            "build", None, self.adapter, []
        )
        self.assertIsNone(error)
        self.assertEqual(
            argv,
            [sys.executable, "-c", "print('BUILD_OK')"]
        )

    def test_full_test_is_allowed(self):
        argv, error = _resolve_operation_argv(
            "test", None, self.adapter, []
        )
        self.assertIsNone(error)
        self.assertEqual(len(argv), 3)
        self.assertNotIn("--filter", argv)

    def test_filtered_test_is_allowed(self):
        argv, error = _resolve_operation_argv(
            "test_filtered",
            "Reserve_ReservedQuantity_IncreasesByReservedAmount",
            self.adapter,
            []
        )
        self.assertIsNone(error)
        self.assertEqual(
            argv[-1],
            "Reserve_ReservedQuantity_IncreasesByReservedAmount"
        )

    def test_filtered_test_without_filter_value_is_rejected(self):
        argv, error = _resolve_operation_argv(
            "test_filtered", None, self.adapter, []
        )
        self.assertIsNone(argv)
        self.assertIn("requires", error)

    def test_filtered_test_rejected_when_adapter_has_no_filter_support(self):
        argv, error = _resolve_operation_argv(
            "test_filtered",
            "SomeTest",
            NoFilterSupportAdapter(),
            []
        )
        self.assertIsNone(argv)
        self.assertIn("does not support filtered", error)

    def test_git_status_is_the_fixed_short_form(self):
        argv, error = _resolve_operation_argv(
            "git_status", None, self.adapter, []
        )
        self.assertIsNone(error)
        self.assertEqual(argv, ["git", "status", "--short"])

    def test_git_diff_is_the_plain_form(self):
        argv, error = _resolve_operation_argv(
            "git_diff", None, self.adapter, []
        )
        self.assertIsNone(error)
        self.assertEqual(argv, ["git", "diff"])

    def test_arbitrary_command_string_as_operation_is_rejected(self):
        argv, error = _resolve_operation_argv(
            "rm -rf /", None, self.adapter, []
        )
        self.assertIsNone(argv)
        self.assertIn("OPERATION REJECTED", error)
        for op in SUPPORTED_OPERATIONS:
            self.assertIn(op, error)

    def test_shell_chaining_operation_strings_are_rejected(self):
        hostile_operations = (
            "dotnet build X.slnx && rm -rf /",
            "git status --short; cat /etc/passwd",
            "dotnet test X.slnx | tee /tmp/x",
            "echo hi & echo bye",
        )

        for hostile in hostile_operations:
            argv, error = _resolve_operation_argv(
                hostile, None, self.adapter, []
            )
            self.assertIsNone(
                argv, f"expected rejection for {hostile!r}"
            )
            self.assertIn("OPERATION REJECTED", error)

    def test_redirection_operation_strings_are_rejected(self):
        argv, error = _resolve_operation_argv(
            'dotnet test X.slnx --filter "Foo" 2>&1',
            None,
            self.adapter,
            []
        )
        self.assertIsNone(argv)
        self.assertIn("OPERATION REJECTED", error)


# ---------------------------------------------------------------------------
# Real subprocess execution: proves no shell is involved
# ---------------------------------------------------------------------------

class RunOperationExecutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.adapter = FakeAdapter()

    def test_full_build_actually_runs(self):
        result = _run_operation(
            self.root, "build", None, self.adapter, []
        )
        self.assertTrue(result.startswith("exit_code=0"))
        self.assertIn("BUILD_OK", result)

    def test_full_test_actually_runs(self):
        result = _run_operation(
            self.root, "test", None, self.adapter, []
        )
        self.assertTrue(result.startswith("exit_code=0"))
        self.assertIn("TEST_OK", result)

    def test_filtered_test_actually_runs_with_filter_value_intact(self):
        result = _run_operation(
            self.root,
            "test_filtered",
            "Reserve_ReservedQuantity_IncreasesByReservedAmount",
            self.adapter,
            []
        )
        self.assertTrue(result.startswith("exit_code=0"))
        self.assertIn(
            "Reserve_ReservedQuantity_IncreasesByReservedAmount",
            result
        )

    def test_filter_with_shell_metacharacters_cannot_execute_shell_syntax(self):
        marker = self.root / "should_not_exist"

        hostile_filter = (
            "harmless"
            f"; touch {marker}"
            "; echo pwned `id` $(id)"
            " > /tmp/agentic-tool-injection-test"
            " & echo done"
        )

        result = _run_operation(
            self.root,
            "test_filtered",
            hostile_filter,
            self.adapter,
            []
        )

        self.assertTrue(result.startswith("exit_code=0"))

        # The entire hostile string must arrive as ONE literal argv
        # element in the fake runner's echoed argv, not be split or
        # reinterpreted.
        self.assertIn(repr([hostile_filter]), result)

        # Decisive proof: none of the injected shell syntax executed.
        self.assertFalse(marker.exists())

    def test_unknown_operation_runs_nothing(self):
        marker = self.root / "should_not_exist_either"

        result = _run_operation(
            self.root, f"touch {marker}", None, self.adapter, []
        )

        self.assertIn("OPERATION REJECTED", result)
        self.assertFalse(marker.exists())


# ---------------------------------------------------------------------------
# _execute_tool: the actual dispatch path a model's tool call goes through
# ---------------------------------------------------------------------------

def model_tool_call(name, arguments):
    return {
        "function": {
            "name": name,
            "arguments": arguments
        }
    }


class ExecuteToolDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.adapter = FakeAdapter()

    def test_model_requesting_filtered_test_succeeds(self):
        call = model_tool_call(
            "run_operation",
            {
                "operation": "test_filtered",
                "filter":
                    "Reserve_ReservedQuantity_"
                    "IncreasesByReservedAmount"
            }
        )

        result = _execute_tool(
            self.root, call, set(), self.adapter, []
        )

        self.assertTrue(result.startswith("exit_code=0"))

    def test_model_trying_the_old_raw_shell_string_is_rejected(self):
        # This is the exact shape of the request that originally
        # tripped the old exact-string allow-list, now attempted
        # against the new schema by (mis)using "operation" as a raw
        # command string.
        call = model_tool_call(
            "run_operation",
            {
                "operation":
                    'dotnet test InventoryPipeline.slnx '
                    '--filter "FullyQualifiedName~'
                    'Reserve_ReservedQuantity_'
                    'IncreasesByReservedAmount" 2>&1'
            }
        )

        result = _execute_tool(
            self.root, call, set(), self.adapter, []
        )

        self.assertIn("OPERATION REJECTED", result)

    def test_unknown_tool_name_is_reported_not_executed(self):
        call = model_tool_call("run_command", {"command": "ls"})

        result = _execute_tool(
            self.root, call, set(), self.adapter, []
        )

        self.assertIn("Unknown tool", result)


# ---------------------------------------------------------------------------
# LanguageAdapter argv construction (goal 8: language independence)
# ---------------------------------------------------------------------------

class DotNetAdapterArgvTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DotNetAdapter()
        self.files = [
            "InventoryPipeline.slnx",
            "InventoryPipeline/Program.cs",
            "InventoryPipeline.Tests/UnitTest1.cs",
        ]

    def test_build_argv_targets_the_solution(self):
        self.assertEqual(
            self.adapter.build_argv(self.files),
            ["dotnet", "build", "InventoryPipeline.slnx"]
        )

    def test_test_argv_without_filter_matches_existing_behavior(self):
        self.assertEqual(
            self.adapter.test_argv(self.files),
            ["dotnet", "test", "InventoryPipeline.slnx"]
        )

    def test_test_argv_with_filter_matches_real_dotnet_syntax(self):
        argv = self.adapter.test_argv(
            self.files,
            filter=(
                "FullyQualifiedName~Reserve_ReservedQuantity_"
                "IncreasesByReservedAmount"
            )
        )

        self.assertEqual(
            argv,
            [
                "dotnet", "test", "InventoryPipeline.slnx",
                "--filter",
                "FullyQualifiedName~Reserve_ReservedQuantity_"
                "IncreasesByReservedAmount"
            ]
        )

    def test_hostile_filter_value_stays_one_argv_element(self):
        hostile = "Foo; rm -rf /; echo `id` $(id) 2>&1"

        argv = self.adapter.test_argv(self.files, filter=hostile)

        self.assertEqual(argv[-1], hostile)
        self.assertEqual(len(argv), 5)


class GenericLanguageAdapterArgvTests(unittest.TestCase):
    def setUp(self):
        self.adapter = MinimalGenericAdapter()

    def test_build_argv_falls_back_to_tokenized_build_command(self):
        self.assertEqual(
            self.adapter.build_argv([]),
            ["make", "build"]
        )

    def test_test_argv_falls_back_to_tokenized_test_command(self):
        self.assertEqual(
            self.adapter.test_argv([]),
            ["make", "test"]
        )

    def test_filter_is_unsupported_by_default(self):
        self.assertIsNone(
            self.adapter.test_argv([], filter="anything")
        )


if __name__ == "__main__":
    unittest.main()
