import argparse
import subprocess
from pathlib import Path

from core.pipeline import run_pipeline
from core.repository import git_restore_all
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
        "--spec-dir",
        help=(
            "Run all .md specs in a directory "
            "sequentially. Path is relative to "
            "the project."
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


def discover_spec_queue(
    project,
    spec_dir
):
    directory = (
        project
        / spec_dir
    ).resolve()

    try:
        directory.relative_to(
            project
        )
    except ValueError:
        raise ValueError(
            "Spec directory must be inside "
            "the project repository."
        )

    if not directory.exists():
        raise ValueError(
            f"Spec directory does not exist: "
            f"{directory}"
        )

    if not directory.is_dir():
        raise ValueError(
            f"Spec directory is not a directory: "
            f"{directory}"
        )

    specs = sorted(
        path
        for path
        in directory.glob("*.md")
        if path.is_file()
    )

    if not specs:
        raise ValueError(
            f"No .md specs found in: "
            f"{directory}"
        )

    return [
        str(
            path.relative_to(
                project
            )
        )
        for path
        in specs
    ]


def spec_already_completed(
    project,
    spec_path
):
    expected = (
        "agent: complete "
        + Path(spec_path).stem
    )

    result = subprocess.run(
        [
            "git",
            "log",
            "--format=%s",
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True
    )

    return expected in {
        line.strip()
        for line in result.stdout.splitlines()
    }


def commit_spec_result(
    project,
    spec_path
):
    subprocess.run(
        [
            "git",
            "add",
            "-A",
        ],
        cwd=project,
        check=True
    )

    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True
    )

    if not status.stdout.strip():
        print(
            "No repository changes "
            "to commit."
        )
        return True

    message = (
        "agent: complete "
        + Path(
            spec_path
        ).stem
    )

    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            message,
        ],
        cwd=project,
        check=True
    )

    return True


def run_single_spec(
    config,
    project,
    spec_path
):
    context = build_project_context(
        project,
        selected_source_path=
            spec_path,
        isolate_selected_source=True
    )

    print()
    print(
        format_project_context_report(
            context
        )
    )

    if (
        context["status"]
        != "resolved"
    ):
        print()
        print(
            "Spec context could not "
            "be resolved."
        )
        return False

    current_work = context[
        "current_work"
    ]

    run_config = dict(
        config
    )

    run_config[
        "selected_source"
    ] = current_work[
        "path"
    ]

    run_config[
        "project_context"
    ] = context

    run_config["resume"] = False

    return run_pipeline(
        run_config,
        current_work[
            "content"
        ],
        VERSION
    )


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

    if (
        args.spec
        and args.spec_dir
    ):
        print(
            "--spec and --spec-dir "
            "cannot be used together."
        )
        return 1

    if (
        args.resume
        and args.spec_dir
    ):
        print(
            "--resume cannot currently "
            "be used with --spec-dir."
        )
        return 1

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

    if args.spec_dir:
        try:
            spec_queue = discover_spec_queue(
                project,
                args.spec_dir
            )

        except ValueError as exc:
            print(exc)
            return 1

        print()
        print("=" * 60)
        print("MULTI-SPEC RUN")
        print("=" * 60)

        for index, spec_path in enumerate(
            spec_queue,
            start=1
        ):
            print()
            print(
                f"[{index}/{len(spec_queue)}] "
                f"{spec_path}"
            )

        completed = []

        for index, spec_path in enumerate(
            spec_queue,
            start=1
        ):
            if spec_already_completed(
                project,
                spec_path
            ):
                print()
                print(
                    f"SKIP completed spec: "
                    f"{spec_path}"
                )
                completed.append(
                    spec_path
                )
                continue

            print()
            print("=" * 60)
            print(
                f"SPEC {index}/{len(spec_queue)} "
                f"- {spec_path}"
            )
            print("=" * 60)

            max_spec_attempts = config.get(
                "max_spec_attempts",
                5
            )

            success = False

            for spec_attempt in range(
                1,
                max_spec_attempts + 1
            ):
                print()
                print(
                    f"SPEC ATTEMPT "
                    f"{spec_attempt}/"
                    f"{max_spec_attempts}"
                )

                success = run_single_spec(
                    config,
                    project,
                    spec_path
                )

                if success:
                    break

                print()
                print(
                    "Spec attempt failed. "
                    "Restoring last committed "
                    "repository state before retry."
                )

                git_restore_all(
                    project
                )

            if not success:
                print()
                print("=" * 60)
                print("MULTI-SPEC RUN STOPPED")
                print("=" * 60)
                print(
                    f"Failed spec: {spec_path}"
                )
                print(
                    f"Attempts: "
                    f"{max_spec_attempts}"
                )
                print(
                    f"Completed: "
                    f"{len(completed)}/"
                    f"{len(spec_queue)}"
                )

                return 1

            try:
                commit_spec_result(
                    project,
                    spec_path
                )

            except subprocess.CalledProcessError as exc:
                print()
                print(
                    "Automatic commit failed: "
                    f"{exc}"
                )
                return 1

            completed.append(
                spec_path
            )

        print()
        print("=" * 60)
        print("MULTI-SPEC RUN PASSED")
        print("=" * 60)
        print(
            f"Completed: "
            f"{len(completed)}/"
            f"{len(spec_queue)}"
        )

        for spec_path in completed:
            print(
                f"- PASS {spec_path}"
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
