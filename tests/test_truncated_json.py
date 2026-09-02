"""
Structural completion of a reviewer response cut off mid-generation.

The bytes in test_recovers_the_real_case_9_response are a verbatim excerpt
of what qwen3.5:9b returned in the 16K context evaluation
(tests/reviewer_model_eval/results/context-20260902-112156, case 9),
where the harness recorded `call_failed` and discarded a correct
rejection.
"""

import json
import unittest

from core.truncated_json import (
    complete_truncated_json,
    completion_candidates,
)


class CompleteTruncatedJsonTest(unittest.TestCase):
    def test_closes_a_single_missing_brace(self):
        text = '{"audit": {"requirements": [{"id": "1"}]}'

        self.assertEqual(
            complete_truncated_json(text),
            {"audit": {"requirements": [{"id": "1"}]}}
        )

    def test_closes_nested_arrays_and_objects(self):
        text = '{"a": {"b": [{"c": 1}, {"d": 2}'

        self.assertEqual(
            complete_truncated_json(text),
            {"a": {"b": [{"c": 1}, {"d": 2}]}}
        )

    def test_keeps_the_complete_fields_of_a_cut_off_entry(self):
        # Generation stopped inside the second object's `evidence`
        # string. Completion walks back to the last boundary that closes
        # cleanly, which keeps the `id` the model did finish writing and
        # drops only the half-written field.
        #
        # This is deliberately not "discard the whole entry": every
        # retained byte was emitted by the model, and for an audit the
        # schema gate is what decides whether a stripped-down entry is
        # still usable -- a requirement entry that lost its `evidence`
        # or its boolean `covered` fails validate_audit_schema, so the
        # recovery in _recover_verdict_from_incomplete returns None.
        text = '{"items": [{"id": "1"}, {"id": "2", "evidence": "half'

        self.assertEqual(
            complete_truncated_json(text),
            {"items": [{"id": "1"}, {"id": "2"}]}
        )

    def test_drops_a_dangling_key_with_no_value(self):
        text = '{"items": [{"id": "1"}], "decision":'

        self.assertEqual(
            complete_truncated_json(text),
            {"items": [{"id": "1"}]}
        )

    def test_preserves_escaped_quotes_inside_strings(self):
        text = '{"evidence": "he said \\"no\\" clearly"'

        self.assertEqual(
            complete_truncated_json(text),
            {"evidence": 'he said "no" clearly'}
        )

    def test_does_not_treat_a_brace_in_a_string_as_structure(self):
        text = '{"evidence": "a } and a ] in prose"'

        self.assertEqual(
            complete_truncated_json(text),
            {"evidence": "a } and a ] in prose"}
        )

    def test_returns_none_for_a_non_object_document(self):
        self.assertIsNone(
            complete_truncated_json('[1, 2, 3')
        )

    def test_returns_none_for_empty_or_non_string_input(self):
        for value in ("", "   ", None, 17, {"a": 1}):
            self.assertIsNone(
                complete_truncated_json(value)
            )

    def test_returns_none_when_nothing_needs_closing(self):
        # Already balanced: not a truncation, so this module has no
        # opinion and the caller keeps using its own parse.
        self.assertIsNone(
            complete_truncated_json('{"a": 1} trailing garbage')
        )

    def test_returns_none_when_unrecoverable(self):
        self.assertIsNone(
            complete_truncated_json('{"a": ')
        )

    def test_never_invents_keys(self):
        # `covered` was still being written when generation stopped, so
        # it must NOT appear in the result. Nothing may be filled in.
        text = '{"audit": {"requirements": [{"id": "1", "covered": true'

        recovered = complete_truncated_json(text)

        self.assertEqual(
            list(recovered.keys()),
            ["audit"]
        )
        self.assertEqual(
            list(recovered["audit"].keys()),
            ["requirements"]
        )
        self.assertEqual(
            recovered["audit"]["requirements"],
            [{"id": "1"}]
        )
        self.assertNotIn(
            "covered",
            recovered["audit"]["requirements"][0]
        )

    def test_recovers_the_real_case_9_response(self):
        # Verbatim tail shape of the 16K case-9 response: a complete
        # audit, a stated REJECT, and no final closing brace.
        text = (
            '{"audit":{"requirements":['
            '{"id":"1","covered":true,"evidence":"deposit returns true"}'
            '],"contradictions":[],'
            '"decision":"REJECT",'
            '"issues":["Requirement 3 is not covered - no test verifies '
            'Deposit returns false when the account does not exist"]}'
        )

        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)

        recovered = complete_truncated_json(text)

        self.assertEqual(
            recovered["audit"]["decision"],
            "REJECT"
        )
        self.assertIn(
            "Requirement 3 is not covered",
            recovered["audit"]["issues"][0]
        )


class CompletionCandidatesTest(unittest.TestCase):
    def test_stops_at_an_unbalanced_closer(self):
        # More closers than openers is corruption, not truncation.
        candidates = completion_candidates('{"a": 1}}{"b"')

        self.assertTrue(
            all(
                end <= len('{"a": 1}}')
                for end, _ in candidates
            )
        )

    def test_records_the_stack_still_open_at_each_boundary(self):
        candidates = completion_candidates('{"a": [{"b": 1}')

        self.assertEqual(
            candidates[-1][1],
            ("{", "[")
        )


if __name__ == "__main__":
    unittest.main()
