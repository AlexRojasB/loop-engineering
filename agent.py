from core.pipeline import run_pipeline
from core.utils import load_json


VERSION = "2.5.0"


def main():
    config = load_json(
        "config.json"
    )

    with open(
        "TASK.md"
    ) as f:
        task = f.read()

    success = run_pipeline(
        config,
        task,
        VERSION
    )

    if not success:
        print()
        print(
            f"AGENT {VERSION} "
            "PIPELINE DID NOT COMPLETE"
        )


if __name__ == "__main__":
    main()
