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

## Authorized future API

The contract gate decides, deterministically, whether a symbol the
compiler reports as missing is one the CURRENT specification asked for.
That verdict is now handed to the structural reviewer, the semantic
reviewer and the revision prompt as structured evidence:

    - `Description`
      authorized because: requested by the current specification
      compiler evidence of current absence: CS1061: ...

Reviewers are told this classification is machine evidence and that mere
absence is not theirs to re-litigate. They may still reject the contract
for any other defect, and a symbol the specification never asked for gets
no protection at all -- it is still an invented API.

## Intrinsic test-source defects

A test-first contract is compiled while the requested future API is still
missing, and compilers suppress cascading diagnostics inside an
expression whose type could not be resolved. A defect written inside such
an expression is therefore invisible at gate time and only surfaces once
production implements the future API -- against a contract that is
already frozen.

So the harness also analyses the generated test source directly, with no
compiler involved, through `LanguageAdapter.analyze_test_source`. The
.NET adapter detects void-returning assertion helpers combined with a
boolean operator. This check runs before compilation and before any model
reviewer, and runs even when no toolchain is available.

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

The harness can also raise a challenge itself. When production compiles,
the frozen test file does not, every diagnostic is inside files the agent
may not edit, and the same diagnostics reproduce across repair rounds,
the harness files a `frozen_test_compilation` report on its own
initiative -- because the implementation model cannot be relied on to do
it. Escalation grants nothing: it consumes the same budgets and goes
through the same independent adjudication, and two reviewers must still
confirm. An ordinary failing assertion never escalates.

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

Every finding records HOW it was established, and prompts present the
tiers differently:

- deterministic checks and confirmed contract challenges are stated as
  established evidence;
- a single reviewer's unconfirmed opinion is stated as a hypothesis to
  re-test, because reviewers have previously "found" defects that were
  in fact task-authorized future API;
- transient model/service failures are stored for observability and
  never rendered into a prompt.

When memory has to be trimmed, the least authoritative entries are
evicted first, so a burst of model opinions cannot push out the
machine-verified finding that explains the failure.

## Optional shutdown when all work is finished

For long unattended runs on a dedicated machine, the harness can power
the machine off once the ENTIRE requested workload has reached a
controlled terminal state.

It is opt-in. Without `--shutdown-when-done` nothing here can ever run:

    python agent.py . --spec-dir specs/queue                  # never powers off
    python agent.py . --spec-dir specs/queue --shutdown-when-done

    --shutdown-when-done      enable automatic power-off (default off)
    --no-shutdown-when-done   explicitly disable, overriding config
    --shutdown-delay SECONDS  delay before power-off (default 60)
    --shutdown-dry-run        run the whole decision path, never power off

Config equivalents are `shutdown_when_done`, `shutdown_delay_seconds`
and `shutdown_dry_run`. An explicit CLI flag always wins, in both
directions.

Shutdown is the LAST side effect of a successful finalization path, and
everything about it fails toward staying powered on. Power-off is only
reached when the run ended at an explicitly handled terminal state, the
orchestrator has no remaining work, repository finalization succeeded,
and both audit events are already on disk:

    run_finished        result, run mode, completed/total work,
                        failure reason, shutdown_when_done, timestamp
    shutdown_requested  reason: run_terminal_and_idle

An interrupt, an unexpected exception, a failed commit, an unclean
rollback or an unwritable history file all leave the machine ON.

For `--spec-dir`, only the MULTI-SPEC ORCHESTRATOR's own terminal
result counts -- never an individual spec. A queue that stops at 4/8
because a spec exhausted its retries is still terminal: this invocation
will do no further work, so an opted-in operator gets a power-off once
rollback came back clean.

The decision, the audit trail and the OS call live in `core/power.py`
(`ShutdownSettings`, `WorkloadResult`, `ShutdownPolicy`,
`ShutdownController`, `RunFinalizer`, `LinuxPowerOffExecutor`), not
scattered through the pipeline. The executor is argv-based, never
shell-interpreted, embeds no password, and is mockable; the request is
latched so at most one real power-off happens per run.
`WorkloadResult.has_remaining_work()` is the single predicate a future
job queue replaces.

Real power-off runs `/usr/bin/systemctl poweroff` and requires a
one-time OS authorization for the account running the harness; the
harness never edits sudoers or polkit itself. Validate with
`--shutdown-dry-run` first.

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
