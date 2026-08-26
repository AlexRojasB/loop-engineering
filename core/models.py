import json
import socket
import urllib.request


OLLAMA_URL = "http://localhost:11434/api/generate"


def ollama(
    model,
    prompt,
    timeout,
    json_mode=False,
    think=False,
    num_ctx=None,
    num_predict=None
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

    options = {}

    if num_ctx is not None:
        options["num_ctx"] = num_ctx

    if num_predict is not None:
        options["num_predict"] = num_predict

    if options:
        payload["options"] = options

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

        done_reason = result.get(
            "done_reason"
        )

        # Ollama reports "length" when generation was cut off by the
        # output/context budget rather than reaching a natural stop.
        # With json_mode on, the grammar-constrained decoder force-
        # closes braces/quotes on cutoff, so a truncated response can
        # still parse as syntactically valid JSON while its content is
        # incomplete. done_reason is the only reliable signal for
        # that — do not infer truncation from response text shape.
        truncated = done_reason == "length"

        if (
            not response_text.strip()
            and thinking_text
        ):
            response_text = thinking_text

        return {
            "ok": True,
            "response": response_text,
            "thinking": thinking_text,
            "done_reason": done_reason,
            "truncated": truncated,
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
            "done_reason": None,
            "truncated": False,
            "error":
                f"{model} timed out after {timeout}s"
        }

    except Exception as exc:
        return {
            "ok": False,
            "response": None,
            "thinking": None,
            "done_reason": None,
            "truncated": False,
            "error":
                f"{type(exc).__name__}: {exc}"
        }


def call_model(
    config,
    model,
    prompt,
    json_mode=False,
    reduced_prompt=None,
    think=False,
    num_ctx=None,
    num_predict=None
):
    result = ollama(
        model,
        prompt,
        config.get(
            "model_timeout_seconds",
            420
        ),
        json_mode=json_mode,
        think=think,
        num_ctx=num_ctx,
        num_predict=num_predict
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
        think=think,
        num_ctx=num_ctx,
        num_predict=num_predict
    )
