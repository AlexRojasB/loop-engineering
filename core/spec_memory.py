"""
Bounded cross-attempt failure memory, scoped to ONE work item.

The multi-spec runner restores the repository between outer SPEC
ATTEMPTs, which is correct and must stay. The cost is amnesia: attempt N+1
re-derives the same defective contract attempt N already disproved.

This module keeps a small, condensed record of *what went wrong* for the
current work item only:

- scoped by (source path + hash of source content), so a different work
  item — or an edited one — can never inherit it;
- bounded in both entry count and per-entry length, so prompts never grow
  with the number of attempts;
- stored OUTSIDE the target repository, so `git restore` semantics,
  clean-baseline checks and automatic commits are all unaffected.
"""

import hashlib
import json
import re
from pathlib import Path


DEFAULT_LIMIT = 12

DEFAULT_MAX_CHARS = 240

WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------
# Provenance
#
# Ledger Full #3 showed why a flat list of "findings" is actively
# harmful. Spec 005's reviewers wrongly rejected a task-authorized future
# API; those false positives were written into memory as findings, fed
# back into the next attempt's prompts, and read there as established
# fact -- so the next reviewers repeated them almost verbatim. Memory
# amplified an error instead of preventing one.
#
# The fix is not less memory, it is honest memory: every entry records
# HOW it was established, and prompts present machine-verified evidence
# and independently adjudicated defects differently from one model's
# unconfirmed opinion.
# ---------------------------------------------------------------------

DETERMINISTIC_CONFIRMED = "deterministic_confirmed"

CHALLENGE_CONFIRMED = "challenge_confirmed"

SEMANTIC_CONFIRMATION_REJECTION = "semantic_confirmation_rejection"

REVIEWER_CONCERN = "reviewer_concern"

IMPLEMENTATION_OBSERVATION = "implementation_observation"

TRANSIENT_MODEL_FAILURE = "transient_model_failure"


PROVENANCE_VALUES = (
    DETERMINISTIC_CONFIRMED,
    CHALLENGE_CONFIRMED,
    SEMANTIC_CONFIRMATION_REJECTION,
    REVIEWER_CONCERN,
    IMPLEMENTATION_OBSERVATION,
    TRANSIENT_MODEL_FAILURE,
)


# Higher wins when memory has to be trimmed: a machine-verified finding
# must never be evicted by a flood of model opinions.
PROVENANCE_AUTHORITY = {
    DETERMINISTIC_CONFIRMED: 5,
    CHALLENGE_CONFIRMED: 5,
    SEMANTIC_CONFIRMATION_REJECTION: 3,
    REVIEWER_CONCERN: 2,
    IMPLEMENTATION_OBSERVATION: 2,
    TRANSIENT_MODEL_FAILURE: 0,
}


# Category prefix -> provenance. Categories are written by the phases as
# "contract/<reviewer>" or a bare label; anything unrecognised is treated
# as an unconfirmed observation rather than as evidence.
CATEGORY_PROVENANCE = {
    "contract/compilation": DETERMINISTIC_CONFIRMED,
    "contract/source": DETERMINISTIC_CONFIRMED,
    "contract/challenge_confirmed": CHALLENGE_CONFIRMED,
    "contract/challenge_review": CHALLENGE_CONFIRMED,
    "contract/challenge": CHALLENGE_CONFIRMED,
    "contract/semantic_confirmation": SEMANTIC_CONFIRMATION_REJECTION,
    "contract/structural": REVIEWER_CONCERN,
    "contract/semantic": REVIEWER_CONCERN,
    "contract/guard": DETERMINISTIC_CONFIRMED,
    "implementation": IMPLEMENTATION_OBSERVATION,
    "model": TRANSIENT_MODEL_FAILURE,
}


def provenance_for_category(category):
    """
    Best-effort provenance for a category label. Unknown categories fall
    back to the WEAKEST interpretation that still carries signal, so a
    new caller can never accidentally promote an opinion to evidence.
    """

    key = str(category or "").strip()

    if key in CATEGORY_PROVENANCE:
        return CATEGORY_PROVENANCE[key]

    return IMPLEMENTATION_OBSERVATION


AUTHORITATIVE_PROVENANCE = {
    DETERMINISTIC_CONFIRMED,
    CHALLENGE_CONFIRMED,
}

HYPOTHESIS_PROVENANCE = {
    REVIEWER_CONCERN,
    SEMANTIC_CONFIRMATION_REJECTION,
}


ENTRY_PATTERN = re.compile(
    r"^\[(?P<category>[^\]]*)\]\s*(?P<body>.*)$"
)


