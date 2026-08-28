import json
import os
import re
import subprocess
import urllib.request

from core.project_runtime import harness_project_runtime_dir

OLLAMA_URL = "http://localhost:11434/api/generate"


def ollama_generate(model, prompt):
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False
    }).encode()

    request = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(request) as response:
        result = json.loads(response.read().decode())

    return result["response"]


def sanitize_code(text):
    text = text.strip()

    text = re.sub(
        r"^```(?:csharp|cs|python|typescript|javascript|java|rust)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(r"\s*```$", "", text)

    return text.strip() + "\n"


def run_commands(project_path, commands):
    results = []

    for command in commands:
        process = subprocess.run(
            command,
            cwd=project_path,
            shell=True,
            capture_output=True,
            text=True
        )

        output = process.stdout + process.stderr

        results.append({
            "command": command,
            "exit_code": process.returncode,
            "output": output
        })

        if process.returncode != 0:
            return False, results

    return True, results


def make_feedback(results):
    return "\n\n".join(
        f"Command: {r['command']}\n"
        f"Exit code: {r['exit_code']}\n"
        f"{r['output']}"
        for r in results
    )


def normalize_failure(results):
    signatures = []

    for result in results:
        output = result["output"]

        compiler_codes = re.findall(
            r"\b(?:CS|NETSDK)\d+\b",
            output
        )

        test_names = re.findall(
            r"[A-Za-z0-9_.]+Tests\.[A-Za-z0-9_]+",
            output
        )

        signatures.extend(compiler_codes)
        signatures.extend(test_names)

    return "|".join(sorted(set(signatures)))


def build_repair_prompt(spec, current_code, feedback):
    return f"""
You are repairing an existing software implementation.

SPECIFICATION:

{spec}

CURRENT IMPLEMENTATION:

{current_code}

VALIDATION OR REVIEW FAILURE:

{feedback}

The deterministic validation and review feedback above are authoritative.

Your job is to make the implementation satisfy the specification
and pass all validation.

Rules:

1. Diagnose every reported issue.
2. Fix root causes, not symptoms.
3. Preserve behavior that is already correct.
4. Do not modify or weaken tests.
5. Do not remove required functionality.
6. Do not introduce unrelated functionality.
7. Preserve all specification requirements.

Return the COMPLETE corrected target source file.

Output source code only.
Do not include Markdown fences.
Do not explain your answer.
"""


def review_implementation(model, spec, code):
    prompt = f"""
You are reviewing an implementation against a specification.

SPECIFICATION:

{spec}

IMPLEMENTATION:

{code}

Review for:
- missing requirements
- incorrect behavior
- contradictions with the specification
- unnecessary functionality
- unsafe or suspicious implementation choices
- issues not necessarily caught by build or tests

Do not invent problems.

Return EXACTLY this structure:

DECISION: APPROVE or REJECT

ISSUES:
- issue 1
- issue 2

If there are no real issues, write:

DECISION: APPROVE

ISSUES:
- none
"""

    return ollama_generate(model, prompt)


def parse_review(review):
    upper = review.upper()

    if "DECISION: APPROVE" in upper:
        return True

    return False


def main():
    with open("config.json") as f:
        config = json.load(f)

    with open("SPEC.md") as f:
        spec = f.read()

    project_path = config["project_path"]

    target_path = os.path.join(
        project_path,
        config["target_file"]
    )

    repair_model = config["repair_model"]
    escalation_model = config["escalation_model"]
    reviewer_model = config["reviewer_model"]

    max_iterations = config["max_iterations"]
    validation_commands = config["validation_commands"]

    previous_signature = None
    stuck_count = 0
    feedback = None

    for iteration in range(1, max_iterations + 1):

        print()
        print("=" * 60)
        print(f"CYCLE {iteration}")
        print("=" * 60)

        print()
        print("Running deterministic validation...")

        success, results = run_commands(
            project_path,
            validation_commands
        )

        for result in results:
            print()
            print(f"$ {result['command']}")
            print(result["output"])

        if not success:
            feedback = make_feedback(results)
            signature = normalize_failure(results)

            print()
            print(f"Failure signature: {signature}")

            if (
                previous_signature is not None
                and signature == previous_signature
            ):
                stuck_count += 1
                print(
                    f"Same failure detected. "
                    f"Stuck count: {stuck_count}"
                )
            else:
                stuck_count = 0

            previous_signature = signature

        else:
            print()
            print("Deterministic validation passed.")
            print("Calling reviewer...")

            with open(target_path) as f:
                current_code = f.read()

            review = review_implementation(
                reviewer_model,
                spec,
                current_code
            )

            print()
            print("=" * 60)
            print("REVIEW")
            print("=" * 60)
            print(review)

            review_passed = parse_review(review)

            if review_passed:
                print()
                print("=" * 60)
                print("FULL PIPELINE PASSED")
                print("=" * 60)
                print("Build/tests: PASS")
                print("Reviewer: APPROVE")
                return

            print()
            print("Reviewer rejected implementation.")

            feedback = f"""
REVIEWER FEEDBACK:

{review}
"""

            previous_signature = None
            stuck_count = 0

        with open(target_path) as f:
            current_code = f.read()

        if stuck_count >= 1:
            model = escalation_model
            role = "ESCALATION"
        else:
            model = repair_model
            role = "REPAIR"

        print()
        print(f"Role:  {role}")
        print(f"Model: {model}")
        print("Calling model...")

        prompt = build_repair_prompt(
            spec,
            current_code,
            feedback
        )

        response = ollama_generate(
            model,
            prompt
        )

        code = sanitize_code(response)

        # Per-cycle attempt history is HARNESS runtime state, not part
        # of the user's project. Writing it into the target repository
        # leaves untracked artifacts behind that survive rollback and
        # break the next attempt's clean-baseline check.
        attempt_path = os.path.join(
            str(
                harness_project_runtime_dir(
                    project_path
                )
                / "cycles"
            ),
            f"cycle-{iteration}.txt"
        )

        os.makedirs(
            os.path.dirname(
                attempt_path
            ),
            exist_ok=True
        )

        with open(attempt_path, "w") as f:
            f.write(code)

        with open(target_path, "w") as f:
            f.write(code)

    print()
    print("=" * 60)
    print(
        f"FAILED TO CONVERGE AFTER "
        f"{max_iterations} CYCLES"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
