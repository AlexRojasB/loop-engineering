import os
import subprocess
from pathlib import Path


IGNORED_DIRS = {
    ".git",
    "bin",
    "obj",
    ".venv",
    "node_modules"
}


def safe_path(workspace, relative):
    root = Path(workspace).resolve()
    target = (root / relative).resolve()

    if target != root and root not in target.parents:
        raise ValueError(
            f"Path escapes workspace: {relative}"
        )

    return target


def read_file(workspace, relative):
    path = safe_path(
        workspace,
        relative
    )

    if not path.exists():
        raise FileNotFoundError(relative)

    return path.read_text()


def write_file(
    workspace,
    relative,
    content
):
    path = safe_path(
        workspace,
        relative
    )

    if not path.exists():
        raise FileNotFoundError(
            f"File creation not supported yet: {relative}"
        )

    path.write_text(content)


def discover_files(workspace):
    files = []

    for root, dirs, names in os.walk(
        workspace
    ):
        dirs[:] = [
            d for d in dirs
            if d not in IGNORED_DIRS
        ]

        for name in names:
            if name.startswith(".ai-"):
                continue

            if name.endswith(".backup"):
                continue

            path = Path(root) / name

            files.append(
                str(
                    path.relative_to(
                        workspace
                    )
                )
            )

    return sorted(files)


def run_command(
    workspace,
    command
):
    process = subprocess.run(
        command,
        cwd=workspace,
        shell=True,
        capture_output=True,
        text=True,
        timeout=300
    )

    return {
        "exit_code": process.returncode,
        "output":
            process.stdout
            + process.stderr
    }


def git_status(workspace):
    process = subprocess.run(
        [
            "git",
            "status",
            "--short"
        ],
        cwd=workspace,
        capture_output=True,
        text=True
    )

    return process.stdout


def git_diff(workspace):
    process = subprocess.run(
        [
            "git",
            "diff",
            "--",
            "."
        ],
        cwd=workspace,
        capture_output=True,
        text=True
    )

    return process.stdout


def git_restore_all(workspace):
    subprocess.run(
        [
            "git",
            "restore",
            "."
        ],
        cwd=workspace,
        capture_output=True,
        text=True
    )


def ensure_clean_baseline(
    workspace
):
    status = git_status(
        workspace
    )

    if status.strip():
        print()
        print(
            "ERROR: Workspace is not clean."
        )
        print(status)
        return False

    return True


def snapshot_files(
    workspace,
    paths
):
    return {
        path: read_file(
            workspace,
            path
        )
        for path in paths
    }


def restore_snapshot(
    workspace,
    snapshot
):
    for path, content in (
        snapshot.items()
    ):
        write_file(
            workspace,
            path,
            content
        )
