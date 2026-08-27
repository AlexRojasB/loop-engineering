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
                        str(entry)
                        for entry in stored.get(
                            "entries",
                            []
                        )
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

    def record(self, category, details):
        """
        Record one or more condensed findings under `category`.
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

        for item in items:
            body = condense(
                item,
                self.max_chars
            )

            if not body:
                continue

            entry = f"[{category}] {body}"

            if entry in self.entries:
                continue

            self.entries.append(entry)

            added = True

        if len(self.entries) > self.limit:
            self.entries = self.entries[
                -self.limit:
            ]

        return added

    def clear(self):
        self.entries = []

        if self.path:
            try:
                Path(self.path).unlink()
            except OSError:
                pass

    def as_text(self):
        if not self.entries:
            return NO_MEMORY_TEXT

        return "\n".join(
            f"- {entry}"
            for entry in self.entries
        )


NO_MEMORY_TEXT = (
    "(no findings from previous attempts at this work item)"
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


def record_spec_failure(config, category, details):
    memory = memory_from_config(
        config
    )

    if memory is None:
        return False

    before = len(memory.entries)

    added = memory.record(
        category,
        details
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
