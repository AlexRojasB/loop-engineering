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