def spec_scope_key(source_path, source_text):
    """
    Identity of one work item. Content-hashed so an edited specification
    starts with a clean memory rather than inheriting stale findings.
    """

    digest = hashlib.sha256(
        (source_text or "").encode(
            "utf-8",
            "replace"
        )
    ).hexdigest()[:16]

    return f"{source_path or '<unknown>'}@{digest}"


def condense(value, max_chars=DEFAULT_MAX_CHARS):
    """
    Collapse an arbitrary model response / issue object into one short
    single-line finding. Prevents huge model output from ever reaching a
    later prompt.
    """

    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(
                value,
                sort_keys=True
            )
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)

    text = WHITESPACE.sub(
        " ",
        text
    ).strip()

    if len(text) <= max_chars:
        return text

    return text[:max_chars - 3].rstrip() + "..."


def make_entry(category, body, provenance=None):
    return {
        "provenance":
            provenance
            or provenance_for_category(
                category
            ),
        "category": str(category or ""),
        "body": str(body or "")
    }


def normalize_entry(entry):
    """
    Accept both the current structured form and the flat
    "[category] body" strings written by earlier harness versions, so an
    existing memory file keeps working after an upgrade.
    """

    if isinstance(entry, dict):
        body = str(
            entry.get("body", "")
        ).strip()

        if not body:
            return None

        category = str(
            entry.get("category", "")
        )

        provenance = entry.get(
            "provenance"
        )

        if provenance not in PROVENANCE_VALUES:
            provenance = provenance_for_category(
                category
            )

        return {
            "provenance": provenance,
            "category": category,
            "body": body
        }

    text = str(entry or "").strip()

    if not text:
        return None

    match = ENTRY_PATTERN.match(text)

    if match:
        return make_entry(
            match.group("category"),
            match.group("body").strip()
        )

    return make_entry(
        "",
        text,
        IMPLEMENTATION_OBSERVATION
    )


def entry_key(entry):
    return (
        entry["category"],
        entry["body"]
    )


def entry_line(entry):
    if entry["category"]:
        return f"[{entry['category']}] {entry['body']}"

    return entry["body"]


