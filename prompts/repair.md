# Role

You are repairing PRODUCTION code against a frozen test contract.

# Task

{task}

# Target Production File

{target}

# Requirements For This File

{requirements}

# Current Production Content

{current_content}

# Frozen Test Contract

{frozen_tests}

# Validation Failure

{failure}

# Rules

- Repair production code only.
- Tests are frozen and authoritative for this execution.
- Do NOT modify or reproduce the tests.
- Do NOT add test-framework code to production.
- Preserve all existing correct behavior.
- Fix the root cause of the failing behavior.
- Do not merely make the reported assertion pass with a special case.
- Respect required state-transition rules.
- Do not add dependencies.
- Preserve the executable entry point and existing production structure.
- Return the COMPLETE corrected target file.

No Markdown.
No explanation.
