from pathlib import Path


IGNORED_DIRS = {
    ".git",
    ".agent",
    ".venv",
    "venv",
    "bin",
    "obj",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".idea",
    ".vscode"
}


SOURCE_FILENAMES = {
    "task.md": "task",
    "tasks.md": "tasks",
    "backlog.md": "backlog",
    "readme.md": "readme",
    "architecture.md": "architecture",
    "decisions.md": "architecture",
    "requirements.md": "requirements",
    "spec.md": "spec",
    "specs.md": "spec"
}


SOURCE_DIR_HINTS = {
    "spec": {
        "spec",
        "specs",
        "specifications",
        "requirements"
    },

    "task": {
        "task",
        "tasks",
        "work-items",
        "workitems"
    },

    "backlog": {
        "backlog",
        "backlogs"
    },

    "architecture": {
        "architecture",
        "adr",
        "adrs",
        "decisions"
    },

    "docs": {
        "docs",
        "documentation"
    },

    "openspec": {
        "openspec"
    }
}


SUPPORTED_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst"
}


def is_ignored(path):
    return any(
        part in IGNORED_DIRS
        for part in path.parts
    )


def classify_by_filename(path):
    return SOURCE_FILENAMES.get(
        path.name.lower()
    )


def classify_by_directory(path):
    parts = {
        part.lower()
        for part in path.parts
    }

    for category, hints in (
        SOURCE_DIR_HINTS.items()
    ):
        if parts & hints:
            return category

    return None


def classify_source(path):
    filename_category = (
        classify_by_filename(
            path
        )
    )

    if filename_category:
        return filename_category

    directory_category = (
        classify_by_directory(
            path
        )
    )

    if directory_category:
        return directory_category

    stem = path.stem.lower()

    if "spec" in stem:
        return "spec"

    if "task" in stem:
        return "task"

    if "backlog" in stem:
        return "backlog"

    if (
        "architecture" in stem
        or stem.startswith("adr")
    ):
        return "architecture"

    return "documentation"


def discover_project_sources(
    workspace
):
    root = Path(
        workspace
    ).resolve()

    discovered = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(
            root
        )

        if is_ignored(
            relative
        ):
            continue

        if (
            path.suffix.lower()
            not in SUPPORTED_EXTENSIONS
        ):
            continue

        category = classify_source(
            relative
        )

        discovered.append(
            {
                "path": str(relative),
                "category": category,
                "name": path.name,
                "depth":
                    len(relative.parts) - 1,
                "size":
                    path.stat().st_size
            }
        )

    return sorted(
        discovered,
        key=lambda item: (
            item["category"],
            item["depth"],
            item["path"].lower()
        )
    )


def group_sources_by_category(
    sources
):
    grouped = {}

    for source in sources:
        category = source[
            "category"
        ]

        grouped.setdefault(
            category,
            []
        ).append(
            source
        )

    return grouped


def format_source_inventory(
    sources
):
    if not sources:
        return "No project source documents discovered."

    lines = []

    for source in sources:
        lines.append(
            f"[{source['category']}] "
            f"{source['path']}"
        )

    return "\n".join(
        lines
    )
