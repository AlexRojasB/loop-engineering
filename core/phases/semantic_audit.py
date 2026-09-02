"""
Evidence-first semantic audit.

Why this exists
---------------

An A/B evaluation of two local reviewer models on the real semantic-review
path produced the same pathology in both:

    qwen3.5:9b   15/30 correct, 75% APPROVE rate, 5/15 bad contracts caught
    gemma4:12b   18/30 correct, 90% APPROVE rate, 3/15 bad contracts caught

Always answering APPROVE scores 15/30 on that suite. Both models were at or
barely above that line, and the generation traces showed why: gemma's median
verdict was 8 generated tokens -- the length of

    {"decision":"APPROVE"}

The old reviewer contract let a model reach APPROVE without ever
demonstrating it had looked at the contract. Accuracy was therefore not
measuring judgement; it was measuring the suite's APPROVE ratio.

The fix is not "make the model talk more". It is to require INSPECTABLE
EVIDENCE, validate that evidence deterministically wherever the harness can,
and derive the effective verdict from the audit rather than trusting the
model's own `decision` field.

Three properties this module preserves
--------------------------------------

1. FAIL CLOSED. Every degenerate case -- missing audit, unparseable audit,
   internally inconsistent audit -- yields an invalid verdict the caller
   discards and retries. Nothing here can turn a broken audit into an
   approval.

2. NO FABRICATION. Schema repair may reshape what the model already said.
   It may never invent audit content that was not there, so a bare APPROVE
   cannot be laundered into a passing audit.

3. NO BENCHMARK KNOWLEDGE. The deterministic checks below know about JSON
   shape and numeric literals. They know nothing about Ledger, CloseAccount,
   or any specific fixture.
"""

import re


VALID_DECISIONS = {
    "APPROVE",
    "REJECT"
}

# Each dimension is audited under the same {"applicable": ..., ...} shape.
# One uniform shape rather than five bespoke ones is a deliberate
# concession to small local models: fewer distinct structures to learn
# means fewer schema failures, and schema failures were 24 of gemma's 30
# calls before this redesign.
AUDIT_DIMENSIONS = (
    "setup",
    "identity",
    "transitions",
    "future_api"
)

# An evidence string shorter than this is not evidence -- it is a label.
# Small models will happily emit "ok" or "yes" for every check if allowed.
MIN_EVIDENCE_CHARS = 12


# ---------------------------------------------------------------------
# Generic literal consistency
# ---------------------------------------------------------------------

# C# numeric literals carry a type suffix (100m, 3.5f, 42L, 7u). Strip it
# so the value can be compared. Deliberately NOT a general expression
# parser: only a bare literal is recognised, and anything else is simply
# left unchecked.
_NUMERIC_LITERAL = re.compile(
    r"^[+-]?\d[\d_]*(\.\d+)?([mMdDfFlLuU]{1,2})?$"
)

_COMPARATORS = (
    # Longest / most specific spellings first: ">=" must win over ">".
    ("!=", lambda value, bound: value != bound),
    ("<>", lambda value, bound: value != bound),
    (">=", lambda value, bound: value >= bound),
    ("<=", lambda value, bound: value <= bound),
    ("==", lambda value, bound: value == bound),
    (">", lambda value, bound: value > bound),
    ("<", lambda value, bound: value < bound),
    ("=", lambda value, bound: value == bound),
)

# Prose spellings a model reaches for instead of an operator. Mapped to
# the operator form and then handled by exactly the same code path.
_PROSE_CONDITIONS = (
    ("not zero", "!= 0"),
    ("non-zero", "!= 0"),
    ("nonzero", "!= 0"),
    ("non zero", "!= 0"),
    ("is zero", "== 0"),
    ("equals zero", "== 0"),
    ("must be zero", "== 0"),
    ("positive", "> 0"),
    ("negative", "< 0"),
)


