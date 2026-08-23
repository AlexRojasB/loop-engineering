from pathlib import Path


AUTHORITY_PRIORITY = {
    "spec": 100,
    "requirements": 90,
    "task": 80,
    "tasks": 75,
    "architecture": 70,
    "openspec": 65,
    "backlog": 50,
    "docs": 30,
    "documentation": 20,
    "readme": 10
}


ACTIVE_NAME_HINTS = {
    "current",
    "active",
    "next",
    "todo",
    "task",
    "feature"
}


COMPLETED_NAME_HINTS = {
    "done",
    "completed",
    "complete",
    "archive",
    "archived"
}


def source_priority(source):
    return AUTHORITY_PRIORITY.get(
        source["category"],
        0
    )


def filename_tokens(source):
    stem = Path(
        source["path"]
    ).stem.lower()

    normalized = (
        stem
        .replace("-", " ")
        .replace("_", " ")
    )

    return set(
        normalized.split()
    )


def looks_active(source):
    tokens = filename_tokens(
        source
    )

    return bool(
        tokens & ACTIVE_NAME_HINTS
    )


def looks_completed(source):
    tokens = filename_tokens(
        source
    )

    return bool(
        tokens & COMPLETED_NAME_HINTS
    )


def rank_sources(sources):
    ranked = []

    for source in sources:
        item = dict(
            source
        )

        score = source_priority(
            source
        )

        if looks_active(source):
            score += 15

        if looks_completed(source):
            score -= 50

        item[
            "authority_score"
        ] = score

        ranked.append(
            item
        )

    return sorted(
        ranked,
        key=lambda item: (
            -item[
                "authority_score"
            ],
            item["depth"],
            item["path"].lower()
        )
    )


def resolve_authority(
    sources
):
    ranked = rank_sources(
        sources
    )

    if not ranked:
        return {
            "status":
                "no_sources",

            "primary":
                None,

            "authoritative":
                [],

            "supporting":
                [],

            "ambiguous":
                [],

            "message":
                "No project source documents were discovered."
        }

    highest_score = ranked[
        0
    ][
        "authority_score"
    ]

    highest = [
        source
        for source in ranked
        if source[
            "authority_score"
        ] == highest_score
    ]

    authoritative = [
        source
        for source in ranked
        if source[
            "authority_score"
        ] >= 70
    ]

    supporting = [
        source
        for source in ranked
        if source[
            "authority_score"
        ] < 70
    ]

    if len(highest) > 1:
        return {
            "status":
                "ambiguous",

            "primary":
                None,

            "authoritative":
                authoritative,

            "supporting":
                supporting,

            "ambiguous":
                highest,

            "message":
                "Multiple equally authoritative project "
                "sources were discovered. A current work "
                "item must be selected before autonomous "
                "implementation."
        }

    return {
        "status":
            "resolved",

        "primary":
            highest[0],

        "authoritative":
            authoritative,

        "supporting":
            supporting,

        "ambiguous":
            [],

        "message":
            "Project authority resolved."
    }


def format_authority_report(
    resolution
):
    lines = [
        "Authority status: "
        + resolution[
            "status"
        ]
    ]

    primary = resolution.get(
        "primary"
    )

    if primary:
        lines.append(
            "Primary source: "
            f"[{primary['category']}] "
            f"{primary['path']}"
        )

    ambiguous = resolution.get(
        "ambiguous",
        []
    )

    if ambiguous:
        lines.append(
            "Ambiguous authoritative sources:"
        )

        for source in ambiguous:
            lines.append(
                f"- [{source['category']}] "
                f"{source['path']}"
            )

    authoritative = resolution.get(
        "authoritative",
        []
    )

    if authoritative:
        lines.append(
            "Authoritative context:"
        )

        for source in authoritative:
            lines.append(
                f"- [{source['category']}] "
                f"{source['path']}"
            )

    supporting = resolution.get(
        "supporting",
        []
    )

    if supporting:
        lines.append(
            "Supporting context:"
        )

        for source in supporting:
            lines.append(
                f"- [{source['category']}] "
                f"{source['path']}"
            )

    lines.append(
        resolution[
            "message"
        ]
    )

    return "\n".join(
        lines
    )
