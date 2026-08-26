import json
import socket
import urllib.request


OLLAMA_URL = "http://localhost:11434/api/generate"


def ollama(
    model,
    prompt,
    timeout,
    json_mode=False,
    think=False
):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False
    }

    if json_mode:
        payload["format"] = "json"

    if think:
        payload["think"] = True

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout
        ) as response:
            result = json.loads(
                response.read().decode()
            )

        response_text = result.get(
            "response",
            ""
        )

        thinking_text = result.get(
            "thinking"
        )

        if (
            not response_text.strip()
            and thinking_text
        ):
            response_text = thinking_text

        return {
            "ok": True,
            "response": response_text,
            "thinking": thinking_text,
            "error": None
        }

    except (
        TimeoutError,
        socket.timeout
    ):
        return {
            "ok": False,
            "response": None,
            "thinking": None,
            "error":
                f"{model} timed out after {timeout}s"
        }

    except Exception as exc:
        return {
            "ok": False,
            "response": None,
            "thinking": None,
            "error":
                f"{type(exc).__name__}: {exc}"
        }


def call_model(
    config,
    model,
    prompt,
    json_mode=False,
    reduced_prompt=None,
    think=False
):
    result = ollama(
        model,
        prompt,
        config.get(
            "model_timeout_seconds",
            420
        ),
        json_mode=json_mode,
        think=think
    )

    if result["ok"]:
        return result

    print()
    print(
        f"Model call failed: {result['error']}"
    )

    if reduced_prompt is None:
        return result

    print(
        "Retrying once with reduced context..."
    )

    return ollama(
        model,
        reduced_prompt,
        config.get(
            "retry_timeout_seconds",
            300
        ),
        json_mode=json_mode,
        think=think
    )
