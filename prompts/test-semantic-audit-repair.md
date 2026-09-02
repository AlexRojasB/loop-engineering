You produced the following response, but it does not match the required
output schema for a semantic audit.

YOUR PREVIOUS RESPONSE:

{malformed_response}

REQUIRED JSON SCHEMA:

{{
  "audit": {{
    "requirements": [
      {{"id": "1", "covered": true, "evidence": "..."}}
    ],
    "setup":       {{"applicable": true,  "checks": [{{"target": "...", "valid": true, "evidence": "..."}}]}},
    "identity":    {{"applicable": false, "reason": "..."}},
    "transitions": {{"applicable": true,  "checks": [{{"target": "...", "valid": true, "evidence": "..."}}]}},
    "future_api":  {{"applicable": true,  "checks": [{{"target": "...", "valid": true, "evidence": "..."}}]}},
    "contradictions": []
  }},
  "decision": "APPROVE" or "REJECT",
  "issues": []
}}

This is a FORMATTING correction only. It is not a new review and not a
second opinion.

Re-emit the audit findings your previous response ALREADY contains, in
the schema above. Move them into the right fields. Rename fields. Fix
nesting. Nothing else.

Do NOT re-review the test contract.
Do NOT change the decision.
Do NOT add, remove, or reword any finding.

Above all: do NOT invent audit content.

If your previous response did not examine a requirement, do not write a
requirement entry for it. If it did not check identity, mark identity
`applicable: false` with the reason your previous response gave, or omit
what you cannot source from that response. An audit assembled here rather
than during the review is worthless, and the harness counts the entries
to detect exactly that.

If the previous response contains no audit findings at all, you cannot
repair it. Return it unchanged.

Return JSON only. No Markdown. No explanation.
