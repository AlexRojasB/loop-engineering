Correct these NEW TEST METHODS.

TASK:

{task}

CURRENT PRODUCTION:

{production}

EXISTING TEST FILE:

{existing_tests}

CURRENT NEW TEST METHODS:

{snippet}

FINDINGS FROM PREVIOUS ATTEMPTS AT THIS SAME WORK ITEM:

{prior_spec_failures}

These come from earlier outer attempts at this same work item, before
the repository was restored. They are known traps: do not reintroduce
any of them.

PREVIOUSLY RAISED CONCERNS IN THIS TEST CONTRACT RUN:

{prior_issues}

These are concerns raised by earlier review attempts against this same
test contract, in this same run. They are not guaranteed to still apply —
some may already be resolved by an earlier correction. When correcting the
test methods below, make sure none of these concerns are still present or
reintroduced by your correction, in addition to fixing the ISSUES below.

ISSUES:

{issues}

Return ONLY corrected new test methods.

Do not return the full test file.
Do not redefine production types.
Do not declare a class.
Do not add using statements.
Do not implement production behavior.
Do not access private production members directly.

No Markdown.
No explanation.

# Deterministically Authorized Future API

{authorized_future_contract}

The harness already COMPILED this contract and matched each symbol above
against the current authoritative task. That classification is
deterministic machine evidence, not an opinion, and it is not yours to
re-litigate.

For every symbol listed above:

- its absence from current production is EXPECTED and PROVEN;
- "this member does not exist", "this overload does not exist", "this
  property is not on that class", "this parameter is not in the current
  signature" are NOT defects and MUST NOT appear in your issues;
- treat it as if it already existed with the shape the task describes,
  and review the SEMANTICS of how the contract uses it.

A symbol that is NOT listed above gets no such protection. If the
contract references something the task does not ask for, that is an
invented API and you should reject it.

You may still reject this contract for any other defect: wrong Arrange,
unreachable state, contradictory assertion, identity/provenance error,
invented behavior, or an expected result that contradicts the task.
