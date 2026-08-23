You are reviewing a FUTURE TEST CONTRACT in a TDD workflow.

TASK:

{task}

CURRENT PRODUCTION:

{production}

FULL TEST FILE AFTER DETERMINISTIC MERGE:

{tests}

The new production feature DOES NOT EXIST YET.

That is intentional.

DO NOT reject because:
- requested methods do not exist yet
- requested enum/status values do not exist yet
- tests currently fail
- tests currently fail to compile because the new API is missing

Review ONLY test correctness.

Verify:

1. Each new test maps to an actual requirement.
2. Arrange represents the described state.
3. Act invokes the intended feature.
4. Assertions match the business rule.
5. Existing tests remain intact.
6. No production type was redefined.
7. No unrelated behavior was invented.
8. Tests use the public production contract only.
9. Tests do not access private fields or private members directly.
10. Successful transitions verify the expected new state.
11. Invalid transitions verify the state remains unchanged.
12. Unknown identifiers return the expected failure result.

Return JSON only:

{{
  "decision": "APPROVE or REJECT",
  "issues": []
}}
