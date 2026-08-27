"""
Regression coverage for the future-work isolation boundary.

The Ledger benchmark run showed the implementation agent reading later
queued specifications (007, 008) while implementing an earlier one, then
implementing behaviour those later specs described. Removing future work
from a prompt is not sufficient on its own: the agentic phase can
rediscover anything through its own file tools.

These tests pin the boundary at every layer it has to hold:

- project context (planner / reviewer / current-work resolution)
- repository file discovery (what the planner is even shown)
- the agentic `list_files` and `read_file` tools

Nothing here depends on numbering schemes or a particular directory
layout: the fixtures use arbitrary work-item names.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.isolation import (  # noqa: E402
    WorkIsolation,
    build_work_isolation,
    extract_declared_dependencies,
    sibling_source_paths,
)
from core.phases.agentic_implementation_phase import (  # noqa: E402
    _execute_tool,
    _list_files,
    _read_file,
)
from core.project_context import build_project_context  # noqa: E402
from core.repository import discover_files  # noqa: E402


CURRENT = "work/queue/alpha-widget-lookup.md"

LATER_ONE = "work/queue/beta-widget-retirement.md"

LATER_TWO = "work/queue/gamma-widget-atomicity.md"

SHARED_DOC = "docs/architecture.md"


def make_project(root, current_body=None):
    """
    A minimal repository: three queued work items, a shared doc, and
    ordinary production/test sources.
    """

    queue = root / "work" / "queue"
    queue.mkdir(parents=True)

    (root / "docs").mkdir()

    (root / CURRENT).write_text(
        current_body
        or "# Widget Lookup\n\n1. Add `FindWidget`.\n"
    )

    (root / LATER_ONE).write_text(
        "# Widget Retirement\n\n1. Add `RetireWidget`.\n"
    )

    (root / LATER_TWO).write_text(
        "# Widget Atomicity\n\n1. Make retirement atomic.\n"
    )

    (root / SHARED_DOC).write_text(
        "# Architecture\n\nLayering rules.\n"
    )

    src = root / "src"
    src.mkdir()

    (src / "Widgets.cs").write_text(
        "public class WidgetRegistry { }\n"
    )
    (src / "Widgets.csproj").write_text("<Project />\n")

    tests = root / "tests"
    tests.mkdir()

    (tests / "WidgetTests.cs").write_text(
        "public class WidgetTests { }\n"
    )

    return root


def queue_isolation(root, current=CURRENT):
    return build_work_isolation(
        current,
        [LATER_ONE, LATER_TWO],
        source_text=(root / current).read_text()
    )


class FutureWorkIsUnreachableTests(unittest.TestCase):
    """
    Requirement 1: future queued work is invisible while executing the
    current work item.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()

        self.root = make_project(
            Path(self._tmp.name)
        )

        self.isolation = queue_isolation(self.root)

        self.addCleanup(self._tmp.cleanup)

    def test_future_items_are_restricted(self):
        self.assertTrue(
            self.isolation.is_restricted(LATER_ONE)
        )
        self.assertTrue(
            self.isolation.is_restricted(LATER_TWO)
        )

    def test_current_item_is_not_restricted(self):
        self.assertFalse(
            self.isolation.is_restricted(CURRENT)
        )

    def test_future_items_are_absent_from_project_context(self):
        context = build_project_context(
            self.root,
            selected_source_path=CURRENT,
            isolate_selected_source=True,
            isolation=self.isolation
        )

        rendered = str(context)

        for path in (LATER_ONE, LATER_TWO):
            self.assertNotIn(path, rendered)

        self.assertEqual(
            context["current_work"]["path"],
            CURRENT
        )

    def test_future_items_are_absent_from_planner_file_listing(self):
        files = discover_files(
            str(self.root),
            isolation=self.isolation
        )

        self.assertIn(CURRENT, files)

        for path in (LATER_ONE, LATER_TWO):
            self.assertNotIn(path, files)

    def test_without_isolation_future_items_are_still_visible(self):
        # Guards the test itself: the assertions above must be proving
        # the boundary works, not that the fixture lacks the files.
        files = discover_files(
            str(self.root)
        )

        self.assertIn(LATER_ONE, files)


