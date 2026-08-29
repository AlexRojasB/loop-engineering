"""
Regression coverage for bounded cross-SPEC-ATTEMPT failure memory.

The multi-spec runner restores the repository between outer attempts,
which is correct and must stay. The cost observed in the Ledger run was
amnesia: attempt 1 discovered a contract defect, the repo was restored,
and attempt 2 regenerated essentially the same defect with no knowledge
that it had already been disproved.

These tests pin the three properties that make that memory safe:

- it survives outer attempts for ONE work item;
- it never crosses to another work item;
- it stays bounded and condensed, so prompts do not grow with attempts.

They also pin the deliberate storage choice: memory lives outside the
target repository, so `git restore`, the clean-baseline check and the
automatic completion commit are all unaffected.
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent  # noqa: E402

from core.project_runtime import spec_memory_path  # noqa: E402
from core.spec_memory import (  # noqa: E402
    DEFAULT_LIMIT,
    DEFAULT_MAX_CHARS,
    NO_MEMORY_TEXT,
    SpecFailureMemory,
    condense,
    record_spec_failure,
    spec_memory_text,
    spec_scope_key,
)

SPEC_A = "work/queue/alpha.md"
SPEC_B = "work/queue/beta.md"

BODY_A = "# Alpha\n\n1. Add `Alpha`.\n"
BODY_B = "# Beta\n\n1. Add `Beta`.\n"

FINDING = (
    "Previous contract treated CreateThing's bool return "
    "value as an identifier."
)


class ScopeTests(unittest.TestCase):
    def test_scope_distinguishes_work_items(self):
        self.assertNotEqual(
            spec_scope_key(SPEC_A, BODY_A),
            spec_scope_key(SPEC_B, BODY_B)
        )

    def test_scope_is_content_sensitive(self):
        self.assertNotEqual(
            spec_scope_key(SPEC_A, BODY_A),
            spec_scope_key(SPEC_A, BODY_A + "2. More.\n")
        )

    def test_scope_is_stable_for_identical_input(self):
        self.assertEqual(
            spec_scope_key(SPEC_A, BODY_A),
            spec_scope_key(SPEC_A, BODY_A)
        )


class SurvivesOuterAttemptsTests(unittest.TestCase):
    """Requirement 8."""

    def setUp(self):
        self._tmp = TemporaryDirectory()

        self.path = (
            Path(self._tmp.name) / "spec-memory.json"
        )

        self.scope = spec_scope_key(SPEC_A, BODY_A)

        self.addCleanup(self._tmp.cleanup)

    def test_recorded_finding_is_visible_to_the_next_attempt(self):
        first = SpecFailureMemory.load(
            self.path,
            self.scope
        )

        config = {"spec_memory": first}

        record_spec_failure(
            config,
            "contract/semantic",
            FINDING
        )

        # A fresh outer attempt reloads from storage.
        second = SpecFailureMemory.load(
            self.path,
            self.scope
        )

        self.assertIn(
            FINDING,
            second.as_text()
        )

        self.assertIn(
            "contract/semantic",
            second.as_text()
        )

    def test_prompt_text_is_the_placeholder_when_nothing_was_recorded(self):
        self.assertEqual(
            spec_memory_text(
                {
                    "spec_memory":
                        SpecFailureMemory.load(
                            self.path,
                            self.scope
                        )
                }
            ),
            NO_MEMORY_TEXT
        )

    def test_absent_memory_is_a_no_op_not_an_error(self):
        self.assertFalse(
            record_spec_failure({}, "x", "y")
        )

        self.assertEqual(
            spec_memory_text({}),
            NO_MEMORY_TEXT
        )

    def test_success_clears_the_memory(self):
        memory = SpecFailureMemory.load(
            self.path,
            self.scope
        )

        memory.record("contract/semantic", FINDING)
        memory.save()

        memory.clear()

        self.assertTrue(
            SpecFailureMemory.load(
                self.path,
                self.scope
            ).is_empty
        )


class DoesNotLeakBetweenWorkItemsTests(unittest.TestCase):
    """Requirement 9."""

    def setUp(self):
        self._tmp = TemporaryDirectory()

        self.path = (
            Path(self._tmp.name) / "spec-memory.json"
        )

        self.addCleanup(self._tmp.cleanup)

    def test_spec_b_does_not_inherit_spec_a_findings(self):
        memory_a = SpecFailureMemory.load(
            self.path,
            spec_scope_key(SPEC_A, BODY_A)
        )

        memory_a.record("contract/semantic", FINDING)
        memory_a.save()

        memory_b = SpecFailureMemory.load(
            self.path,
            spec_scope_key(SPEC_B, BODY_B)
        )

        self.assertTrue(memory_b.is_empty)
        self.assertNotIn(FINDING, memory_b.as_text())
        self.assertEqual(memory_b.as_text(), NO_MEMORY_TEXT)

    def test_editing_a_work_item_discards_its_stale_memory(self):
        memory = SpecFailureMemory.load(
            self.path,
            spec_scope_key(SPEC_A, BODY_A)
        )

        memory.record("contract/semantic", FINDING)
        memory.save()

        edited = SpecFailureMemory.load(
            self.path,
            spec_scope_key(
                SPEC_A,
                BODY_A + "2. Also add `Gamma`.\n"
            )
        )

        self.assertTrue(edited.is_empty)

    def test_recording_under_a_new_scope_does_not_merge(self):
        memory_a = SpecFailureMemory.load(
            self.path,
            spec_scope_key(SPEC_A, BODY_A)
        )
        memory_a.record("contract/semantic", FINDING)
        memory_a.save()

        memory_b = SpecFailureMemory.load(
            self.path,
            spec_scope_key(SPEC_B, BODY_B)
        )
        memory_b.record("expected_red", "Different finding.")
        memory_b.save()

        stored = json.loads(
            self.path.read_text()
        )

        self.assertEqual(
            stored["scope"],
            spec_scope_key(SPEC_B, BODY_B)
        )

        self.assertNotIn(
            FINDING,
            json.dumps(stored)
        )


class BoundednessTests(unittest.TestCase):
    def test_entry_count_is_capped(self):
        memory = SpecFailureMemory(scope="s")

        for index in range(DEFAULT_LIMIT * 3):
            memory.record(
                "contract",
                f"finding number {index}"
            )

        self.assertEqual(
            len(memory.entries),
            DEFAULT_LIMIT
        )

        # The cap keeps the most recent findings.
        self.assertIn(
            f"finding number {DEFAULT_LIMIT * 3 - 1}",
            memory.as_text()
        )

    def test_long_model_output_is_condensed_not_dumped(self):
        memory = SpecFailureMemory(scope="s")

        memory.record(
            "contract/semantic",
            "word " * 5000
        )

        entry = memory.lines()[0]

        self.assertLess(
            len(entry),
            DEFAULT_MAX_CHARS + 64
        )

        self.assertTrue(
            entry.endswith("...")
        )

    def test_condense_flattens_newlines(self):
        self.assertEqual(
            condense("a\n\n  b\tc  "),
            "a b c"
        )

    def test_structured_issue_objects_are_condensed(self):
        memory = SpecFailureMemory(scope="s")

        memory.record(
            "contract/semantic",
            {
                "issueType": "setup defect",
                "reason": "identifier provenance"
            }
        )

        self.assertIn(
            "identifier provenance",
            memory.as_text()
        )

    def test_duplicate_findings_are_recorded_once(self):
        memory = SpecFailureMemory(scope="s")

        self.assertTrue(
            memory.record("contract", FINDING)
        )
        self.assertFalse(
            memory.record("contract", FINDING)
        )

        self.assertEqual(len(memory.entries), 1)

    def test_empty_details_are_ignored(self):
        memory = SpecFailureMemory(scope="s")

        memory.record("contract", "")
        memory.record("contract", None)
        memory.record("contract", [])

        self.assertTrue(memory.is_empty)

    def test_iterable_of_issues_is_accepted(self):
        memory = SpecFailureMemory(scope="s")

        memory.record(
            "contract",
            ["first issue", "second issue"]
        )

        self.assertEqual(len(memory.entries), 2)


class StorageLocationTests(unittest.TestCase):
    """
    Repository rollback semantics must be preserved: nothing the memory
    writes may land inside the target repository, where it would be
    reverted by `git restore`, break the clean-baseline check as an
    untracked file, or be swept into the automatic completion commit.
    """

    def test_memory_file_is_outside_the_workspace(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            memory_file = spec_memory_path(workspace)

            self.assertFalse(
                str(memory_file).startswith(
                    str(workspace.resolve())
                )
            )

    def test_distinct_projects_get_distinct_files(self):
        with TemporaryDirectory() as one, TemporaryDirectory() as two:
            self.assertNotEqual(
                spec_memory_path(Path(one)),
                spec_memory_path(Path(two))
            )

    def test_unwritable_location_does_not_raise(self):
        memory = SpecFailureMemory(
            scope="s",
            path="/proc/definitely-not-writable/x.json"
        )

        memory.record("contract", FINDING)
        memory.save()

        self.assertEqual(len(memory.entries), 1)

    def test_corrupt_memory_file_is_ignored(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec-memory.json"
            path.write_text("{not json")

            self.assertTrue(
                SpecFailureMemory.load(
                    path,
                    "scope"
                ).is_empty
            )


class RunnerIntegrationTests(unittest.TestCase):
    """
    The runner binds memory per work item and unbinds it afterwards, so a
    later work item can never observe an earlier one's findings.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()

        self.project = Path(self._tmp.name)

        (self.project / "work" / "queue").mkdir(parents=True)

        (self.project / SPEC_A).write_text(BODY_A)
        (self.project / SPEC_B).write_text(BODY_B)

        self.config = {
            "spec_memory_file": str(
                self.project.parent
                / "outside-spec-memory.json"
            )
        }

        self.addCleanup(self._tmp.cleanup)

        self.addCleanup(
            lambda: Path(
                self.config["spec_memory_file"]
            ).unlink(missing_ok=True)
        )

    def test_attach_binds_memory_for_the_requested_item(self):
        memory = agent.attach_spec_memory(
            self.config,
            self.project,
            SPEC_A
        )

        self.assertIs(
            self.config["spec_memory"],
            memory
        )

        self.assertEqual(
            memory.scope,
            spec_scope_key(SPEC_A, BODY_A)
        )

    def test_switching_work_items_starts_from_empty(self):
        agent.attach_spec_memory(
            self.config,
            self.project,
            SPEC_A
        )

        record_spec_failure(
            self.config,
            "contract/semantic",
            FINDING
        )

        self.assertIn(
            FINDING,
            spec_memory_text(self.config)
        )

        agent.attach_spec_memory(
            self.config,
            self.project,
            SPEC_B
        )

        self.assertEqual(
            spec_memory_text(self.config),
            NO_MEMORY_TEXT
        )


if __name__ == "__main__":
    unittest.main()
