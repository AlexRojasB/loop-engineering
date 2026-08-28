"""
Ownership and lifecycle of HARNESS runtime state.

The harness needs somewhere to keep per-project execution state: the
resume state file, the append-only history log, and bounded
cross-attempt failure memory. None of that belongs to the user's
project, so none of it may exist inside the target repository.

Anything the harness writes into the workspace is wrong in four
distinct ways:

- it appears as an untracked artifact and fails the clean-baseline
  check on the NEXT attempt (`ERROR: Workspace is not clean.` for
  `?? .agent/`);
- `git restore .` between attempts does not remove it, so the failure
  is permanent for the rest of the run;
- on success `git add -A` sweeps it into the automatic completion
  commit, polluting the user's history;
- it survives into whatever the user does next with the repository.

So the rule is ownership-based, not per-file: harness-owned runtime
state lives under <harness>/runtime/projects/<project-key>/, keyed by
the resolved workspace path. The workspace only ever contains the
user's project.
"""

import hashlib
import shutil
from pathlib import Path


HARNESS_RUNTIME_DIR = (
    Path(__file__).resolve().parent.parent
    / "runtime"
)

PROJECTS_DIRNAME = "projects"

STATE_FILENAME = "state.json"

HISTORY_FILENAME = "history.jsonl"

SPEC_MEMORY_FILENAME = "spec-memory.json"

# Pre-2.6 harness versions kept state.json/history.jsonl in a `.agent`
# directory INSIDE the target repository. The name is retained only so
# a workspace already dirtied by an older version can be reclaimed;
# nothing writes here any more.
LEGACY_RUNTIME_DIRNAME = ".agent"

LEGACY_RUNTIME_FILENAMES = {
    STATE_FILENAME,
    HISTORY_FILENAME,
    SPEC_MEMORY_FILENAME,
}

RECLAIMED_DIRNAME = "reclaimed-legacy-state"


def project_runtime_key(workspace):
    """
    Stable identity of one target repository: its directory name plus a
    hash of its resolved absolute path, so two projects that happen to
    share a basename never share runtime state.
    """

    resolved = Path(
        workspace
    ).resolve()

    digest = hashlib.sha256(
        str(resolved).encode()
    ).hexdigest()[:12]

    name = resolved.name or "project"

    return f"{name}-{digest}"


def harness_project_runtime_dir(
    workspace,
    create=True
):
    """
    The single harness-owned directory for this project's runtime
    state. Always outside the target repository.
    """

    path = (
        HARNESS_RUNTIME_DIR
        / PROJECTS_DIRNAME
        / project_runtime_key(
            workspace
        )
    )

    if create:
        path.mkdir(
            parents=True,
            exist_ok=True
        )

    return path


def state_path(workspace):
    return (
        harness_project_runtime_dir(
            workspace,
            create=False
        )
        / STATE_FILENAME
    )


def history_path(workspace):
    return (
        harness_project_runtime_dir(
            workspace,
            create=False
        )
        / HISTORY_FILENAME
    )


def spec_memory_path(workspace):
    return (
        harness_project_runtime_dir(
            workspace,
            create=False
        )
        / SPEC_MEMORY_FILENAME
    )


def runtime_paths(workspace):
    """
    Every path the harness may write for this project. Used by the
    cleanliness regression test to prove none of them is inside the
    workspace.
    """

    return {
        "state_file": state_path(
            workspace
        ),
        "history_file": history_path(
            workspace
        ),
        "spec_memory_file":
            spec_memory_path(
                workspace
            ),
    }


def is_inside(path, root):
    path = Path(path).resolve()
    root = Path(root).resolve()

    return (
        path == root
        or root in path.parents
    )


def assert_runtime_outside_workspace(
    config,
    workspace
):
    """
    Fail-closed invariant check.

    Any future configuration or code path that points harness runtime
    state back into the target repository must fail loudly here rather
    than silently dirtying the user's project again.
    """

    offenders = []

    for key in (
        "state_file",
        "history_file",
        "spec_memory_file",
    ):
        value = config.get(key)

        if not value:
            continue

        if is_inside(
            value,
            workspace
        ):
            offenders.append(
                f"{key}={value}"
            )

    if offenders:
        raise ValueError(
            "Harness runtime state must not be written inside the "
            "target repository: "
            + ", ".join(offenders)
        )

    return True