def parse_numeric_literal(value):
    """
    A bare numeric literal as a float, or None when the text is anything
    more interesting than that.

    Returning None is the safe answer everywhere it is used: an
    unrecognised value simply means the deterministic check does not fire.
    """

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    if not isinstance(value, str):
        return None

    text = value.strip()

    if not _NUMERIC_LITERAL.match(text):
        return None

    stripped = text.rstrip("mMdDfFlLuU").replace("_", "")

    try:
        return float(stripped)

    except ValueError:
        return None


def evaluate_condition(observed, condition):
    """
    Evaluate a stated numeric condition against a stated literal.

    Returns True/False when both sides are recognised, and None when
    either is not -- which is the common case and means "no deterministic
    opinion", never "failed".

    This is intentionally the narrowest possible checker: one literal, one
    comparator, one bound. It exists to catch a model that writes down two
    facts which contradict each other in a single arithmetic step, such as
    an initial balance of 100m recorded as failing a `!= 0` requirement.
    It is not, and must not become, a theorem prover.
    """

    left = parse_numeric_literal(observed)

    if left is None or not isinstance(condition, str):
        return None

    text = condition.strip().lower()

    for prose, operator_form in _PROSE_CONDITIONS:
        if prose in text:
            text = operator_form
            break

    for symbol, predicate in _COMPARATORS:
        if symbol not in text:
            continue

        bound = parse_numeric_literal(
            text.split(symbol, 1)[1]
        )

        if bound is None:
            return None

        return predicate(left, bound)

    return None


def literal_inconsistencies(audit):
    """
    Every place the audit states a numeric fact and a verdict about that
    fact which cannot both be true.

    A check that fills in `observed_value` and `required_condition` is
    asserting that `valid` IS the result of that comparison -- the prompt
    says so explicitly. So when the arithmetic disagrees with `valid`, the
    audit contradicts itself and cannot be trusted in either direction.

    Both directions are reported. A model claiming a satisfied condition
    failed (the false-premise trap) and a model claiming a violated
    condition passed (a rubber stamp with numbers attached) are the same
    defect wearing different signs.
    """

    problems = []

    for dimension in AUDIT_DIMENSIONS:
        section = audit.get(dimension)

        if not isinstance(section, dict):
            continue

        for check in section.get("checks") or []:
            if not isinstance(check, dict):
                continue

            observed = check.get("observed_value")
            condition = check.get("required_condition")

            satisfied = evaluate_condition(
                observed,
                condition
            )

            if satisfied is None:
                continue

            claimed = check.get("valid")

            if not isinstance(claimed, bool) or claimed == satisfied:
                continue

            problems.append(
                f"{dimension} check for "
                f"{check.get('target') or 'an unnamed target'} is "
                f"internally inconsistent: it records observed value "
                f"{observed!r} against required condition "
                f"{condition!r}, which evaluates to "
                f"{satisfied}, but reports valid={claimed}."
            )

    return problems


# ---------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------

def _validate_checks(dimension, section):
    """
    Shape of one dimension's check list. Returns a reason or None.
    """

    checks = section.get("checks")

    if not isinstance(checks, list) or not checks:
        return (
            f"'{dimension}' is marked applicable but carries no "
            "checks"
        )

    for check in checks:
        if not isinstance(check, dict):
            return f"'{dimension}' contains a check that is not an object"

        if not str(check.get("target") or "").strip():
            return f"'{dimension}' contains a check with no 'target'"

        if not isinstance(check.get("valid"), bool):
            return (
                f"'{dimension}' check for "
                f"{check.get('target')!r} has no boolean 'valid'"
            )

        evidence = str(check.get("evidence") or "").strip()

        if len(evidence) < MIN_EVIDENCE_CHARS:
            return (
                f"'{dimension}' check for "
                f"{check.get('target')!r} has no substantive "
                "'evidence'"
            )

    return None


