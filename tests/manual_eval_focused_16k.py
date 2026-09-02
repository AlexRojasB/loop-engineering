"""
Focused 16K re-evaluation of the qwen3.5:9b semantic reviewer, with the
deterministic gates running FIRST.

NOT a unit test. Excluded from `unittest discover` by its filename.

Why this exists
---------------

The context sweep (results/context-20260902-112156) settled the context
question: 16K beat both 24K and 32K on accuracy, usable responses and
latency, so `reviewer_context_size` stays at 16384 and this harness does
not offer a context knob. What the sweep also showed is that two of the
four 16K failures were never reviewer-judgement failures at all:

    case 4 (invented API)      the contract references TransferRequest /
                               TransferMetadata / TransferChannel, none
                               of which the spec requests. The
                               deterministic contract gate already
                               classifies those CS0246 diagnostics as
                               INVALID and rejects the contract before
                               any reviewer runs. The old harness called
                               the semantic reviewer directly, so it
                               measured a safety net that production
                               never reaches for this defect.

    case 9 (missing scenario)  the reviewer stated the correct rejection
                               and the harness recorded call_failed,
                               because the response was one closing
                               brace short of parseable.

So this harness differs from manual_eval_reviewer_models.py in exactly
one structural way: it runs the same deterministic validation the Test
Contract phase runs, in the same order, and only calls the semantic
reviewer for contracts that survive it. That makes "model calls avoided
by deterministic validation" a measurable quantity rather than an
assumption.

What is reused, unmodified
--------------------------

    adapter.analyze_test_source            production source-defect check
    adapter.classify_contract_diagnostics  production diagnostic gate
    semantic_test_review_prompt            production prompt rendering
    _resolve_reviewer_verdict              production dispatch, schema
                                           validation, bounded repair,
                                           truncation/recovery handling
    core.models.call_model -> ollama        production Ollama call

and the raw-response capture, hardware sampling and config plumbing from
manual_eval_reviewer_models, so token counts stay observable and the two
harnesses stay comparable.

The compiler is NOT invoked. Each fixture carries the diagnostics the
gate would see -- `authorized_future` for spec-requested symbols and
`unauthorized_future` for invented ones -- which are rendered into
MSBuild-shaped lines and fed to the real classifier. That keeps the run
offline and deterministic while exercising the production classification
code rather than a paraphrase of it.

Run:

    python tests/manual_eval_focused_16k.py
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importing this installs the urlopen capture that makes
# prompt_eval_count / eval_count observable, and provides the hardware
# sampler and config builder. Nothing in it is modified.
import tests.manual_eval_reviewer_models as base  # noqa: E402

from core.phases.test_contract_phase import (  # noqa: E402
    _resolve_reviewer_verdict,
    semantic_test_review_prompt,
)
from core.contract_validation import (  # noqa: E402
    analyze_candidate_test_source,
)
from core.authorized_future import (  # noqa: E402
    authorized_future_entries,
    format_authorized_future,
)
from languages.dotnet import DotNetAdapter  # noqa: E402
from tests.reviewer_model_eval.fixtures import (  # noqa: E402
    CASES,
    CONTRACT_PATH,
    PRODUCTION_PATH,
)


MODEL = "qwen3.5:9b"

# Fixed, deliberately. The context sweep already answered this.
CONTEXT_SIZE = 16384

# The four cases that failed at 16K, plus every case that passed, so a
# fix that breaks a previously-correct case cannot hide.
REGRESSION_CASES = [1, 3, 5, 7, 8, 10]

TARGET_CASES = [2, 4, 6, 9]


def compiler_output(case):
    """
    The build output the deterministic gate would see for this contract.

    Rendered in MSBuild's real diagnostic shape so the production
    `parse_diagnostics` regexes -- not a stand-in -- do the parsing.
    """

    lines = []

    diagnostics = list(
        case.get("authorized_future") or []
    ) + list(
        case.get("unauthorized_future") or []
    )

    for index, diagnostic in enumerate(diagnostics, start=1):
        lines.append(
            f"/w/{CONTRACT_PATH}({index * 10},13): error "
            f"{diagnostic['code']}: {diagnostic['message']} "
            f"[/w/LedgerPipeline.Tests/LedgerPipeline.Tests.csproj]"
        )

    return "\n".join(lines)


def deterministic_gate(case, adapter, config):
    """
    Every check the Test Contract phase runs BEFORE it spends a semantic
    reviewer call, in the same order.

    Returns (handled, verdict, reasons, report) where `handled` means the
    gate reached a decision on its own and the model is not needed.
    """

    # 1. Compiler-free defects intrinsic to the test source.
    source_ok, source_issues = analyze_candidate_test_source(
        adapter,
        case["contract"],
        CONTRACT_PATH
    )

    if not source_ok:
        return (
            True,
            "REJECT",
            list(source_issues),
            {"stage": "source"}
        )

    # 2. Diagnostic classification: is every missing symbol one the
    #    current authoritative spec actually asked for?
    report = adapter.classify_contract_diagnostics(
        compiler_output(case),
        case["task"]
    )

    if report and report.get("verdict") == "INVALID":
        return (
            True,
            "REJECT",
            list(report.get("issues") or []),
            {
                "stage": "diagnostics",
                "report": report
            }
        )

    return (
        False,
        None,
        [],
        {
            "stage": "passed",
            "report": report
        }
    )


def run_case(config, case, adapter, sampler):
    started_total = time.perf_counter()

    handled, gate_verdict, gate_reasons, gate_info = deterministic_gate(
        case,
        adapter,
        config
    )

    report = gate_info.get("report") or {}

    entries = authorized_future_entries(report) or (
        authorized_future_entries(
            {"expected_red": case["authorized_future"]}
        )
    )

    record = {
        "case": case["id"],
        "case_name": case["name"],
        "group": case["group"],
        "expected": case["expected"],
        "num_ctx": CONTEXT_SIZE,
        "model": MODEL,
        "deterministic_handled": handled,
        "deterministic_stage": gate_info.get("stage"),
        "deterministic_verdict": gate_verdict,
        "semantic_reviewer_called": not handled,
        "authorized_symbols": [
            entry["symbol"]
            for entry in entries
        ],
    }

    if handled:
        # The gate decided. No model call is made, which is the entire
        # point of measuring this.
        record.update(
            {
                "actual": gate_verdict,
                "status": "ok",
                "correct": gate_verdict == case["expected"],
                "latency_s": round(
                    time.perf_counter() - started_total,
                    3
                ),
                "ollama_calls": 0,
                "prompt_eval_count": None,
                "eval_count": None,
                "done_reason": None,
                "schema_valid": True,
                "schema_repair_required": False,
                "schema_repair_outcome": None,
                "timeout": False,
                "truncated": False,
                "call_failed": False,
                "verdict_reason": base.summarize_issues(gate_reasons),
                "issues": gate_reasons,
                "false_approve": (
                    gate_verdict == "APPROVE"
                    and case["expected"] == "REJECT"
                ),
                "false_reject": (
                    gate_verdict == "REJECT"
                    and case["expected"] == "APPROVE"
                ),
                "audit_present": False,
                "audit_entries": 0,
            }
        )

        return record

    prompt = semantic_test_review_prompt(
        case["task"],
        {PRODUCTION_PATH: case["production"]},
        case["contract"],
        prior_issues=[],
        prior_spec_failures=None,
        authorized_future=format_authorized_future(entries)
    )

    history_path = config["history_file"]

    _, offset = base.read_new_history(history_path, 0)
    raw_before = len(base.RAW_RESPONSES)

    if sampler is not None:
        sampler.label = f"case{case['id']}"

    started = time.perf_counter()

    outcome = _resolve_reviewer_verdict(
        config,
        MODEL,
        prompt,
        "semantic",
        CONTRACT_PATH,
        1,
        config.get("semantic_reviewer_thinking", False),
        CONTEXT_SIZE,
        config.get("reviewer_output_tokens", 2048),
        authorized_symbols=record["authorized_symbols"]
    )

    latency = time.perf_counter() - started

    if sampler is not None:
        sampler.label = "idle"

    events, _ = base.read_new_history(history_path, offset)
    raws = base.RAW_RESPONSES[raw_before:]

    event_names = [
        event.get("event")
        for event in events
    ]

    repair_events = [
        event
        for event in events
        if event.get("event") == "reviewer_schema_repair"
    ]

    first = raws[0] if raws else {}

    status = outcome["status"]
    decision = outcome.get("decision")
    issues = outcome.get("issues") or []

    raw_text = (
        first.get("response")
        or first.get("thinking")
        or ""
    )

    try:
        raw_parsed = json.loads(raw_text)

    except Exception:
        raw_parsed = None

    record.update(
        {
            "actual": decision if status == "ok" else None,
            "status": status,
            "correct": (
                status == "ok"
                and decision == case["expected"]
            ),
            "latency_s": round(latency, 2),
            "ollama_calls": len(raws),
            "prompt_eval_count": first.get("prompt_eval_count"),
            "eval_count": first.get("eval_count"),
            "done_reason": first.get("done_reason"),
            "schema_valid":
                "reviewer_schema_invalid" not in event_names,
            "schema_repair_required": bool(repair_events),
            "schema_repair_outcome": (
                repair_events[-1]["data"].get("outcome")
                if repair_events
                else None
            ),
            "timeout": status == "call_failed" and not raws,
            "truncated": status == "truncated",
            "unparseable": status == "unparseable",
            "call_failed": status == "call_failed",
            "verdict_recovered":
                "reviewer_verdict_recovered" in event_names,
            "verdict_reason": base.summarize_issues(issues),
            "issues": issues,
            "false_approve": (
                status == "ok"
                and decision == "APPROVE"
                and case["expected"] == "REJECT"
            ),
            "false_reject": (
                status == "ok"
                and decision == "REJECT"
                and case["expected"] == "APPROVE"
            ),
            "audit_present": isinstance(
                (raw_parsed or {}).get("audit"),
                dict
            ) if isinstance(raw_parsed, dict) else False,
            "audit_entries": (
                base.audit_substance(raw_parsed)
                if isinstance(raw_parsed, dict)
                else 0
            ),
            "raw_response": raw_text,
        }
    )

    return record


def summarize(records):
    usable = [
        record
        for record in records
        if record["status"] == "ok"
    ]

    latencies = [
        record["latency_s"]
        for record in records
        if record["semantic_reviewer_called"]
    ]

    avoided = [
        record
        for record in records
        if record["deterministic_handled"]
    ]

    lines = [
        "",
        "=" * 72,
        f"FOCUSED 16K EVALUATION -- {MODEL} @ num_ctx={CONTEXT_SIZE}",
        "=" * 72,
        f"cases                          {len(records)}",
        f"accuracy                       "
        f"{sum(1 for r in records if r['correct'])}/{len(records)}",
        f"  target cases (2,4,6,9)       "
        f"{sum(1 for r in records if r['correct'] and r['case'] in TARGET_CASES)}"
        f"/{sum(1 for r in records if r['case'] in TARGET_CASES)}",
        f"  regression controls          "
        f"{sum(1 for r in records if r['correct'] and r['case'] in REGRESSION_CASES)}"
        f"/{sum(1 for r in records if r['case'] in REGRESSION_CASES)}",
        f"false APPROVE                  "
        f"{sum(1 for r in records if r['false_approve'])}",
        f"false REJECT                   "
        f"{sum(1 for r in records if r['false_reject'])}",
        "",
        f"handled deterministically      {len(avoided)}"
        f"   (cases {[r['case'] for r in avoided]})",
        f"MODEL CALLS AVOIDED            {len(avoided)}",
        f"semantic reviewer called       "
        f"{sum(1 for r in records if r['semantic_reviewer_called'])}",
        f"total ollama calls             "
        f"{sum(r['ollama_calls'] for r in records)}",
        "",
        f"usable verdicts                {len(usable)}/{len(records)}",
        f"timeouts                       "
        f"{sum(1 for r in records if r['timeout'])}",
        f"truncated                      "
        f"{sum(1 for r in records if r['truncated'])}",
        f"unparseable                    "
        f"{sum(1 for r in records if r.get('unparseable'))}",
        f"schema invalid                 "
        f"{sum(1 for r in records if not r['schema_valid'])}",
        f"schema repairs attempted       "
        f"{sum(1 for r in records if r['schema_repair_required'])}",
        f"verdicts recovered             "
        f"{sum(1 for r in records if r.get('verdict_recovered'))}",
    ]

    if latencies:
        lines.extend(
            [
                "",
                f"mean latency (model calls)     "
                f"{statistics.mean(latencies):.1f}s",
                f"median latency                 "
                f"{statistics.median(latencies):.1f}s",
                f"max latency                    {max(latencies):.1f}s",
            ]
        )

    lines.extend(["", "-" * 72])

    for record in records:
        route = (
            f"DET/{record['deterministic_stage']}"
            if record["deterministic_handled"]
            else "semantic"
        )

        lines.append(
            f"case {record['case']:<3} "
            f"{record['expected']:<8} -> "
            f"{str(record['actual']):<8} "
            f"{'OK ' if record['correct'] else 'BAD'}  "
            f"{route:<18} "
            f"{record['latency_s']:>7}s  "
            f"calls={record['ollama_calls']}  "
            f"{record['case_name']}"
        )

        if record["verdict_reason"]:
            lines.append(
                f"          reason: {record['verdict_reason'][:200]}"
            )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--cases",
        default="",
        help="comma-separated case ids (default: all 10)"
    )

    parser.add_argument(
        "--out",
        default=str(
            REPO_ROOT / "tests/reviewer_model_eval/results"
        )
    )

    parser.add_argument(
        "--sample-interval",
        type=float,
        default=5.0
    )

    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.cases:
        wanted = {
            int(value)
            for value in args.cases.split(",")
        }
        cases = [
            case
            for case in CASES
            if case["id"] in wanted
        ]

    else:
        cases = CASES

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    results_path = out / f"focused16k-results-{stamp}.jsonl"
    samples_path = out / f"focused16k-samples-{stamp}.jsonl"
    summary_path = out / f"focused16k-summary-{stamp}.txt"

    config = base.build_config(out, MODEL)
    config["history_file"] = str(
        out / f"focused16k-history-{stamp}.jsonl"
    )

    # This harness exists to test 16384 and nothing else.
    config["reviewer_context_size"] = CONTEXT_SIZE

    adapter = DotNetAdapter()

    sampler = base.Sampler(
        args.sample_interval,
        samples_path
    )
    sampler.start()

    records = []
    started = time.perf_counter()

    try:
        print(
            f"FOCUSED 16K EVALUATION -- {MODEL} @ "
            f"num_ctx={CONTEXT_SIZE}, "
            f"num_predict={config['reviewer_output_tokens']}"
        )
        print(f"cases: {[case['id'] for case in cases]}")
        print()

        for case in cases:
            print("-" * 72)
            print(
                f"case {case['id']} ({case['name']}) | "
                f"expected {case['expected']}"
            )
            sys.stdout.flush()

            record = run_case(
                config,
                case,
                adapter,
                sampler
            )

            records.append(record)

            with results_path.open("a") as handle:
                handle.write(
                    json.dumps(record) + "\n"
                )

            if record["deterministic_handled"]:
                print(
                    f"-> DETERMINISTIC "
                    f"{record['deterministic_verdict']} at "
                    f"{record['deterministic_stage']} stage "
                    f"(no model call) "
                    f"correct={record['correct']}"
                )

            else:
                print(
                    f"-> semantic status={record['status']} "
                    f"actual={record['actual']} "
                    f"correct={record['correct']} "
                    f"{record['latency_s']}s "
                    f"calls={record['ollama_calls']} "
                    f"pe={record['prompt_eval_count']} "
                    f"ec={record['eval_count']}"
                )

            if record["verdict_reason"]:
                print(
                    f"   reason: {record['verdict_reason'][:300]}"
                )

            sys.stdout.flush()

    finally:
        sampler.stop()
        sampler.join(timeout=10)

        summary = summarize(records) if records else "(no records)"

        print(summary)

        summary_path.write_text(summary + "\n")

        print()
        print(f"RESULTS:  {results_path}")
        print(f"SAMPLES:  {samples_path}")
        print(f"SUMMARY:  {summary_path}")
        print(
            f"RUNTIME:  {time.perf_counter() - started:.1f}s"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