def reclaim_legacy_runtime_state(
    workspace
):
    """
    Remove a `.agent/` directory left inside a workspace by an older
    harness version, preserving its contents under the harness runtime
    directory first.

    Fails closed: the directory is only removed when EVERY file in it
    is a known harness runtime artifact. Anything unexpected is left
    exactly where it is and reported, because at that point the
    directory may legitimately belong to the user's project.

    Returns a report dict; `removed` is True only when the workspace is
    now free of it.
    """

    legacy = (
        Path(workspace)
        / LEGACY_RUNTIME_DIRNAME
    )

    if not legacy.exists():
        return {
            "found": False,
            "removed": False,
            "unexpected": [],
            "migrated": [],
            "preserved_to": None
        }

    if not legacy.is_dir():
        return {
            "found": True,
            "removed": False,
            "unexpected": [
                LEGACY_RUNTIME_DIRNAME
            ],
            "migrated": [],
            "preserved_to": None
        }

    unexpected = sorted(
        str(
            path.relative_to(legacy)
        )
        for path in legacy.rglob("*")
        if path.is_file()
        and path.name
        not in LEGACY_RUNTIME_FILENAMES
    )

    if unexpected:
        return {
            "found": True,
            "removed": False,
            "unexpected": unexpected,
            "migrated": [],
            "preserved_to": None
        }

    runtime = harness_project_runtime_dir(
        workspace
    )

    destination = (
        runtime
        / RECLAIMED_DIRNAME
    )

    migrated = []

    try:
        destination.mkdir(
            parents=True,
            exist_ok=True
        )

        for path in sorted(
            legacy.iterdir()
        ):
            if not path.is_file():
                continue

            shutil.copy2(
                path,
                destination / path.name
            )

            # Also adopt it as the ACTIVE runtime file when the new
            # location does not have one yet. Archiving alone would
            # make a run interrupted under the old layout silently
            # unresumable after the upgrade.
            active = runtime / path.name

            if not active.exists():
                shutil.copy2(
                    path,
                    active
                )

                migrated.append(
                    path.name
                )

    except OSError:
        # Preserving the old state is a courtesy, not a requirement.
        destination = None
        migrated = []

    try:
        shutil.rmtree(legacy)

    except OSError as exc:
        return {
            "found": True,
            "removed": False,
            "unexpected": [
                f"could not remove: {exc}"
            ],
            "migrated": sorted(migrated),
            "preserved_to":
                str(destination)
                if destination
                else None
        }

    return {
        "found": True,
        "removed": True,
        "unexpected": [],
        "migrated": sorted(migrated),
        "preserved_to":
            str(destination)
            if destination
            else None
    }


def format_legacy_runtime_report(report):
    if not report:
        return None

    if not report.get("found"):
        return None

    if report.get("removed"):
        message = (
            "Reclaimed harness runtime state from "
            f"{LEGACY_RUNTIME_DIRNAME}/ inside the target repository."
        )

        if report.get("migrated"):
            message += (
                "\nAdopted as current runtime state: "
                + ", ".join(
                    report["migrated"]
                )
            )

        if report.get("preserved_to"):
            message += (
                "\nPreserved previous contents under: "
                f"{report['preserved_to']}"
            )

        return message

    return (
        f"WARNING: {LEGACY_RUNTIME_DIRNAME}/ exists in the target "
        "repository and was left untouched because it contains files "
        "the harness does not own:\n"
        + "\n".join(
            f"- {item}"
            for item in report.get(
                "unexpected",
                []
            )
        )
    )


def configure_project_runtime(
    config,
    workspace
):
    """
    Point every harness runtime path at harness-owned storage.

    Nothing is created inside the target repository. A `.agent/`
    directory left behind by an older harness version is reclaimed
    first, so a workspace dirtied by a previous run heals instead of
    failing every subsequent attempt's clean-baseline check.
    """

    legacy_report = reclaim_legacy_runtime_state(
        workspace
    )

    runtime = harness_project_runtime_dir(
        workspace
    )

    updated = dict(
        config
    )

    updated["runtime_dir"] = str(
        runtime
    )

    updated["state_file"] = str(
        runtime / STATE_FILENAME
    )

    updated["history_file"] = str(
        runtime / HISTORY_FILENAME
    )

    # Cross-attempt failure memory deliberately lives OUTSIDE the target
    # repository. Anything written inside the workspace would either be
    # reverted by the between-attempt `git restore`, break the clean
    # baseline check as an untracked file, or be swept into the automatic
    # completion commit by `git add -A`.
    updated["spec_memory_file"] = str(
        runtime / SPEC_MEMORY_FILENAME
    )

    updated[
        "legacy_runtime_report"
    ] = legacy_report

    assert_runtime_outside_workspace(
        updated,
        workspace
    )

    return updated