def validate_audit_schema(parsed, authorized_symbols=None):
    """
    Confirm the response carries a complete, self-consistent audit.

    Returns None when valid, or a short human-readable reason when not.

    What this rejects, and why each one matters:

    - no `audit` at all -- this is the bare {"decision": "APPROVE"} that
      made the old contract meaningless;
    - an empty `requirements` list -- requirement coverage is the only
      dimension that is never optional, because every contract exists to
      satisfy some requirement;
    - a dimension that is simply absent -- silence must not read as
      "checked and fine", which is the whole point of forcing an explicit
      applicability call;
    - `applicable: true` with no checks, or checks with no evidence --
      the shape of an audit without the substance;
    - `applicable: false` with no reason -- an unjustified opt-out is how
      a model would escape auditing anything at all;
    - a numeric self-contradiction (see literal_inconsistencies);
    - a future symbol the deterministic gate already authorized, marked
      invalid by the model -- the harness compiled the contract and
      matched that symbol against the spec, and that evidence is not the
      model's to overrule.
    """

    if not isinstance(parsed, dict):
        return "response is not a JSON object"

    # `decision` is OPTIONAL, and deliberately so.
    #
    # The effective verdict is derived from the audit, not read from
    # this field, so a complete audit that simply ends after
    # "contradictions" carries everything needed to decide. Observed
    # directly: qwen3.5:9b emits all six audit sections, correctly, and
    # then closes the object -- treating the audit AS the answer, which
    # under an evidence-first contract it is. Rejecting that would spend
    # a whole repair call to recover a verdict already implied by the
    # evidence.
    #
    # A decision that IS present must still be one of the two valid
    # values; a garbage verdict is a malformed response.
    decision = parsed.get("decision")

    if decision is not None and (
        not isinstance(decision, str)
        or decision.upper() not in VALID_DECISIONS
    ):
        return "invalid 'decision' field"

    issues = parsed.get("issues")

    if issues is not None and not isinstance(issues, list):
        return "invalid 'issues' field (must be a list)"

    audit = parsed.get("audit")

    if not isinstance(audit, dict):
        return "missing 'audit' object -- a bare verdict is not an audit"

    requirements = audit.get("requirements")

    if not isinstance(requirements, list) or not requirements:
        return "'audit.requirements' must be a non-empty list"

    for entry in requirements:
        if not isinstance(entry, dict):
            return "'audit.requirements' contains a non-object entry"

        if not str(entry.get("id") or "").strip():
            return "'audit.requirements' contains an entry with no 'id'"

        if not isinstance(entry.get("covered"), bool):
            return (
                "requirement "
                f"{entry.get('id')!r} has no boolean 'covered'"
            )

        evidence = str(entry.get("evidence") or "").strip()

        if len(evidence) < MIN_EVIDENCE_CHARS:
            return (
                "requirement "
                f"{entry.get('id')!r} has no substantive 'evidence'"
            )

    for dimension in AUDIT_DIMENSIONS:
        section = audit.get(dimension)

        if not isinstance(section, dict):
            return (
                f"'audit.{dimension}' is missing -- every dimension "
                "must be explicitly classified, never omitted"
            )

        applicable = section.get("applicable")

        if not isinstance(applicable, bool):
            return f"'audit.{dimension}.applicable' must be a boolean"

        if not applicable:
            reason = str(section.get("reason") or "").strip()

            if len(reason) < MIN_EVIDENCE_CHARS:
                return (
                    f"'audit.{dimension}' is marked not applicable "
                    "without a substantive 'reason'"
                )

            continue

        problem = _validate_checks(dimension, section)

        if problem is not None:
            return problem

    if not isinstance(audit.get("contradictions"), list):
        return "'audit.contradictions' must be a list (empty when none)"

    inconsistencies = literal_inconsistencies(audit)

    if inconsistencies:
        return f"audit is internally inconsistent: {inconsistencies[0]}"

    overruled = overruled_authorized_symbols(
        audit,
        authorized_symbols
    )

    if overruled:
        return (
            "audit marks deterministically authorized future symbol "
            f"{overruled[0]!r} as invalid; the harness already compiled "
            "the contract and matched that symbol against the "
            "authoritative task"
        )

    return None


