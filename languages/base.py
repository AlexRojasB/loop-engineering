import shlex
from abc import ABC, abstractmethod


class LanguageAdapter(ABC):
    name = "generic"

    @abstractmethod
    def can_handle(self, files):
        raise NotImplementedError

    @abstractmethod
    def build_command(self, workspace_files):
        raise NotImplementedError

    @abstractmethod
    def test_command(self, workspace_files):
        raise NotImplementedError

    def build_argv(self, workspace_files):
        """
        Structured argv equivalent of build_command(), for callers that
        must invoke the build without a shell. Generic fallback:
        tokenize the command string this adapter already produces
        internally (safe here since it is built from known repository
        paths, not external input).
        """
        return shlex.split(
            self.build_command(workspace_files)
        )

    def test_argv(self, workspace_files, filter=None):
        """
        Structured argv equivalent of test_command(), optionally scoped
        to a single test filter expression. The filter, when supported,
        must be appended as its own argv element and never interpolated
        into a command string.

        Returns None when a filter is requested but this adapter has no
        structured filter support, so callers can reject the request
        instead of silently ignoring the filter.
        """
        if filter:
            return None

        return shlex.split(
            self.test_command(workspace_files)
        )

    def classify_red_state(self, output):
        return None

    def classify_contract_diagnostics(
        self,
        output,
        spec_text=""
    ):
        """
        Optional adapter hook: classify build/test diagnostics into
        contract-level categories so the harness can tell a legitimate
        test-first RED apart from a broken test contract.

        Implementations return a dict:

            {
                "supported": True,
                "verdict": "VALID" | "INVALID" | "UNKNOWN",
                "expected_red": [diagnostic, ...],
                "invalid": [diagnostic, ...],
                "broken_syntax": [diagnostic, ...],
                "unclassified": [diagnostic, ...],
                "issues": ["human readable defect", ...],
                "compiles_clean": bool
            }

        where each diagnostic is
        {"code", "symbol", "message", "category", "reason"}.

        `spec_text` is the CURRENT authoritative specification. A missing
        symbol is only EXPECTED RED when that spec actually asked for it.

        Returning None means "this language cannot classify diagnostics
        yet"; callers must then fall back to their previous behaviour
        rather than assuming anything.
        """
        return None

    def analyze_test_source(self, source, path=None):
        """
        Optional adapter hook: find defects INTRINSIC to generated test
        code, without compiling anything.

        This exists because a compiler cannot be relied on to reveal
        them. Test-first contracts are compiled while the requested
        future API is still missing, and every mainstream compiler
        suppresses cascading diagnostics inside an expression whose type
        could not be resolved. A defect sitting inside such an
        expression is therefore INVISIBLE at gate time and only appears
        later, once production implements the future API -- i.e. during
        implementation, against a contract that is already frozen.

        Implementations return a list of dicts:

            {
                "code": "<adapter diagnostic id>",
                "message": "<what is wrong>",
                "line": <1-based line number or None>,
                "reason": "<why this is intrinsic to the test code>"
            }

        Only report defects that are wrong REGARDLESS of what production
        code exists, so a legitimate test-first reference to a future API
        is never flagged. Returning [] means "nothing intrinsic found",
        which is also the correct answer for an adapter that cannot do
        this analysis.
        """
        return []

    def parse_diagnostic_locations(self, output):
        """
        Optional adapter hook: extract file-attributed diagnostics from
        build/test output.

        Implementations return a list of dicts:

            {"path": "<repo-relative or absolute>", "line": int|None,
             "code": str, "message": str}

        Used to decide whether a compile failure is attributable
        exclusively to frozen test files. Returning [] means "cannot
        attribute", which callers must treat as insufficient evidence.
        """
        return []

    def extract_failure_paths(self, output):
        return []

    def is_test_path(self, path):
        lower = path.lower()

        return (
            "test" in lower
            or "spec" in lower
        )

    def is_config_path(self, path):
        return False

    def describe(self):
        return {
            "name": self.name
        }
