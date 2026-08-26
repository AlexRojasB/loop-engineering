You produced the following response, but it does not match the
required output schema for this review.

YOUR PREVIOUS RESPONSE:

{malformed_response}

REQUIRED JSON SCHEMA:

{{
  "decision": "APPROVE" or "REJECT",
  "issues": []
}}

Your previous response already contains a judgment, and, if that
judgment was REJECT, one or more issues. Re-emit that SAME judgment
and SAME issues in the exact schema above.

Do NOT reconsider or re-review the test contract.
Do NOT change the decision.
Do NOT add, remove, or reword any issue.

This is a formatting correction only. It is not a new review.

If the previous response's judgment was REJECT, extract every issue
it described into the "issues" array as complete strings.

If the previous response's judgment was APPROVE, return an empty
"issues" array.

Return JSON only. No Markdown. No explanation.
