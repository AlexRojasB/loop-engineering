import json
import re


def load_json(path):
    with open(path) as f:
        return json.load(f)


def compact(text, limit=6000):
    if not text:
        return ""

    if len(text) <= limit:
        return text

    half = limit // 2

    return (
        text[:half]
        + "\n\n...[TRUNCATED BY HARNESS]...\n\n"
        + text[-half:]
    )


def extract_code(text):
    text = text.strip()

    fenced = re.search(
        r"```(?:csharp|cs|python|typescript|javascript|"
        r"java|rust|go|xml|json)?\s*\n?(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL
    )

    if fenced:
        text = fenced.group(1)

    text = re.sub(
        r"^```[A-Za-z0-9_+\-]*\s*",
        "",
        text
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    return text.strip() + "\n"
