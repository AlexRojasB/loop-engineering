import sys
from pathlib import Path

from core.pipeline import run_pipeline
from core.utils import load_json


VERSION = "2.5.0"

HARNESS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = HARNESS_DIR / "config.json"
TASK_PATH = HARNESS_DIR / "TASK.md"


def main():
    if len(sys.argv) != 2:
        print(
            "Usage: python agent.py <project-path>"
        )
        return 1

    project_path = Path(
        sys.argv[1]
    ).expanduser().resolve()

    if not project_path.exists():
        print(
            f"Project path does not exist: "
            f"{project_path}"
        )
        return 1

    if not project_path.is_dir():
        print(
            f"Project path is not a directory: "
            f"{project_path}"
        )
        return 1

    config = load_json(
        CONFIG_PATH
    )

    config["workspace"] = str(
        project_path
    )

    if not TASK_PATH.exists():
        print(
            f"Task file does not exist: "
            f"{TASK_PATH}"
        )
        return 1

    task = TASK_PATH.read_text()

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
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
