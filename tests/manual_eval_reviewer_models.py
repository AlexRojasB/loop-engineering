"""
Reviewer-model comparison: qwen3.5:9b vs gemma4:12b on the REAL semantic
contract-review path.

NOT a unit test. Excluded from `unittest discover` by its filename.

What this exercises
-------------------

Nothing in core/ or prompts/ is modified, imported-and-patched, or
re-implemented. Each measurement is:

    semantic_test_review_prompt(...)   -- production prompt rendering
    _resolve_reviewer_verdict(...)     -- production dispatch, thinking
                                          logging, truncation handling,
                                          schema validation and the one
                                          bounded schema repair
    core.models.call_model -> ollama   -- production Ollama API call

with the production reviewer settings from config.json:

    num_ctx      = reviewer_context_size    (16384)
    num_predict  = reviewer_output_tokens   (2048)
    think        = semantic_reviewer_thinking (true)
    json_mode    = True
    timeout      = model_timeout_seconds

Only two things differ from a production run, and neither touches
production code or configuration:

1. `history_file` is redirected to the scratchpad, so the real run
   history is not contaminated. Observability events are then read back
   from that file to detect schema-invalid verdicts and schema repairs.
2. `urllib.request.urlopen` is wrapped IN THIS PROCESS ONLY to record the
   raw Ollama response body before handing the identical bytes to
   core.models. core.models discards prompt_eval_count/eval_count, so
   this is the only way to report token counts without editing it. The
   production code path executes byte-for-byte unchanged.

`spec_memory` is deliberately absent from the config, so
record_spec_failure() is a no-op (memory_from_config returns None) and no
benchmark repository or memory file is touched.

Run:

    python tests/manual_eval_reviewer_models.py --reps 3
"""

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.phases.test_contract_phase import (  # noqa: E402
    _resolve_reviewer_verdict,
    semantic_test_review_prompt,
)
from core.authorized_future import (  # noqa: E402
    format_authorized_future,
    authorized_future_entries,
)
from core.phases.semantic_audit import audit_substance  # noqa: E402
from core.utils import load_json  # noqa: E402
from tests.reviewer_model_eval.fixtures import (  # noqa: E402
    CASES,
    CONTRACT_PATH,
    PRODUCTION_PATH,
)


MODELS = [
    "qwen3.5:9b",
    "gemma4:12b",
]


# ---------------------------------------------------------------------------
# Non-invasive raw-response capture
# ---------------------------------------------------------------------------

RAW_RESPONSES = []

_real_urlopen = urllib.request.urlopen


class _ReplayedResponse:
    """Hands core.models the exact bytes the server returned."""

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capturing_urlopen(request, *args, **kwargs):
    with _real_urlopen(request, *args, **kwargs) as response:
        data = response.read()

    try:
        RAW_RESPONSES.append(
            json.loads(data.decode())
        )

    except Exception:
        RAW_RESPONSES.append({})

    return _ReplayedResponse(data)


urllib.request.urlopen = _capturing_urlopen


# ---------------------------------------------------------------------------
# Hardware sampling
# ---------------------------------------------------------------------------

def _run(command):
    try:
        return subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        ).stdout.strip()

    except Exception as exc:
        return f"<{type(exc).__name__}: {exc}>"


def hardware_snapshot(label):
    return {
        "label": label,
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "ollama_ps": _run("ollama ps"),
        "free_h": _run("free -h"),
        "nvidia_smi": _run("nvidia-smi"),
    }


def hardware_sample():
    """Compact numeric sample, cheap enough to take every few seconds."""

    mem = _run(
        "free -m | awk '/^Mem:/ {print $3, $7} /^Swap:/ {print $3}'"
    ).split()

    gpu = _run(
        "nvidia-smi --query-gpu=memory.used,temperature.gpu,"
        "utilization.gpu --format=csv,noheader,nounits"
    ).split(",")

    ps = _run(
        "ollama ps --format json 2>/dev/null || ollama ps"
    )

    sample = {
        "timestamp": time.time(),
        "raw_ollama_ps": ps,
    }

    try:
        sample["mem_used_mb"] = int(mem[0])
        sample["mem_available_mb"] = int(mem[1])
        sample["swap_used_mb"] = int(mem[2])

    except Exception:
        pass

    try:
        sample["gpu_mem_used_mb"] = int(gpu[0].strip())
        sample["gpu_temp_c"] = int(gpu[1].strip())
        sample["gpu_util_pct"] = int(gpu[2].strip())

    except Exception:
        pass

    return sample


