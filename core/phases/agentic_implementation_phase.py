import json
import subprocess
import urllib.request
from pathlib import Path

from core.state import (
    append_history,
    mark_phase_completed,
    mark_phase_started,
    save_state,
)


DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MAX_STEPS = 40
DEFAULT_CONTEXT = 16384


def _safe_path(root, requested):
    path = (
        root / requested
    ).resolve()

    if (
        path != root
        and root not in path.parents
    ):
        raise ValueError(
            "Path escapes workspace."
        )

    return path


def _read_file(root, path):
    target = _safe_path(
        root,
        path
    )

    return target.read_text()


def _write_file(
    root,
    path,
    content,
    writable_paths
):
    if path not in writable_paths:
        return (
            "WRITE REJECTED: "
            f"{path} is not an authorized "
            "implementation target. "
            "Frozen tests and specifications "
            "must not be modified."
        )

    target = _safe_path(
        root,
        path
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    target.write_text(
        content
    )

    return f"Wrote {path}"


def _list_files(
    root,
    requested_path=None
):
    start = root

    if requested_path:
        candidate = _safe_path(
            root,
            requested_path
        )

        if candidate.is_dir():
            start = candidate

    result = []

    for path in start.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(
            root
        )

        if any(
            part in {
                ".git",
                "bin",
                "obj",
                ".agent",
            }
            for part in relative.parts
        ):
            continue

        result.append(
            str(relative)
        )

    return "\n".join(
        sorted(result)
    )


def _run_command(
    root,
    command,
    build_command,
    test_command
):
    allowed = {
        build_command,
        test_command,
        "git status --short",
        "git diff",
    }

    if command not in allowed:
        return (
            "COMMAND REJECTED.\n"
            "Allowed commands:\n- "
            + "\n- ".join(
                sorted(allowed)
            )
        )

    result = subprocess.run(
        command,
        cwd=root,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=240
    )

    output = result.stdout

    if len(output) > 16000:
        output = (
            output[:8000]
            + "\n...[TRUNCATED]...\n"
            + output[-8000:]
        )

    return (
        f"exit_code={result.returncode}\n"
        f"{output}"
    )


def _tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description":
                    "Read a text file from "
                    "the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type":
                                "string"
                        }
                    },
                    "required": [
                        "path"
                    ]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description":
                    "Replace the complete "
                    "contents of an authorized "
                    "production file. Frozen "
                    "tests and specifications "
                    "cannot be modified.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type":
                                "string"
                        },
                        "content": {
                            "type":
                                "string"
                        }
                    },
                    "required": [
                        "path",
                        "content"
                    ]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description":
                    "List repository files, "
                    "optionally below a path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type":
                                "string"
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description":
                    "Run the project build, "
                    "project tests, or an "
                    "allowed Git inspection.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type":
                                "string"
                        }
                    },
                    "required": [
                        "command"
                    ]
                }
            }
        }
    ]