def overruled_authorized_symbols(audit, authorized_symbols):
    """
    Authorized future symbols the model marked invalid anyway.

    The deterministic gate compiled the contract and matched each of
    these against the current specification. A reviewer rejecting one for
    not existing yet is re-litigating machine evidence, and that was the
    single most common false rejection before this redesign.

    Matching is by normalised suffix so that `LedgerService.CloseAccount`
    in the check and `CloseAccount` in the gate refer to the same thing.
    """

    if not authorized_symbols:
        return []

    known = {
        str(symbol).strip().lower()
        for symbol in authorized_symbols
        if str(symbol).strip()
    }

    section = audit.get("future_api")

    if not isinstance(section, dict):
        return []

    overruled = []

    for check in section.get("checks") or []:
        if not isinstance(check, dict) or check.get("valid") is not False:
            continue

        target = str(check.get("target") or "").strip().lower()

        if not target:
            continue

        for symbol in known:
            if (
                target == symbol
                or target.endswith("." + symbol)
                or symbol.endswith("." + target)
            ):
                overruled.append(check.get("target"))
                break

    return overruled


# ---------------------------------------------------------------------
# Effective verdict
# ---------------------------------------------------------------------

def audit_failures(audit):
    """
    Every reason the audit itself gives to withhold approval, as issue
    strings the revision prompt can act on.

    Read only from a schema-valid audit.
    """

    failures = []

    for entry in audit.get("requirements") or []:
        if isinstance(entry, dict) and entry.get("covered") is False:
            failures.append(
                f"Requirement {entry.get('id')} is not covered by any "
                f"test: {entry.get('evidence')}"
            )

    for dimension in AUDIT_DIMENSIONS:
        section = audit.get(dimension)

        if not isinstance(section, dict) or not section.get("applicable"):
            continue

        for check in section.get("checks") or []:
            if isinstance(check, dict) and check.get("valid") is False:
                failures.append(
                    f"{dimension} check failed for "
                    f"{check.get('target')}: {check.get('evidence')}"
                )

    for contradiction in audit.get("contradictions") or []:
        text = str(contradiction or "").strip()

        if text:
            failures.append(f"Contradiction: {text}")

    return failures


def derive_effective_verdict(parsed):
    """
    The verdict the AUDIT supports, which is not always the verdict the
    model wrote down.

    Returns (decision, issues) for a schema-valid response, or
    (None, reason) when the audit cannot support any verdict.

    The asymmetry here is the point:

    - APPROVE survives only when the audit is clean. A model that lists a
      contradiction, an uncovered requirement or a failed check and then
      writes "APPROVE" has produced the evidence for its own rejection,
      and the evidence wins.

    - REJECT is never overturned. A model may reject for something the
      structured dimensions do not model, so its own issues stand -- but
      it must say SOMETHING. A rejection with no issues and no audit
      failure is unusable: there is nothing for the revision prompt to
      fix, so it fails closed as an invalid audit rather than becoming a
      silent rejection loop.
    """

    audit = parsed.get("audit") or {}

    # An absent decision is not a missing answer. The audit either
    # convicts the contract or it does not, and that IS the verdict.
    stated = str(parsed.get("decision") or "").upper()
    issues = [
        str(issue).strip()
        for issue in parsed.get("issues") or []
        if str(issue or "").strip()
    ]

    failures = audit_failures(audit)

    # No stated decision, but the model listed issues. Those issues are
    # a rejection in everything but name, and reading them as an
    # approval would silently discard the only defects the model found.
    rejecting = (
        stated == "REJECT"
        or (not stated and bool(issues))
    )

    if rejecting:
        if not issues and not failures:
            return (
                None,
                "REJECT with no issues and no failing audit entry -- "
                "there is nothing for a revision to act on"
            )

        return "REJECT", issues + [
            failure
            for failure in failures
            if failure not in issues
        ]

    if failures:
        # Self-contradiction. Deriving REJECT rather than discarding keeps
        # the evidence the model already produced and hands it to the
        # revision prompt, which is strictly more useful than a retry.
        return "REJECT", failures

    return "APPROVE", []


