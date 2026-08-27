# Loop Engineering

Loop Engineering is a local, language-agnostic software engineering harness for specification-driven development using local LLMs.

It combines deterministic workflow controls with an agentic implementation loop powered by Ollama tool calling.

## What it does

- Resolves an authoritative specification
- Isolates the current work item from later queued work
- Generates and reviews tests
- Validates the candidate contract deterministically before freezing it
- Freezes a test contract before implementation
- Confirms an expected RED state
- Runs an agentic implementation loop
- Restricts production writes to authorized files
- Runs deterministic build and test validation
- Performs a final review
- Supports multi-spec execution and resume state

## Architecture

Specification
-> Work Isolation
-> Planning
-> Test Contract (deterministic validation + review)
-> Expected RED
-> Agentic Implementation
-> Deterministic Build
-> Deterministic Tests
-> Final Review

The implementation agent can inspect files, edit authorized production files, run build/test commands, inspect failures, and iterate until GREEN.

Frozen tests and specifications cannot be modified by the implementation agent.

## Current model setup

The current tested configuration includes:

- qwen3:4b-instruct-2507-q4_K_M
- qwen2.5-coder:7b-instruct-q4_K_M
- qwen3.5:4b
- qwen3.5:9b

The current agentic implementation model is qwen3.5:4b.

Models are configurable in config.json.

## Requirements

- Python 3
- Git
- Ollama
- Compatible local models
- Build/test tooling for the target repository

The current language adapter supports .NET projects.

## Setup

Create a virtual environment:

    python3 -m venv .venv
    source .venv/bin/activate

Make sure Ollama is running and verify your models:

    ollama list

## Running a specification

From the target repository:

    python -u ~/loop-engineering/agent.py . --spec specs/queue/example.md

To save output:

    python -u ~/loop-engineering/agent.py . --spec specs/queue/example.md | tee ~/loop-engineering/run-example.txt

## Work isolation

During a queued run, only the CURRENT work item is authoritative.

Later queued items are excluded from planner, test-generation, reviewer
and implementation context, and are neither listed nor readable through
the agentic file tools. Production and test sources are never restricted:
behaviour delivered by earlier work stays available through committed
repository state.

A work item may re-admit a specific document explicitly:

    ## Depends On

    - docs/architecture.md

## Contract validation

Before an expensive semantic review, the harness compiles the candidate
test contract and asks the language adapter to classify the resulting
diagnostics. That separates:

- the requested future API not existing yet (legitimate expected RED)
- proof that the contract itself is wrong (an existing API misused, an
  API the current specification never requested, or invalid syntax)

The second category is rejected before freezing. Classification lives
behind the adapter (`classify_contract_diagnostics`), so other languages
can supply their own without changing the pipeline. When an adapter
cannot classify, the contract proceeds to the existing reviewers exactly
as before — the check only ever short-circuits on positive evidence.

## Cross-attempt failure memory

Repository state is restored between failed outer attempts at a work
item. A bounded, condensed record of *why* previous attempts failed
survives that restore and is fed back into test generation, revision and
review, so the same defect is not rediscovered from scratch.

The memory is scoped to one work item (keyed by path and content hash),
capped in both entry count and entry length, cleared when the item
finishes, and stored outside the target repository so rollback, clean
baseline checks and automatic commits are unaffected.

## Safety model

The agentic phase is intentionally constrained:

- Specifications cannot be modified
- Later queued work items cannot be listed or read
- Frozen tests cannot be modified
- Writes are restricted to planner-authorized production files
- Commands are structured operations, never arbitrary shell
- Build and tests are independently re-run by the harness
- Repository state is checked before execution

## Current status

Loop Engineering is experimental and under active development.

Current focus areas include:

- Reducing test-contract latency
- Improving context efficiency
- Expanding language adapters
- Improving multi-spec execution
- Better observability and timing
- Benchmarking local models
- Strengthening resume and recovery behavior

## License

MIT
