# Loop Engineering

Loop Engineering is a local, language-agnostic software engineering harness for specification-driven development using local LLMs.

It combines deterministic workflow controls with an agentic implementation loop powered by Ollama tool calling.

## What it does

- Resolves an authoritative specification
- Isolates the current work item from later queued work
- Generates and reviews tests
- Validates the candidate contract deterministically before freezing it
- Freezes a test contract before implementation
- Lets implementation challenge a demonstrably impossible frozen
  contract, under independent validation
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

A confirmed frozen-contract challenge raised during implementation
returns control to Test Contract, a bounded number of times.

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

## Frozen contract challenge

Frozen tests are immutable, and the implementation agent still cannot
modify one. But a contract can be wrong: it may compile, pass structural
and semantic review, and still be impossible to satisfy.

The implementation agent can therefore file a structured
`report_contract_issue` report and stop. A report never invalidates a
contract on its own:

1. deterministic evidence gate - the cited test names must exist in the
   frozen contract, the quoted production evidence must literally occur
   in the named file, and the claimed failure must reproduce right now;
2. prerequisites - a report is only considered once the agent has
   actually written an implementation and run the tests, so an
   unimplemented feature can never be reported as an impossible
   contract;
3. independent review - two independent reviewer verdicts must both
   CONFIRM, using a prompt that explicitly refuses "not implemented
   yet", "hard" and "I would have written it differently";
4. on confirmation, the Test Contract phase is reopened, the disproved
   contract is forbidden from being frozen again, and the confirmed
   defect is written to cross-attempt failure memory;
5. on rejection, the contract stands and implementation continues.

Everything is bounded: submissions per attempt, independent reviews per
attempt, and contract reopenings per spec attempt. Every failure path
fails closed - an unreadable, truncated or unparseable verdict keeps the
contract.

A deterministic stall reminder points at the procedure once the same
failure has survived several repair rounds, so a correct diagnosis turns
into a decision instead of being restated for the rest of the step
budget.

Whether the configured reviewer model actually adjudicates correctly is
measured, not assumed:

    python tests/manual_eval_contract_challenge.py

That runs two cases from the Ledger benchmark through the real reviewer:
an impossible contract (must CONFIRM) and the same contract with a
correct setup, merely unimplemented (must REJECT).

## Harness runtime state

Harness runtime state - resume state, history log, cross-attempt memory
- is owned by the harness, not by the target project, and lives outside
the repository under:

    runtime/projects/<project-name>-<path-hash>/

Nothing the harness writes for its own bookkeeping ever appears in the
target repository, so it cannot fail the clean-baseline check as an
untracked artifact, cannot survive `git restore` between attempts, and
cannot be swept into the automatic completion commit. The invariant is
asserted at startup rather than assumed.

Rollback after a failed attempt restores tracked files and removes
untracked artifacts the attempt created, leaving `git status --short`
empty. Files the project itself ignores (build output) are left alone.

A `.agent/` directory left inside a workspace by an older harness
version is reclaimed on the next run - its contents preserved under the
harness runtime directory - so a dirtied repository heals instead of
failing every subsequent attempt.

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
- Frozen tests cannot be modified, and a contract challenge is a
  report, never an edit
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
