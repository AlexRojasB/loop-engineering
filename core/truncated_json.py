"""
Structural completion of a JSON document that was cut off mid-generation.

This exists for one observed failure. In the 16K context evaluation
(results/context-20260902-112156, case 9) the semantic reviewer produced a
complete, correct audit -- it identified that requirement 3 had no test --
and the harness recorded `call_failed` and threw the verdict away. The
response body carried no `done_reason` at all, so the truncation guard in
core/models.py never fired, and `json.loads` raised on the missing final
brace.

What this module does is deliberately the least it can do: it closes
brackets and quotes that generation never got to close, after trimming any
dangling fragment back to the last position where the document was
structurally complete. It moves no content, adds no keys and invents no
values. Every byte in the result was emitted by the model.

What it does NOT do is decide whether the recovered document may be
trusted. Truncation removes content, and removing audit content can only
ever make a contract look CLEANER than the model found it -- the damning
requirement may be exactly the one that got cut. So a caller must never
derive an approval from a completed document. See
`_recover_verdict_from_incomplete` in core/phases/test_contract_phase.py,
which accepts a recovered REJECT and refuses a recovered APPROVE.

Nothing here is language- or schema-aware; it is plain JSON repair.
"""

import json


CLOSERS = {
    "{": "}",
    "[": "]"
}

# How many structural boundaries to walk back from the end. A reviewer
# response is a few thousand characters, and the cut is at the tail, so
# the first candidate almost always wins. The bound only stops a
# pathological input from turning into a long parse loop.
MAX_CANDIDATES = 400


def completion_candidates(text):
    """
    Every position at which `text` could be closed off cleanly, as
    (end_index_exclusive, unclosed_stack).

    A position qualifies when a value has just finished: after a closing
    brace or bracket, or after the quote that ends a string. Cutting
    anywhere else would leave a half-written key, number or literal that
    no amount of bracket-closing can rescue.
    """

    stack = []
    candidates = []
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False

            elif char == "\\":
                escaped = True

            elif char == '"':
                in_string = False
                candidates.append(
                    (index + 1, tuple(stack))
                )

            continue

        if char == '"':
            in_string = True

        elif char in CLOSERS:
            stack.append(char)

        elif char in ("}", "]"):
            if not stack:
                # More closers than openers: this is not a truncated
                # document, it is a corrupt one. Stop here.
                break

            stack.pop()
            candidates.append(
                (index + 1, tuple(stack))
            )

    return candidates


def complete_truncated_json(text):
    """
    The JSON object `text` was in the middle of emitting, or None.

    Call this only after `json.loads` has already failed: a document that
    parses on its own is not truncated and must be used as it is.

    Returns None -- meaning "no opinion", never "invalid" -- when the text
    is not a dict, closes nothing, or cannot be completed at any
    boundary. None always leaves the caller on its existing failure path.
    """

    if not isinstance(text, str) or not text.strip():
        return None

    candidates = completion_candidates(text)

    for end, stack in reversed(
        candidates[-MAX_CANDIDATES:]
    ):
        if not stack:
            # Already balanced at this point, so whatever follows is
            # trailing garbage rather than an unfinished structure.
            continue

        head = text[:end].rstrip().rstrip(",")

        candidate = head + "".join(
            CLOSERS[char]
            for char in reversed(stack)
        )

        try:
            value = json.loads(candidate)

        except (json.JSONDecodeError, ValueError):
            continue

        if isinstance(value, dict):
            return value

    return None
