import argparse
from pathlib import Path

from core.pipeline import run_pipeline
from core.project_context import (
    build_project_context,
    format_project_context_report,
    selectable_sources,
)
from core.project_runtime import configure_project_runtime
from core.resume import (
    format_resume_report,
    inspect_resume_state,
)
from core.utils import load_json


VERSION = "2.5.1"

HARNESS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = HARNESS_DIR / "config.json"


def parse_args():
    parser = argparse.ArgumentParser(
        description=
            "Local autonomous engineering agent"
    )

    parser.add_argument(
        "project",
        help=
            "Path to the project repository"
    )

    parser.add_argument(
        "--spec",
        help=(
            "Explicit project source/spec to use "
            "as the current work item. "
            "Path is relative to the project."
        )
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help=
            "Resume from persisted project state"
    )

    parser.add_argument(
        "--list-sources",
        action="store_true",
        help=(
            "Discover and display project "
            "task/spec/backlog/documentation sources "
            "without running the agent"
        )
    )

    parser.add_argument(
        "--resume-info",
        action="store_true",
        help=(
            "Display persisted resume state "
            "without running the agent"
        )
    )

    return parser.parse_args()


def resolve_project(
    path_value
):
    project = Path(
        path_value
    ).expanduser().resolve()

    if not project.exists():
        raise ValueError(
            f"Project path does not exist: "
            f"{project}"
        )

    if not project.is_dir():
        raise ValueError(
            f"Project path is not a directory: "
            f"{project}"
        )

    return project


def main():
    args = parse_args()

    try:
        project = resolve_project(
            args.project
        )

    except ValueError as exc:
        print(exc)
        return 1

    config = load_json(
        CONFIG_PATH
    )

    config["workspace"] = str(
        project
    )

    config = configure_project_runtime(
        config,
        project
    )

    config["resume"] = bool(
        args.resume
    )

    if args.resume_info:
        inspection = inspect_resume_state(
            config,
            project
        )

        print()
        print(
            format_resume_report(
                inspection
            )
        )

        return 0

    context = build_project_context(
        project,
        selected_source_path=
            args.spec
    )

    print()
    print(
        format_project_context_report(
            context
        )
    )

    if args.list_sources:
        return 0

    if (
        context["status"]
        == "no_sources"
    ):
        print()
        print(
            "No task/spec/backlog/documentation "
            "sources were discovered."
        )

        print(
            "Add a project source document "
            "before running autonomously."
        )

        return 1

    if (
        context["status"]
        == "ambiguous"
    ):
        print()
        print(
            "Multiple authoritative sources "
            "could represent the current work."
        )

        print(
            "Possible current work items:"
        )

        for source_path in selectable_sources(
            context
        ):
            print(
                f"  - {source_path}"
            )

        print()
        print(
            "Select one explicitly with:"
        )

        print(
            "  python agent.py "
            "<project> --spec <path>"
        )

        return 1

    if (
        context["status"]
        == "selected_source_not_found"
    ):
        print()
        print(
            context["message"]
        )

        return 1

    current_work = context[
        "current_work"
    ]

    task = current_work[
        "content"
    ]

    config[
        "selected_source"
    ] = current_work[
        "path"
    ]

    config[
        "project_context"
    ] = context

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
