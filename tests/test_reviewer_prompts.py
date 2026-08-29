"""
Deterministic regression coverage for the Test Contract reviewer prompts.

These tests do not call a model. They pin two things:

1. The prompt files actually contain the new guidance required to catch
   the two Inventory-benchmark failure classes (numeric-invariant
   contradictions, fresh-instance contradictory setup).
2. The prompts render cleanly (via the real render_prompt/str.format
   pipeline) against fixture production/test code that contains C#
   braces, without leaking Inventory-specific language.

Whether a given local model actually *acts* on this guidance is not
something a deterministic test can prove — see
tests/manual_eval_reviewer_prompts.py for that.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.authorized_future import (  # noqa: E402
    NO_AUTHORIZED_FUTURE,
    format_authorized_future,
)
from core.prompts import load_prompt, render_prompt  # noqa: E402
from core.spec_memory import NO_MEMORY_TEXT  # noqa: E402

from tests.fixtures.toy_domains import (  # noqa: E402
    LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION,
    LEDGER_PRODUCTION,
    LEDGER_TASK,
    WIDGET_BAD_SNIPPET_FRESH_INSTANCE_GUARD,
    WIDGET_PRODUCTION,
    WIDGET_TASK,
)

INVENTORY_SPECIFIC_TERMS = (
    "inventory",
    "sku",
    "reservation",
    "reserve(",
    "reservedquantity",
    "availablequantity",
)


class SemanticReviewerPromptTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_prompt("test-semantic-reviewer.md")

    def test_covers_numeric_mutation_operators(self):
        for token in ("+=", "-="):
            self.assertIn(
                token,
                self.raw,
                f"expected semantic reviewer prompt to mention {token!r}"
            )

        lowered = self.raw.lower()

        for token in ("increment", "decrement"):
            self.assertIn(
                token,
                lowered,
                f"expected semantic reviewer prompt to mention {token!r}"
            )

    def test_covers_quantitative_domain_vocabulary(self):
        lowered = self.raw.lower()

        for token in (
            "counter",
            "balance",
            "quantit",
            "collection size"
        ):
            self.assertIn(
                token,
                lowered,
                f"expected semantic reviewer prompt to mention {token!r}"
            )

    def test_has_no_inventory_specific_language(self):
        lowered = self.raw.lower()

        for term in INVENTORY_SPECIFIC_TERMS:
            self.assertNotIn(
                term,
                lowered,
                f"semantic reviewer prompt should stay domain-generic, "
                f"found {term!r}"
            )

    def test_renders_against_generic_ledger_fixture(self):
        rendered = render_prompt(
            "test-semantic-reviewer.md",
            task=LEDGER_TASK,
            production=LEDGER_PRODUCTION,
            tests=LEDGER_BAD_SNIPPET_QUANTITATIVE_CONTRADICTION,
            prior_issues="(none raised yet in this Test Contract run)",
            prior_spec_failures=NO_MEMORY_TEXT,
            authorized_future_contract=NO_AUTHORIZED_FUTURE
        )

        self.assertIn(LEDGER_TASK, rendered)
        self.assertIn("Withdraw_DoesNotChangeBalance_WhenSuccessful", rendered)
        self.assertIn("balance -= amount", rendered)

    def test_covers_object_identity_and_provenance_guidance(self):
        lowered = self.raw.lower()

        for token in (
            "identity and provenance",
            "owns the authoritative state",
            "registered",
        ):
            self.assertIn(
                token,
                lowered,
                f"expected semantic reviewer prompt to mention {token!r}"
            )

    def test_has_generic_identity_provenance_worked_example(self):
        # Invalid: object constructed directly, never registered with
        # its owning component.
        self.assertIn("new Book(", self.raw)
        self.assertIn("library.CheckOut(book.Id", self.raw)
        self.assertIn("book.CheckedOutCount", self.raw)

        # Valid: object created/retrieved through the owning component.
        self.assertIn("library.AddBook(", self.raw)
        self.assertIn("library.FindByCode(", self.raw)

    def test_covers_root_cause_vs_surface_syntax_guidance(self):
        lowered = self.raw.lower()

        self.assertIn("root cause", lowered)
        self.assertIn("surface", lowered)


class StructuralReviewerPromptTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_prompt("test-reviewer.md")

    def test_covers_fresh_instance_guard_language(self):
        lowered = self.raw.lower()

        for token in (
            "fresh instance",
            "predetermined",
            "brand-new instance",
            "construct"
        ):
            self.assertIn(
                token,
                lowered,
                f"expected structural reviewer prompt to mention {token!r}"
            )

    def test_has_no_inventory_specific_language(self):
        lowered = self.raw.lower()

        for term in INVENTORY_SPECIFIC_TERMS:
            self.assertNotIn(
                term,
                lowered,
                f"structural reviewer prompt should stay domain-generic, "
                f"found {term!r}"
            )

    def test_renders_against_generic_widget_fixture(self):
        rendered = render_prompt(
            "test-reviewer.md",
            task=WIDGET_TASK,
            production=WIDGET_PRODUCTION,
            tests=WIDGET_BAD_SNIPPET_FRESH_INSTANCE_GUARD,
            prior_issues="(none raised yet in this Test Contract run)",
            prior_spec_failures=NO_MEMORY_TEXT,
            authorized_future_contract=NO_AUTHORIZED_FUTURE
        )

        self.assertIn(WIDGET_TASK, rendered)
        self.assertIn("Register_RejectsWhenCodeAlreadyExists", rendered)
        self.assertIn("public bool Register(string code, int quantity)", rendered)

    def test_covers_object_identity_and_provenance_guidance(self):
        lowered = self.raw.lower()

        for token in (
            "identity and provenance",
            "owns the authoritative state",
            "registered",
        ):
            self.assertIn(
                token,
                lowered,
                f"expected structural reviewer prompt to mention {token!r}"
            )

    def test_has_generic_identity_provenance_worked_example(self):
        # Invalid: object constructed directly, never registered with
        # its owning component.
        self.assertIn("new Book(", self.raw)
        self.assertIn("library.CheckOut(book.Id", self.raw)
        self.assertIn("book.CheckedOutCount", self.raw)

        # Valid: object created/retrieved through the owning component.
        self.assertIn("library.AddBook(", self.raw)
        self.assertIn("library.FindByCode(", self.raw)

    def test_covers_root_cause_vs_surface_syntax_guidance(self):
        lowered = self.raw.lower()

        self.assertIn("root cause", lowered)
        self.assertIn("surface", lowered)

    def test_renders_against_generic_identity_provenance_fixture(self):
        # The identity/provenance section itself must survive the
        # real str.format() render pipeline together with fixture
        # content, same as the other sections.
        rendered = render_prompt(
            "test-reviewer.md",
            task=WIDGET_TASK,
            production=WIDGET_PRODUCTION,
            tests=WIDGET_BAD_SNIPPET_FRESH_INSTANCE_GUARD,
            prior_issues="(none raised yet in this Test Contract run)",
            prior_spec_failures=NO_MEMORY_TEXT,
            authorized_future_contract=NO_AUTHORIZED_FUTURE
        )

        self.assertIn("Object Identity And Provenance", rendered)
        self.assertIn("new Book(", rendered)


if __name__ == "__main__":
    unittest.main()