# ---------------------------------------------------------------------
# Repair eligibility
# ---------------------------------------------------------------------

def audit_substance(parsed):
    """
    How many concrete audit entries a response actually contains,
    counted leniently across plausible shapes.

    This is the anti-fabrication measure. It is deliberately generous
    about WHERE it finds entries -- a misplaced or misnamed audit still
    counts -- because the question it answers is not "is this valid" but
    "did the model do any auditing at all".
    """

    if not isinstance(parsed, dict):
        return 0

    audit = parsed.get("audit")

    if not isinstance(audit, dict):
        # A misplaced audit still counts, so fall back to scanning the
        # top level -- but never count the verdict's own fields. A bare
        # {"decision": "REJECT", "issues": [...]} has no audit in it,
        # and mistaking its issues for evidence would buy a repair call
        # whose only possible output is an invented audit.
        audit = {
            key: value
            for key, value in parsed.items()
            if key not in ("decision", "issues", "verdict")
        }

    total = 0

    for value in audit.values():
        if isinstance(value, list):
            total += len(value)

        elif isinstance(value, dict):
            checks = value.get("checks")

            if isinstance(checks, list):
                total += len(checks)

            elif value.get("applicable") is False:
                total += 1

    return total


def hoist_misplaced_verdict(parsed):
    """
    Lift a `decision`/`issues` pair the model wrote INSIDE `audit` up to
    the root, where every reader of a reviewer response expects it.

    Observed in the 16K context evaluation, case 9: qwen3.5:9b closed its
    audit sections and then wrote

        "decision": "REJECT",
        "issues": ["Requirement 3 is not covered - no test verifies
                    Deposit returns false when the account does not
                    exist"]

    one level too deep. The response was schema-VALID -- `decision` is
    optional at the root and `audit` is not a closed set of keys -- so
    validation passed, `derive_effective_verdict` read a root with no
    decision and no issues, found no failing audit entry, and returned
    APPROVE. The model's own correctly-reasoned rejection was discarded
    in favour of the opposite verdict.

    This is a pure structural move: the same bytes, one level up. It
    invents nothing, and it cannot launder an approval, because
    relocation is MONOTONIC TOWARDS REJECT:

    - hoisting `issues` can only add rejection evidence, since
      `derive_effective_verdict` treats a decision-less response that
      carries issues as a rejection;
    - hoisting `decision: "REJECT"` can only turn a derived APPROVE into
      the rejection the model stated;
    - hoisting `decision: "APPROVE"` changes nothing, because a stated
      approval never survives a failing audit entry anyway.

    Only ever fills a root field that is absent or null, so a verdict the
    model did state at the root always wins.
    """

    if not isinstance(parsed, dict):
        return parsed

    audit = parsed.get("audit")

    if not isinstance(audit, dict):
        return parsed

    moved = {
        key: audit[key]
        for key in ("decision", "issues")
        if key in audit and parsed.get(key) is None
    }

    if not moved:
        return parsed

    hoisted = dict(parsed)

    hoisted["audit"] = {
        key: value
        for key, value in audit.items()
        if key not in moved
    }

    hoisted.update(moved)

    return hoisted


def audit_repair_eligible(parsed):
    """
    Whether a malformed response is worth one bounded repair call.

    Repair reshapes what the model said. It cannot supply what the model
    never said, so a response with no audit substance is not a formatting
    problem -- it is an absent audit, and the only honest handling is a
    fresh review attempt. Skipping the call also saves a round trip that
    could only ever produce an invention.
    """

    return audit_substance(parsed) > 0


def repair_fabricated(original, repaired):
    """
    True when a repair produced more audit entries than it was given.

    Reshaping may merge, split or drop entries; it may not conjure them.
    Counting is the cheapest guard that still makes fabrication of a
    whole audit -- the failure mode that matters -- structurally
    impossible.
    """

    return audit_substance(repaired) > audit_substance(original)