class SpecFailureMemory:
    """
    Append-only, de-duplicated, bounded findings for one work item.
    """

    def __init__(
        self,
        scope,
        entries=None,
        path=None,
        limit=DEFAULT_LIMIT,
        max_chars=DEFAULT_MAX_CHARS
    ):
        self.scope = scope
        self.path = str(path) if path else None
        self.limit = limit
        self.max_chars = max_chars
        self.entries = list(entries or [])

    # -- persistence ---------------------------------------------------

    @classmethod
    def load(
        cls,
        path,
        scope,
        limit=DEFAULT_LIMIT,
        max_chars=DEFAULT_MAX_CHARS
    ):
        """
        Load memory for `scope`. A stored scope that does not match is
        discarded rather than reused: memory never crosses work items.
        """

        entries = []

        if path:
            try:
                stored = json.loads(
                    Path(path).read_text()
                )

                if stored.get("scope") == scope:
                    entries = [
                        normalize_entry(entry)
                        for entry in stored.get(
                            "entries",
                            []
                        )
                    ]

                    entries = [
                        entry
                        for entry in entries
                        if entry
                    ]

            except (
                OSError,
                ValueError,
                AttributeError
            ):
                entries = []

        return cls(
            scope=scope,
            entries=entries,
            path=path,
            limit=limit,
            max_chars=max_chars
        )

    def save(self):
        if not self.path:
            return

        target = Path(self.path)

        try:
            target.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            target.write_text(
                json.dumps(
                    {
                        "scope": self.scope,
                        "entries": self.entries
                    },
                    indent=2
                )
            )

        except OSError:
            # Memory is an optimisation, never a correctness
            # requirement. A read-only runtime directory must not fail
            # the run.
            pass

    # -- content -------------------------------------------------------

    @property
    def is_empty(self):
        return not self.entries

    def record(
        self,
        category,
        details,
        provenance=None
    ):
        """
        Record one or more condensed findings under `category`.

        `provenance` says HOW the finding was established. Callers that
        omit it get the provenance implied by the category, which is
        deliberately the weakest reading for anything unrecognised.
        """

        if details is None:
            items = []

        elif isinstance(details, (str, bytes)):
            items = [details]

        elif isinstance(details, dict):
            items = [details]

        else:
            items = list(details)

        added = False

        existing = {
            entry_key(entry)
            for entry in self.entries
        }

        for item in items:
            body = condense(
                item,
                self.max_chars
            )

            if not body:
                continue

            entry = make_entry(
                category,
                body,
                provenance
            )

            key = entry_key(entry)

            if key in existing:
                continue

            existing.add(key)

            self.entries.append(entry)

            added = True

        self._trim()

        return added

    def _trim(self):
        """
        Bound the memory, evicting the LEAST authoritative entries first.

        A plain tail-truncation would let a burst of reviewer opinions
        push out the machine-verified finding that actually explains the
        failure -- which is the opposite of what memory is for.
        """

        if len(self.entries) <= self.limit:
            return

        ordered = sorted(
            enumerate(self.entries),
            key=lambda pair: (
                PROVENANCE_AUTHORITY.get(
                    pair[1]["provenance"],
                    1
                ),
                pair[0]
            ),
            reverse=True
        )

        keep = sorted(
            index
            for index, _ in ordered[:self.limit]
        )

        self.entries = [
            self.entries[index]
            for index in keep
        ]

    def clear(self):
        self.entries = []

        if self.path:
            try:
                Path(self.path).unlink()
            except OSError:
                pass

    def lines(self):
        """
        Flat "[category] body" rendering of every entry, in order.

        Useful for assertions and logs that only care about content;
        `as_text()` is what prompts get, because it also carries the
        authority of each finding.
        """

        return [
            entry_line(entry)
            for entry in self.entries
        ]

    def entries_with_provenance(self, provenance):
        return [
            entry
            for entry in self.entries
            if entry["provenance"] == provenance
        ]

    def as_text(self):
        """
        Render memory for a prompt, GROUPED BY HOW EACH FINDING WAS
        ESTABLISHED.

        A flat list invites a model to treat every line as settled fact.
        In Ledger Full #3 that turned a reviewer's false positive into a
        premise the next attempt's reviewers repeated. Machine-verified
        evidence and independently adjudicated defects are therefore
        stated as established; a single unconfirmed model opinion is
        stated as a hypothesis to re-check.
        """

        if not self.entries:
            return NO_MEMORY_TEXT

        confirmed = [
            entry
            for entry in self.entries
            if entry["provenance"]
            in AUTHORITATIVE_PROVENANCE
        ]

        hypotheses = [
            entry
            for entry in self.entries
            if entry["provenance"]
            in HYPOTHESIS_PROVENANCE
        ]

        observations = [
            entry
            for entry in self.entries
            if entry["provenance"]
            == IMPLEMENTATION_OBSERVATION
        ]

        # Transient model/infrastructure failures are kept in the stored
        # memory for observability but say nothing about the contract,
        # so they are never rendered into a prompt.

        blocks = []

        if confirmed:
            blocks.append(
                CONFIRMED_HEADING
                + "\n"
                + "\n".join(
                    f"- {entry_line(entry)}"
                    for entry in confirmed
                )
            )

        if hypotheses:
            blocks.append(
                HYPOTHESIS_HEADING
                + "\n"
                + "\n".join(
                    f"- {entry_line(entry)}"
                    for entry in hypotheses
                )
            )

        if observations:
            blocks.append(
                OBSERVATION_HEADING
                + "\n"
                + "\n".join(
                    f"- {entry_line(entry)}"
                    for entry in observations
                )
            )

        if not blocks:
            return NO_MEMORY_TEXT

        return "\n\n".join(blocks)


NO_MEMORY_TEXT = (
    "(no findings from previous attempts at this work item)"
)

CONFIRMED_HEADING = (
    "CONFIRMED EVIDENCE (machine-verified by a deterministic check, or "
    "independently adjudicated). These describe real defects of earlier "
    "attempts; do not reproduce them:"
)

HYPOTHESIS_HEADING = (
    "UNCONFIRMED REVIEWER CONCERNS (one model's opinion from an earlier "
    "attempt, never independently confirmed, and known to be wrong "
    "sometimes -- reviewers have previously 'found' defects that were "
    "actually task-authorized future API). Treat each as a HYPOTHESIS "
    "to re-test from scratch against the material in front of you. Do "
    "NOT restate one as a finding, and do NOT reject anything solely "
    "because it appears here:"
)

OBSERVATION_HEADING = (
    "OBSERVATIONS FROM EARLIER ATTEMPTS (context only, not evidence of "
    "any specific defect):"
)


# ---------------------------------------------------------------------
# Config-level helpers
#
# Phases stay decoupled from storage: they only ever ask the config.
# ---------------------------------------------------------------------

def memory_from_config(config):
    return config.get(
        "spec_memory"
    )


def record_spec_failure(
    config,
    category,
    details,
    provenance=None
):
    memory = memory_from_config(
        config
    )

    if memory is None:
        return False

    before = len(memory.entries)

    added = memory.record(
        category,
        details,
        provenance
    )

    if added:
        memory.save()

        try:
            from core.state import append_history

            append_history(
                config,
                "spec_failure_recorded",
                {
                    "category": category,
                    "entries": memory.entries[before:]
                }
            )

        except Exception:
            pass

    return added


def spec_memory_text(config):
    memory = memory_from_config(
        config
    )

    if memory is None:
        return NO_MEMORY_TEXT

    return memory.as_text()