class Sampler(threading.Thread):
    def __init__(self, interval, out_path):
        super().__init__(daemon=True)
        self.interval = interval
        self.out_path = out_path
        self._stop_event = threading.Event()
        self.samples = []
        self.label = "idle"

    def run(self):
        while not self._stop_event.is_set():
            sample = hardware_sample()
            sample["label"] = self.label
            self.samples.append(sample)

            with open(self.out_path, "a") as handle:
                handle.write(
                    json.dumps(sample) + "\n"
                )

            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()


def summarize_samples(samples, predicate):
    selected = [
        sample
        for sample in samples
        if predicate(sample)
    ]

    if not selected:
        return None

    def peak(key, reducer=max):
        values = [
            sample[key]
            for sample in selected
            if key in sample
        ]

        return reducer(values) if values else None

    return {
        "samples": len(selected),
        "mem_used_mb_peak": peak("mem_used_mb"),
        "mem_available_mb_min": peak(
            "mem_available_mb",
            min
        ),
        "swap_used_mb_peak": peak("swap_used_mb"),
        "gpu_mem_used_mb_peak": peak("gpu_mem_used_mb"),
        "gpu_temp_c_peak": peak("gpu_temp_c"),
        "gpu_util_pct_peak": peak("gpu_util_pct"),
    }


# ---------------------------------------------------------------------------
# Eval config
# ---------------------------------------------------------------------------

def build_config(scratch_dir, model):
    """
    The production reviewer configuration, with history redirected.

    Note what is NOT here: no "spec_memory" key, so record_spec_failure()
    short-circuits and writes nothing anywhere.
    """

    config = dict(
        load_json(REPO_ROOT / "config.json")
    )

    config["history_file"] = str(
        scratch_dir / f"history-{model.replace(':', '_')}.jsonl"
    )

    return config


def read_new_history(path, offset):
    file = Path(path)

    if not file.exists():
        return [], offset

    with file.open() as handle:
        handle.seek(offset)
        text = handle.read()
        new_offset = handle.tell()

    events = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            events.append(json.loads(line))

        except json.JSONDecodeError:
            pass

    return events, new_offset


def summarize_issues(issues, limit=3):
    if not issues:
        return ""

    parts = []

    for issue in issues[:limit]:
        if isinstance(issue, str):
            text = issue

        elif isinstance(issue, dict):
            text = (
                issue.get("issue")
                or issue.get("description")
                or issue.get("message")
                or json.dumps(issue)
            )

        else:
            text = str(issue)

        parts.append(
            " ".join(str(text).split())
        )

    joined = " | ".join(parts)

    if len(issues) > limit:
        joined += f" | (+{len(issues) - limit} more)"

    return joined


