from pathlib import Path


RUNTIME_DIRNAME = ".agent"


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

    return updated
