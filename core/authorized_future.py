"""
Deterministic authorization of future API references, propagated to the
model reviewers.

The contract compilation gate already answers, deterministically, the
question the reviewers keep getting wrong:

    "Is the symbol the compiler says is missing something the CURRENT
     authoritative specification asked for?"

When the answer is yes the gate classifies the diagnostic as
`expected_red` and lets the contract through. But that verdict used to be
thrown away: reviewers received only the task text and the production
code, and had to re-derive the same judgement from prose. In Ledger Full
#3 Spec 005 they derived it wrongly, over and over -- the specification
says "extend successful transfers with an optional description", never
the literal words `Description` or a four-argument `Transfer`, so
reviewer after reviewer rejected a contract the gate had already
authorized:

    "the production LedgerService.Transfer method does NOT have this
     parameter"
    "the Transaction class in production code has no Description property"

Both statements are true and neither is a defect. That spec burned all
five attempts and ended the run.

This module carries the gate's finding into the reviewer prompts as
structured evidence, so reviewers stop re-litigating mere absence. It
grants nothing on its own: a symbol appears here only because the
adapter matched a real compiler diagnostic against the current
specification, and reviewers remain free to reject the contract for any
other reason.
"""


NO_AUTHORIZED_FUTURE = (
    "(none: the deterministic gate found no task-authorized future "
    "symbols in this contract)"
)

MAX_ENTRIES = 12

MAX_MESSAGE_CHARS = 200


def _clip(value, limit=MAX_MESSAGE_CHARS):
    text = " ".join(
        str(value or "").split()
    )

    if len(text) <= limit:
        return text

    return text[:limit - 3].rstrip() + "..."


def authorized_future_entries(report):
    """
    Structured record of every future symbol the deterministic gate
    authorized for this candidate contract.

    Each entry is:

        {
            "symbol":     what the contract referenced,
            "authority":  why the current spec authorizes it,
            "evidence":   the compiler diagnostic that proves it is
                          absent from production today
        }

    Returns [] when the gate did not run, could not classify, or found
    nothing -- in which case reviewers simply get no extra evidence and
    behave exactly as before.
    """

    if not isinstance(report, dict):
        return []

    entries = []
    seen = set()

    for diagnostic in report.get(
        "expected_red",
        []
    ) or []:
        if not isinstance(diagnostic, dict):
            continue

        symbol = diagnostic.get("symbol")

        if not symbol or symbol in seen:
            continue

        seen.add(symbol)

        entries.append(
            {
                "symbol": str(symbol),
                "authority": _clip(
                    diagnostic.get("reason")
                    or (
                        f"'{symbol}' is requested by the current "
                        "authoritative specification."
                    )
                ),
                "evidence": _clip(
                    f"{diagnostic.get('code', '')}: "
                    f"{diagnostic.get('message', '')}"
                )
            }
        )

        if len(entries) >= MAX_ENTRIES:
            break

    return entries


def format_authorized_future(entries):
    """
    Render authorized future symbols for a reviewer prompt.
    """

    if not entries:
        return NO_AUTHORIZED_FUTURE

    lines = []

    for entry in entries:
        lines.append(
            f"- `{entry['symbol']}`\n"
            f"  authorized because: {entry['authority']}\n"
            f"  compiler evidence of current absence: "
            f"{entry['evidence']}"
        )

    return "\n".join(lines)


def authorized_future_text(report):
    return format_authorized_future(
        authorized_future_entries(
            report
        )
    )