class CompletedBehaviorRemainsAvailableTests(unittest.TestCase):
    """
    Requirement 2: isolation restricts *queued work documents*, never the
    repository. Behaviour delivered by earlier work stays reachable the
    way it always was — through committed source.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()

        self.root = make_project(
            Path(self._tmp.name)
        )

        self.isolation = queue_isolation(self.root)

        self.addCleanup(self._tmp.cleanup)

    def test_production_and_test_sources_stay_visible(self):
        files = discover_files(
            str(self.root),
            isolation=self.isolation
        )

        self.assertIn("src/Widgets.cs", files)
        self.assertIn("tests/WidgetTests.cs", files)

    def test_production_source_is_readable_by_the_agent(self):
        content = _read_file(
            self.root,
            "src/Widgets.cs",
            self.isolation
        )

        self.assertIn("WidgetRegistry", content)

    def test_unrelated_documents_stay_visible(self):
        self.assertFalse(
            self.isolation.is_restricted(SHARED_DOC)
        )

        files = discover_files(
            str(self.root),
            isolation=self.isolation
        )

        self.assertIn(SHARED_DOC, files)


class AgenticToolsCannotBypassIsolationTests(unittest.TestCase):
    """
    Requirement 3: the agentic file tools are the actual leak path seen
    in the benchmark. They must enforce the same boundary.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()

        self.root = make_project(
            Path(self._tmp.name)
        )

        self.isolation = queue_isolation(self.root)

        self.addCleanup(self._tmp.cleanup)

    def test_list_files_omits_future_work(self):
        listing = _list_files(
            self.root,
            None,
            self.isolation
        )

        self.assertIn(CURRENT, listing)

        for path in (LATER_ONE, LATER_TWO):
            self.assertNotIn(path, listing)

    def test_list_files_scoped_to_the_queue_directory_omits_future_work(self):
        listing = _list_files(
            self.root,
            "work/queue",
            self.isolation
        )

        self.assertIn(CURRENT, listing)
        self.assertNotIn(LATER_ONE, listing)

    def test_read_file_refuses_future_work_without_leaking_content(self):
        result = _read_file(
            self.root,
            LATER_ONE,
            self.isolation
        )

        self.assertIn("READ REJECTED", result)
        self.assertNotIn("RetireWidget", result)

    def test_read_file_refuses_relative_path_spellings(self):
        for spelling in (
            "./" + LATER_ONE,
            "work/./queue/beta-widget-retirement.md",
        ):
            result = _read_file(
                self.root,
                spelling,
                self.isolation
            )

            self.assertIn(
                "READ REJECTED",
                result,
                f"{spelling!r} bypassed isolation"
            )

    def test_read_file_through_the_tool_dispatcher_is_refused(self):
        call = {
            "function": {
                "name": "read_file",
                "arguments": {"path": LATER_TWO}
            }
        }

        result = _execute_tool(
            self.root,
            call,
            writable_paths={"src/Widgets.cs"},
            adapter=None,
            repository_files=[],
            isolation=self.isolation
        )

        self.assertIn("READ REJECTED", result)

        # The refusal must not carry the document body back. (The path
        # itself is echoed, so assert on body text only.)
        self.assertNotIn("Make retirement atomic", result)

    def test_list_files_through_the_tool_dispatcher_is_filtered(self):
        call = {
            "function": {
                "name": "list_files",
                "arguments": {}
            }
        }

        result = _execute_tool(
            self.root,
            call,
            writable_paths={"src/Widgets.cs"},
            adapter=None,
            repository_files=[],
            isolation=self.isolation
        )

        self.assertIn(CURRENT, result)
        self.assertNotIn(LATER_ONE, result)

    def test_tools_are_unrestricted_when_no_boundary_is_supplied(self):
        # Backwards compatibility: a caller that passes no isolation
        # keeps the pre-existing behaviour exactly.
        listing = _list_files(self.root)

        self.assertIn(LATER_ONE, listing)

        self.assertIn(
            "RetireWidget",
            _read_file(self.root, LATER_ONE)
        )


class DeclaredDependencyTests(unittest.TestCase):
    """
    The boundary is not absolute: a work item may explicitly declare that
    it needs another document.
    """

    def test_section_style_declaration_is_parsed(self):
        declared = extract_declared_dependencies(
            "# Item\n\n"
            "## Depends On\n\n"
            "- work/queue/beta-widget-retirement.md\n\n"
            "## Requirements\n\n"
            "1. Something.\n"
        )

        self.assertEqual(
            declared,
            [LATER_ONE]
        )

    def test_key_style_declaration_is_parsed(self):
        declared = extract_declared_dependencies(
            "depends_on: docs/architecture.md, "
            "work/queue/beta-widget-retirement.md\n"
        )

        self.assertEqual(
            sorted(declared),
            sorted([SHARED_DOC, LATER_ONE])
        )

    def test_requirements_prose_is_not_mistaken_for_a_declaration(self):
        self.assertEqual(
            extract_declared_dependencies(
                "# Item\n\n## Requirements\n\n"
                "1. This depends on nothing.\n"
            ),
            []
        )

    def test_declared_dependency_is_readmitted(self):
        with TemporaryDirectory() as tmp:
            root = make_project(
                Path(tmp),
                current_body=(
                    "# Widget Lookup\n\n"
                    "## Depends On\n\n"
                    f"- {LATER_ONE}\n\n"
                    "## Requirements\n\n1. Add `FindWidget`.\n"
                )
            )

            isolation = queue_isolation(root)

            self.assertFalse(
                isolation.is_restricted(LATER_ONE)
            )

            self.assertTrue(
                isolation.is_restricted(LATER_TWO)
            )

            self.assertIn(
                "RetireWidget",
                _read_file(root, LATER_ONE, isolation)
            )


class IsolationDerivationTests(unittest.TestCase):
    def test_siblings_are_the_generic_fallback_peer_set(self):
        sources = [
            {"path": CURRENT},
            {"path": LATER_ONE},
            {"path": SHARED_DOC},
        ]

        self.assertEqual(
            sibling_source_paths(sources, CURRENT),
            [LATER_ONE]
        )

    def test_disabled_isolation_restricts_nothing(self):
        isolation = WorkIsolation.disabled()

        self.assertFalse(isolation.active)
        self.assertFalse(
            isolation.is_restricted(LATER_ONE)
        )

    def test_round_trips_through_config_serialization(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))

            original = queue_isolation(root)

            restored = WorkIsolation.from_dict(
                original.to_dict()
            )

            self.assertEqual(
                restored.restricted,
                original.restricted
            )

            self.assertTrue(
                restored.is_restricted(LATER_ONE)
            )


if __name__ == "__main__":
    unittest.main()
