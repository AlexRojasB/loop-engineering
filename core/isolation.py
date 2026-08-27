"""
Work isolation boundary.

During a queued/multi-spec run the CURRENT authoritative source is the
only specification the agent may consult. Later queued work items exist
for scheduling purposes, but their contents must never enter planner,
test-generation, reviewer or implementation context, and must not be
reachable through the agentic file tools either.

This module owns that boundary as a single, generic value object. It
knows nothing about numbering schemes, directory names, or any
particular benchmark: it is handed a current source and a set of peer
sources and restricts everything that is neither the current source nor
an explicitly declared dependency.

Repository/production files are never restricted. Behaviour delivered by
previously completed work stays visible the way it always was: through
committed repository state.
"""

import re
from pathlib import PurePosixPath


DEPENDENCY_HEADINGS = re.compile(
    r"^\s{0,3}#{1,6}\s*"
    r"(depends[\s\-_]*on"
    r"|dependenc(?:y|ies)"
    r"|required[\s\-_]*context"
    r"|context[\s\-_]*documents?)"
    r"\s*:?\s*$",
    re.IGNORECASE
)

ANY_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+\S"
)

DEPENDENCY_KEYS = re.compile(
    r"^\s*[-*]?\s*"
    r"(depends[\s\-_]*on"
    r"|dependenc(?:y|ies)"
    r"|required[\s\-_]*context"
    r"|context[\s\-_]*documents?)"
    r"\s*:\s*(?P<value>.*)$",
    re.IGNORECASE
)

PATH_TOKEN = re.compile(
    r"[A-Za-z0-9_./\\-]+"
)

DOCUMENT_SUFFIXES = (
    ".md",
    ".txt",
    ".rst",
)


def normalize_path(value):
    """
    Normalize a repository-relative path for comparison. Accepts POSIX
    or Windows separators and a leading './'.
    """

    if value is None:
        return None

    text = str(value).strip().replace("\\", "/")

    while text.startswith("./"):
        text = text[2:]

    text = text.strip("/")

    if not text:
        return None

    return str(
        PurePosixPath(text)
    )


def looks_like_document_path(token):
    lowered = token.lower()

    return lowered.endswith(
        DOCUMENT_SUFFIXES
    )


def extract_declared_dependencies(source_text):
    """
    Explicit dependency mechanism.

    A work item may declare that it legitimately needs another document
    by either:

        depends_on: docs/architecture.md, specs/shared/glossary.md

    or a dedicated section:

        ## Depends On

        - specs/shared/glossary.md

    Only document-shaped paths are accepted; anything else is ignored.
    """

    declared = []

    if not source_text:
        return declared

    in_section = False

    for line in source_text.splitlines():
        if DEPENDENCY_HEADINGS.match(line):
            in_section = True
            continue

        if in_section and ANY_HEADING.match(line):
            in_section = False

        key_match = DEPENDENCY_KEYS.match(line)

        candidates = []

        if key_match:
            candidates = PATH_TOKEN.findall(
                key_match.group("value")
            )

        elif in_section:
            candidates = PATH_TOKEN.findall(
                line
            )

        for token in candidates:
            if not looks_like_document_path(
                token
            ):
                continue

            normalized = normalize_path(
                token
            )

            if (
                normalized
                and normalized not in declared
            ):
                declared.append(
                    normalized
                )

    return declared


class WorkIsolation:
    """
    An immutable set of repository-relative paths that are invisible for
    the duration of the current work item.
    """

    def __init__(
        self,
        current_source=None,
        restricted_paths=(),
        dependencies=()
    ):
        self.current_source = normalize_path(
            current_source
        )

        self.dependencies = tuple(
            sorted(
                {
                    normalized
                    for normalized in (
                        normalize_path(path)
                        for path in dependencies
                    )
                    if normalized
                }
            )
        )

        allowed = set(
            self.dependencies
        )

        if self.current_source:
            allowed.add(
                self.current_source
            )

        self.restricted = frozenset(
            normalized
            for normalized in (
                normalize_path(path)
                for path in restricted_paths
            )
            if normalized
            and normalized not in allowed
        )

    @classmethod
    def disabled(cls):
        return cls()

    @property
    def active(self):
        return bool(
            self.restricted
        )

    def is_restricted(self, path):
        """
        True when `path` is a restricted document, or lives beneath a
        restricted directory.
        """

        normalized = normalize_path(
            path
        )

        if not normalized or not self.restricted:
            return False

        if normalized in self.restricted:
            return True

        for restricted in self.restricted:
            if normalized.startswith(
                restricted + "/"
            ):
                return True

        return False

    def allows(self, path):
        return not self.is_restricted(
            path
        )

    def filter_paths(self, paths):
        return [
            path
            for path in paths
            if self.allows(path)
        ]

    def rejection_message(self, path):
        return (
            "READ REJECTED: "
            f"{path} is outside the current work "
            "isolation boundary. Only the current "
            "authoritative specification, its declared "
            "dependencies, and normal repository files "
            "are available. Later queued work items must "
            "not influence this implementation."
        )

    def describe(self):
        if not self.active:
            return "Work isolation: inactive."

        lines = [
            "Work isolation: active."
        ]

        if self.current_source:
            lines.append(
                f"Authoritative source: "
                f"{self.current_source}"
            )

        lines.append(
            f"Restricted sources: "
            f"{len(self.restricted)}"
        )

        for path in sorted(
            self.restricted
        ):
            lines.append(
                f"- {path}"
            )

        for path in self.dependencies:
            lines.append(
                f"- (declared dependency, visible) "
                f"{path}"
            )

        return "\n".join(lines)

    def to_dict(self):
        return {
            "current_source":
                self.current_source,

            "restricted_paths":
                sorted(self.restricted),

            "dependencies":
                list(self.dependencies)
        }

    @classmethod
    def from_dict(cls, data):
        if not data:
            return cls.disabled()

        return cls(
            current_source=data.get(
                "current_source"
            ),
            restricted_paths=data.get(
                "restricted_paths",
                []
            ),
            dependencies=data.get(
                "dependencies",
                []
            )
        )


def build_work_isolation(
    current_source_path,
    peer_source_paths,
    source_text=None
):
    """
    Build the isolation boundary for one work item.

    `peer_source_paths` is every other project source the runner knows
    about — for a queued run, the rest of the queue. Anything the
    current source explicitly declares as a dependency stays visible.
    """

    dependencies = extract_declared_dependencies(
        source_text
    )

    return WorkIsolation(
        current_source=current_source_path,
        restricted_paths=peer_source_paths or (),
        dependencies=dependencies
    )


def sibling_source_paths(
    sources,
    current_source_path
):
    """
    Generic fallback used when no explicit queue is available: treat the
    other project sources that live in the same directory as the current
    one as its peers.
    """

    current = normalize_path(
        current_source_path
    )

    if not current:
        return []

    current_parent = str(
        PurePosixPath(current).parent
    )

    peers = []

    for source in sources:
        path = normalize_path(
            source["path"]
            if isinstance(source, dict)
            else source
        )

        if not path or path == current:
            continue

        if str(
            PurePosixPath(path).parent
        ) == current_parent:
            peers.append(path)

    return peers
