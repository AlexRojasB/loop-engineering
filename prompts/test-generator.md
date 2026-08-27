You are writing ONLY NEW TEST METHODS for a TDD workflow.

TASK:

{task}

CURRENT PRODUCTION:

{production}

EXISTING TEST FILE:

{existing_tests}

PREVIOUS ATTEMPT FINDINGS FOR THIS SAME WORK ITEM:

{prior_spec_failures}

These come from earlier outer attempts at this same work item; the
repository has since been restored. Do not regenerate a test that
repeats any of them.

Generate ONLY the new test methods required for the task.

IMPORTANT:

- Do NOT output the complete test file.
- Do NOT output namespace declarations.
- Do NOT output using statements.
- Do NOT output a test class declaration.
- Do NOT output production classes.
- Do NOT output production enums.
- Do NOT output production interfaces.
- Do NOT implement the requested feature inside tests.
- Preserve the existing testing style.
- The production feature does NOT exist yet.
- It is expected that the new tests initially fail.
- Use only the public production contract. Do not access private fields or private members directly.

Return only one or more new test methods.

No Markdown.
No explanation.

CRITICAL TDD REQUIREMENT:

The generated tests MUST exercise the requested NEW behavior directly.

At least one generated test MUST fail against CURRENT PRODUCTION for a reason
caused specifically by the requested feature not being implemented yet.

Examples:

- If the task adds a public property, directly assert that property's required
  initial value and/or its value after the requested operation.

- If the task adds a method, invoke that future public method directly.

- If the task changes state, assert the exact requested state transition.

- If the task adds accumulation behavior, assert the accumulated value after
  multiple operations.

- If the task adds filtering/query behavior, assert the exact returned items.

Do NOT generate tests that merely re-test existing behavior.

Do NOT weaken the contract to avoid compilation failures caused by public
members explicitly required by the task.

A missing future public member is an EXPECTED RED condition.

Before returning the tests, ask:

"Would at least one of these tests fail against the unchanged production code
because the requested feature is still missing?"

If the answer is NO, the generated tests are invalid and must be strengthened.
