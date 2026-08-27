import hashlib
from pathlib import Path


RUNTIME_DIRNAME = ".agent"

HARNESS_RUNTIME_DIR = (
    Path(__file__).resolve().parent.parent
    / "runtime"
)


def project_runtime_dir(
    workspace
):
    path = (
        Path(workspace)
        / RUNTIME_DIRNAME
    )

    path.mkdir(
        parents=True,
        exist_ok=True
    )

    return path


def configure_project_runtime(
    config,
    workspace
):
    runtime = project_runtime_dir(
        workspace
    )

    updated = dict(
        config
    )

    updated["state_file"] = str(
        runtime / "state.json"
    )

    updated["history_file"] = str(
        runtime / "history.jsonl"
    )

    # Cross-attempt failure memory deliberately lives OUTSIDE the target
    # repository. Anything written inside the workspace would either be
    # reverted by the between-attempt `git restore`, break the clean
    # baseline check as an untracked file, or be swept into the automatic
    # completion commit by `git add -A`.
    updated["spec_memory_file"] = str(
        spec_memory_path(workspace)
    )

    return updated


def spec_memory_path(workspace):
    key = hashlib.sha256(
        str(
            Path(workspace).resolve()
        ).encode()
    ).hexdigest()[:12]

    name = Path(
        workspace
    ).resolve().name or "project"

    return (
        HARNESS_RUNTIME_DIR
        / "spec-memory"
        / f"{name}-{key}.json"
    )
