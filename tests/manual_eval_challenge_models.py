"""
Challenge-adjudicator comparison: qwen3.5:9b vs gemma4:12b.

Reuses the real fixtures and the real `review_challenge` path from
tests/manual_eval_contract_challenge.py, overriding only the model in an
IN-MEMORY copy of the config. config.json is not touched.

In production `review_challenge` resolves its model as

    challenge_reviewer_model
        -> semantic_reviewer_model
            -> test_reviewer_model

and config.json sets no `challenge_reviewer_model`, so whichever model
wins the semantic-reviewer role automatically becomes the adjudicator too.
That is why this role has to be measured before swapping the model.

Run:

    python tests/manual_eval_challenge_models.py --reps 2
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.contract_challenge import (  # noqa: E402
    normalize_challenge,
    review_challenge,
)
from core.utils import load_json  # noqa: E402
from tests.manual_eval_contract_challenge import (  # noqa: E402
    CASES,
    EVIDENCE,
    TASK,
)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--reps",
        type=int,
        default=2
    )

    parser.add_argument(
        "--models",
        default="qwen3.5:9b,gemma4:12b"
    )

    parser.add_argument(
        "--out",
        default=str(
            REPO_ROOT
            / "tests/reviewer_model_eval/results/challenge-results.jsonl"
        )
    )

    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base = load_json(REPO_ROOT / "config.json")

    for model in [
        name.strip()
        for name in args.models.split(",")
        if name.strip()
    ]:
        config = dict(base)
        config["challenge_reviewer_model"] = model

        for case in CASES:
            challenge, error = normalize_challenge(
                case["args"]
            )

            if error:
                raise SystemExit(
                    f"fixture is malformed: {error}"
                )

            for repetition in range(1, args.reps + 1):
                print()
                print("=" * 68)
                print(
                    f"{model} | {case['name']} | rep {repetition} "
                    f"| expected {case['expected']}"
                )
                print("=" * 68)

                started = time.perf_counter()

                review = review_challenge(
                    config,
                    TASK,
                    case["contract"],
                    case["production"],
                    challenge,
                    EVIDENCE
                )

                latency = time.perf_counter() - started

                actual = (
                    "CONFIRM"
                    if review["confirmed"]
                    else "REJECT"
                )

                record = {
                    "model": model,
                    "case": case["name"],
                    "repetition": repetition,
                    "expected": case["expected"],
                    "actual": actual,
                    "correct": actual == case["expected"],
                    "latency_s": round(latency, 2),
                    "reasons": review["reasons"],
                    "verdicts": [
                        {
                            "reviewer": item["reviewer"],
                            "decision": item["decision"],
                            "status": item["status"]
                        }
                        for item in review["reviews"]
                    ],
                }

                with out_path.open("a") as handle:
                    handle.write(
                        json.dumps(record) + "\n"
                    )

                print(
                    f"-> actual={actual} "
                    f"correct={record['correct']} "
                    f"{record['latency_s']}s"
                )
                print(
                    json.dumps(
                        record["verdicts"],
                        indent=2
                    )
                )

    print()
    print(f"RESULTS: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
