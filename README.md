# Loop Engineering

Loop Engineering is a local, language-agnostic software engineering harness for specification-driven development using local LLMs.

It combines deterministic workflow controls with an agentic implementation loop powered by Ollama tool calling.

## What it does

- Resolves an authoritative specification
- Generates and reviews tests
- Freezes a test contract before implementation
- Confirms an expected RED state
- Runs an agentic implementation loop
- Restricts production writes to authorized files
- Runs deterministic build and test validation
- Performs a final review
- Supports multi-spec execution and resume state

## Architecture

Specification
-> Planning
-> Test Contract
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

## Safety model

The agentic phase is intentionally constrained:

- Specifications cannot be modified
- Frozen tests cannot be modified
- Writes are restricted to planner-authorized production files
- Commands are allow-listed
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
