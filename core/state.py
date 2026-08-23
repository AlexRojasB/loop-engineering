import json
from datetime import datetime, timezone
from pathlib import Path


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def default_state(task):
    return {
        "task": task,
        "phase": "starting",
        "planner_complete": False,
        "tests_generated": False,
        "tests_structurally_valid": False,
        "tests_reviewed": False,
        "tests_frozen": False,
        "expected_red_confirmed": False,
        "implementation_generated": False,
        "build": "unknown",
        "tests": {
            "status": "unknown",
            "passed": None,
            "failed": None
        },
        "review": "pending",
        "updated_at": now_iso()
    }


def ensure_parent(path):
    Path(path).parent.mkdir(
        parents=True,
        exist_ok=True
    )


def save_state(config, state):
    path = config["state_file"]

    ensure_parent(path)

    state["updated_at"] = now_iso()

    with open(
        path,
        "w"
    ) as f:
        json.dump(
            state,
            f,
            indent=2
        )


def append_history(
    config,
    event,
    data=None
):
    path = config["history_file"]

    ensure_parent(path)

    entry = {
        "timestamp": now_iso(),
        "event": event,
        "data": data or {}
    }

    with open(
        path,
        "a"
    ) as f:
        f.write(
            json.dumps(entry)
            + "\n"
        )


def load_state(config):
    path = Path(
        config["state_file"]
    )

    if not path.exists():
        return None

    with path.open() as f:
        return json.load(f)


def read_history(config):
    path = Path(
        config["history_file"]
    )

    if not path.exists():
        return []

    events = []

    with path.open() as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            events.append(
                json.loads(line)
            )

    return events


PIPELINE_PHASES = (
    "planning",
    "test_contract",
    "expected_red",
    "implementation",
    "build",
    "tests",
    "review",
)


def ensure_checkpoints(state):
    checkpoints = state.setdefault(
        "checkpoints",
        {}
    )

    for phase in PIPELINE_PHASES:
        checkpoints.setdefault(
            phase,
            "pending"
        )

    return checkpoints


def mark_phase_started(
    config,
    state,
    phase
):
    checkpoints = ensure_checkpoints(
        state
    )

    checkpoints[
        phase
    ] = "started"

    state[
        "current_phase"
    ] = phase

    state[
        "phase_status"
    ] = "started"

    state[
        "phase"
    ] = phase

    save_state(
        config,
        state
    )


def mark_phase_completed(
    config,
    state,
    phase
):
    checkpoints = ensure_checkpoints(
        state
    )

    checkpoints[
        phase
    ] = "completed"

    state[
        "current_phase"
    ] = phase

    state[
        "phase_status"
    ] = "completed"

    state[
        "phase"
    ] = phase

    save_state(
        config,
        state
    )


def phase_status(
    state,
    phase
):
    checkpoints = ensure_checkpoints(
        state
    )

    return checkpoints.get(
        phase,
        "pending"
    )