def run_one(config, model, case, repetition, history_path, sampler):
    entries = authorized_future_entries(
        {
            "expected_red": case["authorized_future"]
        }
    )

    prompt = semantic_test_review_prompt(
        case["task"],
        {PRODUCTION_PATH: case["production"]},
        case["contract"],
        prior_issues=[],
        prior_spec_failures=None,
        authorized_future=format_authorized_future(entries)
    )

    # The same deterministic evidence the phase hands the reviewer, so
    # the offline run exercises the authorized-symbol cross-check too.
    authorized_symbols = [
        entry["symbol"]
        for entry in entries
    ]

    _, offset = read_new_history(history_path, 0)
    raw_before = len(RAW_RESPONSES)

    if sampler is not None:
        sampler.label = f"{model}|case{case['id']}|rep{repetition}"

    started = time.perf_counter()

    outcome = _resolve_reviewer_verdict(
        config,
        model,
        prompt,
        "semantic",
        CONTRACT_PATH,
        repetition,
        config.get(
            "semantic_reviewer_thinking",
            False
        ),
        config.get(
            "reviewer_context_size",
            16384
        ),
        config.get(
            "reviewer_output_tokens",
            2048
        ),
        authorized_symbols=authorized_symbols
    )

    latency = time.perf_counter() - started

    if sampler is not None:
        sampler.label = "idle"

    events, _ = read_new_history(history_path, offset)
    raws = RAW_RESPONSES[raw_before:]

    event_names = [
        event.get("event")
        for event in events
    ]

    reasoning = [
        event
        for event in events
        if event.get("event") == "test_review_reasoning"
    ]

    status = outcome["status"]
    decision = outcome.get("decision")

    schema_invalid = (
        "reviewer_schema_invalid" in event_names
    )

    repair_events = [
        event
        for event in events
        if event.get("event") == "reviewer_schema_repair"
    ]

    repair_outcome = (
        repair_events[-1]["data"].get("outcome")
        if repair_events
        else None
    )

    first = raws[0] if raws else {}

    # What the model's own response carried, independent of whether the
    # harness could use it. This is how the redesign gets measured: an
    # APPROVE with no audit is a different failure from an APPROVE with
    # an audit that convicts the contract.
    # core.models falls back to the `thinking` channel when `response`
    # comes back empty, so the audit metrics must read the same text the
    # production path actually parsed -- qwen3.5 with think=True emits
    # the whole audit into `thinking` and leaves `response` blank.
    raw_text = (
        first.get("response")
        or first.get("thinking")
        or ""
    )

    try:
        raw_parsed = json.loads(raw_text)

    except Exception:
        raw_parsed = None

    stated_decision = None
    audit_entries = 0
    audit_present = False

    if isinstance(raw_parsed, dict):
        stated = raw_parsed.get("decision")

        if isinstance(stated, str):
            stated_decision = stated.upper()

        audit_present = isinstance(raw_parsed.get("audit"), dict)
        audit_entries = audit_substance(raw_parsed)

    audit_absent_event = "reviewer_audit_absent" in event_names
    audit_unusable_event = "reviewer_audit_unusable" in event_names

    correct = (
        decision == case["expected"]
        if status == "ok"
        else False
    )

    return {
        "model": model,
        "case": case["id"],
        "case_name": case["name"],
        "group": case["group"],
        "repetition": repetition,
        "expected": case["expected"],
        "actual": decision if status == "ok" else None,
        "status": status,
        "correct": correct,
        "latency_s": round(latency, 2),
        "schema_valid": not schema_invalid,
        "audit_present": audit_present,
        "audit_entries": audit_entries,
        "audit_absent": audit_absent_event,
        "audit_unusable": audit_unusable_event,
        "stated_decision": stated_decision,
        "verdict_derived": (
            status == "ok"
            and stated_decision is not None
            and decision != stated_decision
        ),
        "schema_repair_required": bool(repair_events),
        "schema_repair_outcome": repair_outcome,
        "timeout": status == "call_failed"
                   and not raws,
        "truncated": status == "truncated",
        "call_failed": status == "call_failed",
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
        "thinking_field_returned": bool(reasoning),
        "thinking_chars": (
            len(reasoning[-1]["data"].get("thinking") or "")
            if reasoning
            else 0
        ),
        "issue_summary": summarize_issues(
            outcome.get("issues") or []
        ),
        "issues": outcome.get("issues") or [],
        "ollama_calls": len(raws),
        "prompt_eval_count": first.get("prompt_eval_count"),
        "eval_count": first.get("eval_count"),
        "prompt_eval_duration_s": (
            round(first["prompt_eval_duration"] / 1e9, 2)
            if first.get("prompt_eval_duration")
            else None
        ),
        "eval_duration_s": (
            round(first["eval_duration"] / 1e9, 2)
            if first.get("eval_duration")
            else None
        ),
        "load_duration_s": (
            round(first["load_duration"] / 1e9, 2)
            if first.get("load_duration")
            else None
        ),
        "done_reason": first.get("done_reason"),
        "raw_response": raw_text,
        "raw_thinking": first.get("thinking"),
        "prompt_chars": len(prompt),
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--reps",
        type=int,
        default=3
    )

    parser.add_argument(
        "--models",
        default=",".join(MODELS)
    )

    parser.add_argument(
        "--cases",
        default=""
    )

    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "tests/reviewer_model_eval/results")
    )

    parser.add_argument(
        "--sample-interval",
        type=float,
        default=5.0
    )

    args = parser.parse_args()

    scratch = Path(args.out)
    scratch.mkdir(parents=True, exist_ok=True)

    models = [
        name.strip()
        for name in args.models.split(",")
        if name.strip()
    ]

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

    results_path = scratch / f"results-{stamp}.jsonl"
    samples_path = scratch / f"samples-{stamp}.jsonl"
    hardware_path = scratch / f"hardware-{stamp}.json"

    sampler = Sampler(
        args.sample_interval,
        samples_path
    )
    sampler.start()

    hardware = []
    results = []

    run_started = time.perf_counter()

    try:
        for model in models:
            print()
            print("=" * 72)
            print(f"MODEL BATCH: {model}")
            print("=" * 72)

            snapshot = hardware_snapshot(
                f"before:{model}"
            )
            hardware.append(snapshot)

            print(snapshot["ollama_ps"])
            print(snapshot["free_h"])

            config = build_config(scratch, model)
            history_path = config["history_file"]

            model_started = time.perf_counter()

            for case in cases:
                for repetition in range(1, args.reps + 1):
                    print()
                    print("-" * 72)
                    print(
                        f"{model} | case {case['id']} "
                        f"({case['name']}) | rep {repetition} | "
                        f"expected {case['expected']}"
                    )
                    print("-" * 72)

                    record = run_one(
                        config,
                        model,
                        case,
                        repetition,
                        history_path,
                        sampler
                    )

                    results.append(record)

                    with results_path.open("a") as handle:
                        handle.write(
                            json.dumps(record) + "\n"
                        )

                    print(
                        f"-> status={record['status']} "
                        f"actual={record['actual']} "
                        f"correct={record['correct']} "
                        f"{record['latency_s']}s "
                        f"prompt_tokens="
                        f"{record['prompt_eval_count']} "
                        f"eval_tokens={record['eval_count']} "
                        f"thinking={record['thinking_chars']}c"
                    )

                    if record["issue_summary"]:
                        print(
                            f"   issues: "
                            f"{record['issue_summary'][:400]}"
                        )

            hardware.append(
                hardware_snapshot(f"after:{model}")
            )

            print()
            print(
                f"{model} batch runtime: "
                f"{time.perf_counter() - model_started:.1f}s"
            )

    finally:
        sampler.stop()
        sampler.join(timeout=10)

        total = time.perf_counter() - run_started

        with hardware_path.open("w") as handle:
            json.dump(
                {
                    "snapshots": hardware,
                    "per_model_load": {
                        model: summarize_samples(
                            sampler.samples,
                            lambda sample, m=model:
                                sample.get("label", "").startswith(m)
                        )
                        for model in models
                    },
                    "total_runtime_s": round(total, 1),
                },
                handle,
                indent=2
            )

    print()
    print("=" * 72)
    print(f"RESULTS: {results_path}")
    print(f"SAMPLES: {samples_path}")
    print(f"HARDWARE: {hardware_path}")
    print(f"TOTAL RUNTIME: {total:.1f}s")
    print("=" * 72)

    for model in models:
        subset = [
            record
            for record in results
            if record["model"] == model
        ]

        if not subset:
            continue

        latencies = [
            record["latency_s"]
            for record in subset
        ]

        print()
        print(f"{model}:")
        print(
            f"  accuracy      "
            f"{sum(1 for r in subset if r['correct'])}/{len(subset)}"
        )
        print(
            f"  false APPROVE "
            f"{sum(1 for r in subset if r['false_approve'])}"
        )
        print(
            f"  false REJECT  "
            f"{sum(1 for r in subset if r['false_reject'])}"
        )
        print(
            f"  mean latency  "
            f"{statistics.mean(latencies):.1f}s"
        )
        print(
            f"  median        "
            f"{statistics.median(latencies):.1f}s"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
