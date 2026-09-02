"""
Aggregate the reviewer-model comparison into the requested tables.

    python tests/reviewer_model_eval/report.py <results.jsonl> [hardware.json]
"""

import json
import statistics
import sys
from pathlib import Path

GROUPS = {
    "semantic_trap": [1, 3, 5, 7],
    "clean_approval": [6, 8, 10],
    "legitimate_rejection": [2, 4, 9],
}


def raw_decision(record):
    """
    The decision the model actually reached, ignoring whether the
    harness could use it.

    Separates REASONING quality from OUTPUT-FORMAT compatibility: a
    model that decides correctly but emits {"decision": "APPROVE"} with
    no "issues" key is right about the contract and unusable in the
    pipeline, and those are different problems with different fixes.
    """

    if record["status"] == "ok":
        return record["actual"]

    text = record.get("raw_response") or ""

    try:
        parsed = json.loads(text)

    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None

    decision = parsed.get("decision")

    if isinstance(decision, str):
        decision = decision.upper()

        if decision in ("APPROVE", "REJECT"):
            return decision

    return None


def pct(numerator, denominator):
    if not denominator:
        return "n/a"

    return f"{100.0 * numerator / denominator:.0f}%"


def p95(values):
    if len(values) < 2:
        return None

    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        int(round(0.95 * (len(ordered) - 1)))
    )

    return ordered[index]


def load(path):
    records = []

    for line in Path(path).read_text().splitlines():
        line = line.strip()

        if line:
            records.append(json.loads(line))

    return records


def summarize(records):
    latencies = [
        record["latency_s"]
        for record in records
    ]

    cold = [
        record["latency_s"]
        for record in records
        if record["repetition"] == 1
    ]

    warm = [
        record["latency_s"]
        for record in records
        if record["repetition"] != 1
    ]

    total = len(records)
    correct = sum(
        1
        for record in records
        if record["correct"]
    )

    semantic_correct = sum(
        1
        for record in records
        if raw_decision(record) == record["expected"]
    )

    semantic_scored = sum(
        1
        for record in records
        if raw_decision(record) is not None
    )

    # A reviewer that never reads anything and approves everything
    # scores the APPROVE share of the suite. Any accuracy at or below
    # this line is indistinguishable from a rubber stamp, so the
    # headline number is meaningless without it alongside.
    rubber_stamp = sum(
        1
        for record in records
        if record["expected"] == "APPROVE"
    )

    # The number the role actually exists for: catching the defective
    # contract. Approving everything scores zero here.
    should_reject = [
        record
        for record in records
        if record["expected"] == "REJECT"
    ]

    caught = sum(
        1
        for record in should_reject
        if raw_decision(record) == "REJECT"
    )

    return {
        "n": total,
        "correct": correct,
        "accuracy": pct(correct, total),
        "rubber_stamp_baseline": pct(rubber_stamp, total),
        # The share of readable verdicts that were APPROVE. A reviewer
        # approaching 100% here is a rubber stamp no matter what its
        # accuracy says, because the suite is balanced 50/50.
        "approve_rate": pct(
            sum(
                1
                for record in records
                if raw_decision(record) == "APPROVE"
            ),
            sum(
                1
                for record in records
                if raw_decision(record) is not None
            )
        ),
        "defects_caught": f"{caught}/{len(should_reject)}",
        "semantic_correct": semantic_correct,
        "semantic_accuracy_all": pct(semantic_correct, total),
        "semantic_accuracy_scored": pct(
            semantic_correct,
            semantic_scored
        ),
        "semantic_scored": semantic_scored,
        "false_approve": sum(
            1
            for record in records
            if record["false_approve"]
        ),
        "false_reject": sum(
            1
            for record in records
            if record["false_reject"]
        ),
        "schema_invalid": sum(
            1
            for record in records
            if not record["schema_valid"]
        ),
        "schema_repair_attempted": sum(
            1
            for record in records
            if record["schema_repair_required"]
        ),
        "schema_repair_succeeded": sum(
            1
            for record in records
            if record.get("schema_repair_outcome") == "ok"
        ),
        "unusable_verdicts": sum(
            1
            for record in records
            if record["status"] != "ok"
        ),
        "timeouts": sum(
            1
            for record in records
            if record["timeout"]
        ),
        "truncated": sum(
            1
            for record in records
            if record["truncated"]
        ),
        "call_failed": sum(
            1
            for record in records
            if record["call_failed"]
        ),
        # Ollama reuses the KV cache for a repeated identical prompt,
        # so repetitions 2 and 3 skip prompt evaluation almost entirely.
        # In production every review carries a different prompt, so the
        # rep-1 (cold-prompt) figure is the production-representative
        # one and the all-rep mean understates real latency badly.
        "mean_latency_cold": round(
            statistics.mean(cold) if cold else 0,
            1
        ),
        "median_latency_cold": round(
            statistics.median(cold) if cold else 0,
            1
        ),
        "p95_latency_cold": (
            round(p95(cold), 1)
            if p95(cold) is not None
            else None
        ),
        "mean_latency_cached": round(
            statistics.mean(warm) if warm else 0,
            1
        ),
        "mean_latency": round(
            statistics.mean(latencies),
            1
        ),
        "median_latency": round(
            statistics.median(latencies),
            1
        ),
        "p95_latency": (
            round(p95(latencies), 1)
            if p95(latencies) is not None
            else None
        ),
        "total_latency": round(
            sum(latencies),
            1
        ),
        "thinking_returned": sum(
            1
            for record in records
            if record["thinking_field_returned"]
        ),
        "mean_prompt_tokens": round(
            statistics.mean([
                record["prompt_eval_count"]
                for record in records
                if record.get("prompt_eval_count")
            ] or [0])
        ),
        "mean_eval_tokens": round(
            statistics.mean([
                record["eval_count"]
                for record in records
                if record.get("eval_count")
            ] or [0])
        ),
        # The single clearest signal of whether a reviewer engaged with
        # the contract at all. Before the evidence-first redesign the
        # median verdict was 8 generated tokens -- the length of
        # {"decision":"APPROVE"}.
        "median_eval_tokens": round(
            statistics.median([
                record["eval_count"]
                for record in records
                if record.get("eval_count") is not None
            ] or [0])
        ),
        "audit_present": sum(
            1
            for record in records
            if record.get("audit_present")
        ),
        "audit_absent": sum(
            1
            for record in records
            if record.get("audit_absent")
        ),
        "audit_unusable": sum(
            1
            for record in records
            if record.get("audit_unusable")
        ),
        "verdict_derived": sum(
            1
            for record in records
            if record.get("verdict_derived")
        ),
        "median_audit_entries": round(
            statistics.median([
                record.get("audit_entries") or 0
                for record in records
            ] or [0])
        ),
    }


