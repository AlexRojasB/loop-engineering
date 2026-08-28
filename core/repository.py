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


def run_argv(workspace, argv, timeout=300):
    """
    Run a structured argv list without a shell.

    Used wherever the harness itself invokes build/test tooling. Unlike
    run_command() this never interprets shell syntax, so it is safe for
    commands assembled from adapter output.
    """

    process = subprocess.run(
        argv,
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout
    )

    return {
        "exit_code": process.returncode,
        "output": process.stdout or ""
    }


def discover_files(workspace, isolation=None):
    """
    List repository files.

    When a work isolation boundary is supplied, sources restricted for the
    current work item are omitted entirely, so they can never reach a
    planner prompt or a path-repair lookup.
    """

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

            relative = str(
                path.relative_to(
                    workspace
                )
            )

            if (
                isolation is not None
                and isolation.is_restricted(
                    relative
                )
            ):
                continue

            files.append(
                relative
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


def git_clean_untracked(workspace):
    """
    Remove untracked, non-ignored files and directories.

    Deliberately without -x: files the project itself ignores (build
    output such as bin/ or obj/) are the project's business, are never
    reported by `git status --short`, and re-creating them costs build
    time. Everything else untracked at rollback time was created during
    the attempt that just failed, because the harness refuses to start
    an attempt unless `git status --short` is empty.
    """

    process = subprocess.run(
        [
            "git",
            "clean",
            "-fd"
        ],
        cwd=workspace,
        capture_output=True,
        text=True
    )

    return process.stdout


def rollback_repository(
    workspace,
    clean_untracked=True
):
    """
    Return the target repository to its last committed state.

    Restoring tracked files is not sufficient on its own: a failed
    attempt can also leave NEW files behind (a production file the
    implementation agent created, or -- historically -- harness runtime
    state). Those survive `git restore .` and then fail the next
    attempt's clean-baseline check, which is exactly how one failed
    Ledger attempt turned into four instant failures.

    `clean_untracked` must be False whenever this run did NOT verify a
    clean baseline at startup -- a resumed run, above all. Removing
    untracked files is only provably safe when the harness knows every
    untracked file present was created by the attempt it is discarding;
    on a resumed run an untracked file may predate the harness entirely
    and belong to the user.

    Returns the resulting `git status --short` output, so callers can
    prove the repository really is clean.
    """

    git_restore_all(
        workspace
    )

    if clean_untracked:
        git_clean_untracked(
            workspace
        )

    return git_status(
        workspace
    )
