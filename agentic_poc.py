import json
import subprocess
import sys
import urllib.request
from pathlib import Path


MODEL = "qwen3.5:4b"
OLLAMA_URL = "http://localhost:11434/api/chat"
MAX_STEPS = 40


def safe_path(root, requested):
    path = (root / requested).resolve()

    if root not in path.parents and path != root:
        raise ValueError("Path escapes workspace")

    return path


def read_file(root, path):
    target = safe_path(root, path)

    return target.read_text()


def write_file(root, path, content):
    target = safe_path(root, path)

    target.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    target.write_text(content)

    return f"Wrote {path}"


def list_files(root):
    result = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(root)

        if any(
            part in {
                ".git",
                "bin",
                "obj",
                ".agent"
            }
            for part in relative.parts
        ):
            continue

        result.append(str(relative))

    return "\n".join(
        sorted(result)
    )


def run_command(root, command):
    allowed_prefixes = (
        "dotnet build",
        "dotnet test",
        "git status",
        "git diff",
    )

    if not command.startswith(
        allowed_prefixes
    ):
        return (
            "Command rejected. "
            "Allowed commands: "
            + ", ".join(allowed_prefixes)
        )

    result = subprocess.run(
        command,
        cwd=root,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180
    )

    output = result.stdout

    if len(output) > 12000:
        output = (
            output[:6000]
            + "\n...[TRUNCATED]...\n"
            + output[-6000:]
        )

    return (
        f"exit_code={result.returncode}\n"
        f"{output}"
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description":
                "Read a text file from the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description":
                "Replace the complete contents of "
                "a repository text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "content": {
                        "type": "string"
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
                "List repository files.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description":
                "Run an allowed build, test, or "
                "Git inspection command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string"
                    }
                },
                "required": ["command"]
            }
        }
    }
]


def call_model(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "options": {
            "num_ctx": 16384
        }
    }

    request = urllib.request.Request(
        OLLAMA_URL,
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


def execute_tool(root, call):
    function = call["function"]
    name = function["name"]
    args = function.get(
        "arguments",
        {}
    )

    print()
    print(
        f">>> TOOL: {name}"
    )
    print(
        json.dumps(
            args,
            indent=2
        )
    )

    try:
        if name == "read_file":
            return read_file(
                root,
                args["path"]
            )

        if name == "write_file":
            return write_file(
                root,
                args["path"],
                args["content"]
            )

        if name == "list_files":
            return list_files(root)

        if name == "run_command":
            return run_command(
                root,
                args["command"]
            )

        return (
            f"Unknown tool: {name}"
        )

    except Exception as exc:
        return (
            f"TOOL ERROR: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )


def main():
    if len(sys.argv) != 3:
        print(
            "Usage: python agentic_poc.py "
            "<workspace> <spec>"
        )
        return 1

    root = Path(
        sys.argv[1]
    ).resolve()

    spec_path = sys.argv[2]

    spec = read_file(
        root,
        spec_path
    )

    system = """
You are an autonomous software engineering agent.

You have tools to inspect files, edit files, and run builds/tests.

Work iteratively until the requested task is complete.

Rules:

- Treat the supplied specification as authoritative.
- Inspect existing production code before editing.
- Preserve existing behavior.
- Do not modify the specification.
- Do not modify tests unless explicitly necessary.
- Do not invent unrelated abstractions or dependencies.
- Prefer minimal changes.
- After editing, run the build.
- If the build fails, inspect the error and repair it.
- Then run tests.
- If tests fail, inspect the failures and repair the implementation.
- Continue until both build and tests succeed.
- Do not merely describe what you would do. Use tools.
- Do not stop after a failed build or test.
- When both build and tests pass, provide a concise final summary.
"""

    user = f"""
Implement this specification:

PATH:
{spec_path}

SPECIFICATION:

{spec}

Required validation commands:

dotnet build InventoryPipeline.slnx
dotnet test InventoryPipeline.slnx
"""

    messages = [
        {
            "role": "system",
            "content": system
        },
        {
            "role": "user",
            "content": user
        }
    ]

    for step in range(
        1,
        MAX_STEPS + 1
    ):
        print()
        print("=" * 60)
        print(
            f"AGENTIC STEP {step}/{MAX_STEPS}"
        )
        print("=" * 60)

        response = call_model(
            messages
        )

        message = response[
            "message"
        ]

        messages.append(message)

        thinking = message.get(
            "thinking",
            ""
        )

        if thinking:
            print()
            print(
                "THINKING:"
            )
            print(thinking)

        content = message.get(
            "content",
            ""
        )

        if content:
            print()
            print(
                "MODEL:"
            )
            print(content)

        tool_calls = message.get(
            "tool_calls",
            []
        )

        if not tool_calls:
            print()
            print(
                "No tool call returned. "
                "Agent stopped."
            )
            return 0

        for call in tool_calls:
            result = execute_tool(
                root,
                call
            )

            print()
            print(
                "TOOL RESULT:"
            )
            print(
                result[:12000]
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_name":
                        call["function"][
                            "name"
                        ],
                    "content":
                        result
                }
            )

    print(
        "Maximum agentic steps reached."
    )

    return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