def main():
    records = load(sys.argv[1])

    models = []

    for record in records:
        if record["model"] not in models:
            models.append(record["model"])

    per_model = {
        model: summarize([
            record
            for record in records
            if record["model"] == model
        ])
        for model in models
    }

    print()
    print("=" * 78)
    print("HEADLINE COMPARISON")
    print("=" * 78)

    rows = [
        ("calls", "n"),
        ("accuracy", "accuracy"),
        ("correct / total", None),
        ("false APPROVE", "false_approve"),
        ("false REJECT", "false_reject"),
        ("schema-invalid verdicts", "schema_invalid"),
        ("schema repairs attempted", "schema_repair_attempted"),
        ("schema repairs succeeded", "schema_repair_succeeded"),
        ("UNUSABLE verdicts (harness discards)", "unusable_verdicts"),
        ("-- schema-agnostic reasoning --", "__blank__"),
        ("decision readable at all", "semantic_scored"),
        ("reasoning correct (of all calls)", "semantic_accuracy_all"),
        ("reasoning correct (of readable)", "semantic_accuracy_scored"),
        ("baseline: always APPROVE", "rubber_stamp_baseline"),
        ("APPROVE response rate", "approve_rate"),
        ("defective contracts caught", "defects_caught"),
        ("timeouts", "timeouts"),
        ("truncations", "truncated"),
        ("call failures", "call_failed"),
        ("mean latency COLD prompt (s)", "mean_latency_cold"),
        ("median latency COLD (s)", "median_latency_cold"),
        ("p95 latency COLD (s)", "p95_latency_cold"),
        ("mean latency cached reps (s)", "mean_latency_cached"),
        ("mean latency all reps (s)", "mean_latency"),
        ("median latency all reps (s)", "median_latency"),
        ("p95 latency all reps (s)", "p95_latency"),
        ("total model time (s)", "total_latency"),
        ("thinking field returned", "thinking_returned"),
        ("mean prompt tokens", "mean_prompt_tokens"),
        ("mean eval tokens", "mean_eval_tokens"),
        ("median eval tokens", "median_eval_tokens"),
        ("-- evidence-first audit --", "__blank__"),
        ("responses carrying an audit", "audit_present"),
        ("median audit entries", "median_audit_entries"),
        ("discarded: no audit at all", "audit_absent"),
        ("discarded: audit supports no verdict", "audit_unusable"),
        ("verdict derived against model's own", "verdict_derived"),
    ]

    width = max(len(label) for label, _ in rows) + 2

    header = "".ljust(width)

    for model in models:
        header += model.ljust(16)

    print(header)

    for label, key in rows:
        line = label.ljust(width)

        for model in models:
            if key is None:
                value = (
                    f"{per_model[model]['correct']}/"
                    f"{per_model[model]['n']}"
                )

            elif key == "__blank__":
                value = ""

            else:
                value = per_model[model][key]

            line += str(value).ljust(16)

        print(line)

    print()
    print("=" * 78)
    print("BY CASE GROUP")
    print("=" * 78)

    line = "".ljust(width)

    for model in models:
        line += model.ljust(16)

    print(line)

    for group, ids in GROUPS.items():
        line = f"{group} {ids}".ljust(width)

        for model in models:
            subset = [
                record
                for record in records
                if record["model"] == model
                and record["case"] in ids
            ]

            correct = sum(
                1
                for record in subset
                if record["correct"]
            )

            line += (
                f"{correct}/{len(subset)} "
                f"({pct(correct, len(subset))})"
            ).ljust(16)

        print(line)

    print()
    print("=" * 78)
    print("PER CASE")
    print("=" * 78)

    case_ids = sorted({
        record["case"]
        for record in records
    })

    header = (
        "case".ljust(5)
        + "expect".ljust(9)
    )

    for model in models:
        header += f"{model} verdicts".ljust(34)

    print(header)

    for case_id in case_ids:
        subset = [
            record
            for record in records
            if record["case"] == case_id
        ]

        line = (
            str(case_id).ljust(5)
            + subset[0]["expected"].ljust(9)
        )

        for model in models:
            model_subset = [
                record
                for record in subset
                if record["model"] == model
            ]

            verdicts = "/".join(
                (
                    record["actual"]
                    if record["status"] == "ok"
                    else (
                        f"({raw_decision(record) or 'FAIL'})"
                    )
                )[:9]
                for record in model_subset
            )

            correct = sum(
                1
                for record in model_subset
                if record["correct"]
            )

            mean_latency = (
                statistics.mean([
                    record["latency_s"]
                    for record in model_subset
                ])
                if model_subset
                else 0
            )

            line += (
                f"{verdicts} "
                f"[{correct}/{len(model_subset)}] "
                f"{mean_latency:.0f}s"
            ).ljust(34)

        print(line)

    print()
    print("=" * 78)
    print("EVERY INCORRECT OR UNUSABLE VERDICT")
    print("=" * 78)

    for record in records:
        if record["correct"] and record["status"] == "ok":
            continue

        print()
        print(
            f"{record['model']} | case {record['case']} "
            f"({record['case_name']}) rep {record['repetition']} | "
            f"expected {record['expected']} | "
            f"status={record['status']} actual={record['actual']}"
        )

        if record["issue_summary"]:
            print(f"   issues: {record['issue_summary']}")

        elif record["status"] != "ok":
            print(
                f"   raw: "
                f"{(record.get('raw_response') or '')[:300]}"
            )

    if len(sys.argv) > 2:
        hardware = json.loads(
            Path(sys.argv[2]).read_text()
        )

        print()
        print("=" * 78)
        print("HARDWARE UNDER LOAD (sampled every 5s while calls ran)")
        print("=" * 78)
        print(
            json.dumps(
                hardware["per_model_load"],
                indent=2
            )
        )
        print(
            f"total wall-clock: "
            f"{hardware['total_runtime_s']}s"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
