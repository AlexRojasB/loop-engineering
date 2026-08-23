from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name):
    path = PROMPTS_DIR / name

    if not path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {path}"
        )

    return path.read_text()


def render_prompt(name, **values):
    template = load_prompt(name)

    return template.format(**values)