def _call_model(
    model,
    ollama_url,
    context_size,
    messages
):
    payload = {
        "model":
            model,

        "messages":
            messages,

        "tools":
            _tools(),

        "stream":
            False,

        "options": {
            "num_ctx":
                context_size
        }
    }

    request = urllib.request.Request(
        ollama_url,
        data=json.dumps(
            payload
        ).encode(),
        headers={
            "Content-Type":
                "application/json"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=420
    ) as response:
        return json.loads(
            response.read()
        )


def _execute_tool(
    root,
    call,
    writable_paths,
    build_command,
    test_command
):
    function = call[
        "function"
    ]

    name = function[
        "name"
    ]

    args = function.get(
        "arguments",
        {}
    )

    print()
    print(
        f">>> AGENTIC TOOL: {name}"
    )

    print(
        json.dumps(
            args,
            indent=2
        )
    )

    try:
        if name == "read_file":
            return _read_file(
                root,
                args["path"]
            )

        if name == "write_file":
            return _write_file(
                root,
                args["path"],
                args["content"],
                writable_paths
            )

        if name == "list_files":
            return _list_files(
                root,
                args.get(
                    "path"
                )
            )

        if name == "run_command":
            return _run_command(
                root,
                args["command"],
                build_command,
                test_command
            )

        return (
            f"Unknown tool: {name}"
        )

    except Exception as exc:
        return (
            "TOOL ERROR: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )


def _validate_final_state(
    root,
    build_command,
    test_command
):
    print()
    print(
        "Agent stopped. Performing "
        "deterministic agentic-phase "
        "validation."
    )

    build = _run_command(
        root,
        build_command,
        build_command,
        test_command
    )

    print()
    print("AGENTIC FINAL BUILD:")
    print(build)

    if not build.startswith(
        "exit_code=0"
    ):
        return False

    tests = _run_command(
        root,
        test_command,
        build_command,
        test_command
    )

    print()
    print("AGENTIC FINAL TESTS:")
    print(tests)

    return tests.startswith(
        "exit_code=0"
    )


def run_agentic_implementation_phase(
    config,
    workspace,
    task,
    state,
    implementation_changes,
    build_command,
    test_command
):
    print()
    print("=" * 60)
    print(
        "PHASE 4 - AGENTIC IMPLEMENTATION"
    )
    print("=" * 60)

    mark_phase_started(
        config,
        state,
        "implementation"
    )

    root = Path(
        workspace
    ).resolve()

    writable_paths = {
        change["path"]
        for change
        in implementation_changes
    }

    if not writable_paths:
        print(
            "No authorized implementation "
            "targets."
        )
        return False

    model = config.get(
        "agentic_model",
        DEFAULT_MODEL
    )

    ollama_url = config.get(
        "ollama_url",
        DEFAULT_OLLAMA_URL
    )

    max_steps = int(
        config.get(
            "agentic_max_steps",
            DEFAULT_MAX_STEPS
        )
    )

    context_size = int(
        config.get(
            "agentic_context_size",
            DEFAULT_CONTEXT
        )
    )

    system = f"""
You are an autonomous software engineering implementation agent.

A separate TDD phase has already generated and frozen tests.
Those tests are authoritative and MUST NOT be modified.

You may inspect repository files, modify authorized production
files, run the build, run tests, inspect failures, and repair
the production implementation iteratively.

AUTHORIZED WRITABLE FILES:

{chr(10).join(sorted(writable_paths))}

Rules:

- Treat the supplied task as authoritative.
- Inspect existing production code and frozen tests when useful.
- NEVER modify tests.
- NEVER modify specifications.
- Only write files in AUTHORIZED WRITABLE FILES.
- Preserve existing behavior.
- Prefer minimal production changes.
- Do not introduce unnecessary abstractions or dependencies.
- Run the build after implementation changes.
- Run the tests after the build succeeds.
- If build or tests fail, inspect the exact failure and repair
  production code.
- Continue iterating until build and tests both pass.
- Do not merely describe actions. Use tools.
- Do not stop immediately after a failed command.

Required build command:

{build_command}

Required test command:

{test_command}
"""

    user = f"""
Implement the following frozen-contract task:

{task}

The tests have already been generated and frozen by another
agent. You may read them to understand failures, but you cannot
modify them.

Work until both the required build and test commands succeed.
"""

    messages = [
        {
            "role":
                "system",
            "content":
                system
        },
        {
            "role":
                "user",
            "content":
                user
        }
    ]

    append_history(
        config,
        "agentic_implementation_started",
        {
            "model":
                model,
            "writable_paths":
                sorted(
                    writable_paths
                )
        }
    )

    for step in range(
        1,
        max_steps + 1
    ):
        print()
        print("=" * 60)
        print(
            f"AGENTIC IMPLEMENTATION "
            f"STEP {step}/{max_steps}"
        )
        print("=" * 60)

        try:
            response = _call_model(
                model,
                ollama_url,
                context_size,
                messages
            )

        except Exception as exc:
            print(
                "Agentic model call failed: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            return False

        message = response.get(
            "message",
            {}
        )

        messages.append(
            message
        )

        thinking = message.get(
            "thinking",
            ""
        )

        if thinking:
            print()
            print("THINKING:")
            print(thinking)

        content = message.get(
            "content",
            ""
        )

        if content:
            print()
            print("MODEL:")
            print(content)

        tool_calls = message.get(
            "tool_calls",
            []
        )

        if not tool_calls:
            success = (
                _validate_final_state(
                    root,
                    build_command,
                    test_command
                )
            )

            if success:
                mark_phase_completed(
                    config,
                    state,
                    "implementation"
                )

                append_history(
                    config,
                    "agentic_implementation_completed",
                    {
                        "steps":
                            step
                    }
                )

                save_state(
                    config,
                    state
                )

                return True

            print(
                "Agent stopped before "
                "reaching GREEN."
            )

            return False

        for call in tool_calls:
            result = _execute_tool(
                root,
                call,
                writable_paths,
                build_command,
                test_command
            )

            print()
            print("TOOL RESULT:")
            print(
                result[:16000]
            )

            messages.append(
                {
                    "role":
                        "tool",

                    "tool_name":
                        call[
                            "function"
                        ][
                            "name"
                        ],

                    "content":
                        result
                }
            )

    print(
        "Agentic implementation reached "
        "maximum steps."
    )

    return False
