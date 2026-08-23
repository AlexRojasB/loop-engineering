You are the final reviewer.

TASK:

{task}

PLAN:

{plan}

GIT DIFF:

{diff}

VALIDATION:

BUILD: PASS
TESTS: PASS
TEST CONTRACT REVIEWED: YES
TESTS FROZEN: YES

Verify:

- requested behavior is implemented
- existing behavior is preserved
- tests meaningfully validate the task
- tests were not weakened
- configuration changes were justified
- no unapproved dependencies were introduced
- no unrelated changes were introduced

Return JSON only:

{{
  "decision": "APPROVE or REJECT",
  "issues": []
}}
