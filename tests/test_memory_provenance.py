"""
Regression coverage for problem 4 of Ledger Full #3: cross-attempt memory
must record HOW each finding was established.

Spec 005's reviewers wrongly rejected a task-authorized future API. Those
false positives were written into memory as flat "findings" and fed into
the next attempt's prompts, where they read as established fact. The next
reviewers repeated them almost verbatim:

    attempt 1  "[semantic] the production Transfer method does NOT have
                this parameter"
    attempt 2  "[structural] Previously Raised Concerns ... the
                production Transfer method does NOT have this parameter"
    attempt 3  "[semantic] ... the tests attempt to access
                transaction.Description which is not defined"

Memory amplified an error instead of preventing one. The fix is not less
memory: it is memory that distinguishes a machine-verified diagnostic
from one model's unconfirmed opinion, and says so in the prompt.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.spec_memory import (  # noqa: E402
    CHALLENGE_CONFIRMED,
    DETERMINISTIC_CONFIRMED,
    IMPLEMENTATION_OBSERVATION,
    NO_MEMORY_TEXT,
    REVIEWER_CONCERN,
    SEMANTIC_CONFIRMATION_REJECTION,
    TRANSIENT_MODEL_FAILURE,
    SpecFailureMemory,
    normalize_entry,
    provenance_for_category,
    record_spec_failure,
)


# The real categories the phases write.
REVIEWER_ISSUE = (
    "the production Transfer method does NOT have this parameter"
)

DETERMINISTIC_ISSUE = (
    "CS1503: Argument 1: cannot convert from 'bool' to 'System.Guid' "
    "- this is a defect in the test setup"
)

CHALLENGE_ISSUE = (
    "A confirmed contract challenge (object_identity) proved the "
    "frozen contract unsatisfiable."
)


class CategoryProvenanceTests(unittest.TestCase):
    def test_deterministic_categories(self):
        for category in (
            "contract/compilation",
            "contract/source",
            "contract/guard",
        ):
            self.assertEqual(
                provenance_for_category(category),
                DETERMINISTIC_CONFIRMED,
                category
            )

    def test_challenge_categories(self):
        for category in (
            "contract/challenge_confirmed",
            "contract/challenge_review",
            "contract/challenge",
        ):
            self.assertEqual(
                provenance_for_category(category),
                CHALLENGE_CONFIRMED,
                category
            )

    def test_reviewer_categories_are_only_concerns(self):
        for category in (
            "contract/structural",
            "contract/semantic",
        ):
            self.assertEqual(
                provenance_for_category(category),
                REVIEWER_CONCERN,
                category
            )

    def test_confirmation_rejection_is_its_own_tier(self):
        self.assertEqual(
            provenance_for_category(
                "contract/semantic_confirmation"
            ),
            SEMANTIC_CONFIRMATION_REJECTION
        )

    def test_implementation_and_transient(self):
        self.assertEqual(
            provenance_for_category("implementation"),
            IMPLEMENTATION_OBSERVATION
        )
        self.assertEqual(
            provenance_for_category("model"),
            TRANSIENT_MODEL_FAILURE
        )

    def test_unknown_category_never_becomes_evidence(self):
        self.assertEqual(
            provenance_for_category("something/new"),
            IMPLEMENTATION_OBSERVATION
        )


class RenderingTests(unittest.TestCase):
    def _memory(self):
        memory = SpecFailureMemory(scope="s")
        memory.record("contract/compilation", DETERMINISTIC_ISSUE)
        memory.record("contract/semantic", REVIEWER_ISSUE)
        memory.record("contract/challenge_confirmed", CHALLENGE_ISSUE)
        memory.record("implementation", "never reached GREEN")
        memory.record("model", "reviewer timed out after 420s")
        return memory

    def test_reviewer_concern_is_labeled_a_hypothesis(self):
        text = self._memory().as_text()

        self.assertIn("UNCONFIRMED REVIEWER CONCERNS", text)
        self.assertIn("HYPOTHESIS", text)

        hypothesis_block = text.split(
            "UNCONFIRMED REVIEWER CONCERNS"
        )[1]

        self.assertIn(REVIEWER_ISSUE, hypothesis_block)

    def test_reviewer_concern_carries_an_explicit_warning(self):
        text = self._memory().as_text()

        lowered = text.lower()

        self.assertIn("never independently confirmed", lowered)
        self.assertIn("do not reject anything solely", lowered)

    def test_deterministic_finding_is_labeled_confirmed(self):
        text = self._memory().as_text()

        confirmed_block = text.split(
            "UNCONFIRMED"
        )[0]

        self.assertIn("CONFIRMED EVIDENCE", confirmed_block)
        self.assertIn("machine-verified", confirmed_block)
        self.assertIn(DETERMINISTIC_ISSUE, confirmed_block)

    def test_confirmed_challenge_is_presented_strongly(self):
        text = self._memory().as_text()

        confirmed_block = text.split(
            "UNCONFIRMED"
        )[0]

        self.assertIn(CHALLENGE_ISSUE, confirmed_block)

    def test_implementation_observation_is_context_only(self):
        text = self._memory().as_text()

        self.assertIn(
            "OBSERVATIONS FROM EARLIER ATTEMPTS",
            text
        )

        observation_block = text.split(
            "OBSERVATIONS FROM EARLIER ATTEMPTS"
        )[1]

        self.assertIn("never reached GREEN", observation_block)

    def test_transient_model_failure_is_stored_but_never_prompted(self):
        memory = self._memory()

        self.assertTrue(
            any(
                entry["provenance"] == TRANSIENT_MODEL_FAILURE
                for entry in memory.entries
            )
        )

        self.assertNotIn(
            "timed out",
            memory.as_text()
        )

    def test_empty_memory_is_unchanged(self):
        self.assertEqual(
            SpecFailureMemory(scope="s").as_text(),
            NO_MEMORY_TEXT
        )

    def test_only_transient_entries_render_as_empty(self):
        memory = SpecFailureMemory(scope="s")
        memory.record("model", "timed out")

        self.assertEqual(memory.as_text(), NO_MEMORY_TEXT)


class BoundednessAndDedupTests(unittest.TestCase):
    def test_duplicates_are_still_collapsed(self):
        memory = SpecFailureMemory(scope="s")

        for _ in range(5):
            memory.record("contract/semantic", REVIEWER_ISSUE)

        self.assertEqual(len(memory.entries), 1)

    def test_same_body_under_different_categories_is_kept(self):
        memory = SpecFailureMemory(scope="s")

        memory.record("contract/semantic", REVIEWER_ISSUE)
        memory.record("contract/compilation", REVIEWER_ISSUE)

        self.assertEqual(len(memory.entries), 2)

    def test_memory_stays_bounded(self):
        memory = SpecFailureMemory(scope="s", limit=6)

        for index in range(50):
            memory.record(
                "contract/semantic",
                f"concern {index}"
            )

        self.assertEqual(len(memory.entries), 6)

    def test_entries_stay_length_bounded(self):
        memory = SpecFailureMemory(scope="s", max_chars=120)

        memory.record("contract/semantic", "word " * 4000)

        self.assertLess(
            len(memory.lines()[0]),
            120 + 64
        )

    def test_evidence_survives_a_flood_of_opinions(self):
        """
        A plain tail-truncation would let reviewer noise evict the
        machine-verified finding that actually explains the failure.
        """

        memory = SpecFailureMemory(scope="s", limit=4)

        memory.record("contract/compilation", DETERMINISTIC_ISSUE)
        memory.record("contract/challenge_confirmed", CHALLENGE_ISSUE)

        for index in range(30):
            memory.record(
                "contract/semantic",
                f"opinion {index}"
            )

        bodies = memory.lines()

        self.assertEqual(len(bodies), 4)

        self.assertTrue(
            any(DETERMINISTIC_ISSUE in line for line in bodies)
        )
        self.assertTrue(
            any(CHALLENGE_ISSUE in line for line in bodies)
        )

    def test_transient_entries_are_evicted_first(self):
        memory = SpecFailureMemory(scope="s", limit=2)

        memory.record("model", "timeout a")
        memory.record("model", "timeout b")
        memory.record("contract/compilation", DETERMINISTIC_ISSUE)

        self.assertTrue(
            any(
                entry["provenance"] == DETERMINISTIC_CONFIRMED
                for entry in memory.entries
            )
        )


class PersistenceCompatibilityTests(unittest.TestCase):
    def test_structured_entries_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"

            memory = SpecFailureMemory(scope="s", path=path)
            memory.record("contract/compilation", DETERMINISTIC_ISSUE)
            memory.record("contract/semantic", REVIEWER_ISSUE)
            memory.save()

            reloaded = SpecFailureMemory.load(path, "s")

            self.assertEqual(
                [
                    entry["provenance"]
                    for entry in reloaded.entries
                ],
                [DETERMINISTIC_CONFIRMED, REVIEWER_CONCERN]
            )

    def test_legacy_string_entries_are_still_readable(self):
        """
        A memory file written by an earlier harness version holds flat
        "[category] body" strings. Upgrading must not lose them.
        """

        import json

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"

            path.write_text(
                json.dumps(
                    {
                        "scope": "s",
                        "entries": [
                            f"[contract/semantic] {REVIEWER_ISSUE}",
                            f"[contract/compilation] {DETERMINISTIC_ISSUE}",
                        ]
                    }
                )
            )

            memory = SpecFailureMemory.load(path, "s")

            self.assertEqual(len(memory.entries), 2)

            self.assertEqual(
                memory.entries[0]["provenance"],
                REVIEWER_CONCERN
            )
            self.assertEqual(
                memory.entries[1]["provenance"],
                DETERMINISTIC_CONFIRMED
            )

            self.assertIn(
                "UNCONFIRMED REVIEWER CONCERNS",
                memory.as_text()
            )

    def test_malformed_entries_are_dropped_not_fatal(self):
        self.assertIsNone(normalize_entry(""))
        self.assertIsNone(normalize_entry(None))
        self.assertIsNone(normalize_entry({"body": "   "}))


class RecordingApiTests(unittest.TestCase):
    def test_explicit_provenance_overrides_the_category_default(self):
        memory = SpecFailureMemory(scope="s")

        record_spec_failure(
            {"spec_memory": memory},
            "something/new",
            "a finding",
            DETERMINISTIC_CONFIRMED
        )

        self.assertEqual(
            memory.entries[0]["provenance"],
            DETERMINISTIC_CONFIRMED
        )

    def test_recording_without_memory_is_a_no_op(self):
        self.assertFalse(
            record_spec_failure({}, "contract/semantic", "x")
        )


if __name__ == "__main__":
    unittest.main()
