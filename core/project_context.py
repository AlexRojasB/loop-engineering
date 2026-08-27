from pathlib import Path

from core.authority import (
    format_authority_report,
    resolve_authority,
)
from core.project_sources import (
    discover_project_sources,
    format_source_inventory,
    read_project_source,
)


DEFAULT_AUTHORITATIVE_LIMIT = 12000
DEFAULT_SUPPORTING_LIMIT = 6000
DEFAULT_TOTAL_LIMIT = 24000


def compact_text(
    text,
    limit
):
    if text is None:
        return ""

    if len(text) <= limit:
        return text

    half = limit // 2

    return (
        text[:half]
        + "\n\n...[PROJECT CONTEXT TRUNCATED]...\n\n"
        + text[-half:]
    )


def read_source_content(
    workspace,
    source,
    limit
):
    content = read_project_source(
        workspace,
        source
    )

    return {
        "path": source["path"],
        "category": source["category"],
        "authority_score":
            source.get(
                "authority_score"
            ),
        "content":
            compact_text(
                content,
                limit
            )
    }


def build_project_context(
    workspace,
    selected_source_path=None,
    authoritative_limit=DEFAULT_AUTHORITATIVE_LIMIT,
    supporting_limit=DEFAULT_SUPPORTING_LIMIT,
    total_limit=DEFAULT_TOTAL_LIMIT,
    isolate_selected_source=False,
    isolation=None
):
    """
    Resolve the authoritative source and its supporting context.

    When a WorkIsolation boundary is supplied, restricted sources are
    dropped before authority resolution, so they cannot appear as current
    work, authoritative context, supporting context, or even in the
    printed inventory.
    """

    sources = discover_project_sources(
        workspace
    )

    if isolation is not None:
        sources = [
            source
            for source in sources
            if not isolation.is_restricted(
                source["path"]
            )
        ]

    resolution = resolve_authority(
        sources
    )

    if selected_source_path:
        selected = None

        for source in sources:
            if (
                source["path"]
                == selected_source_path
            ):
                selected = dict(
                    source
                )
                break

        if selected is None:
            return {
                "status":
                    "selected_source_not_found",

                "current_work":
                    None,

                "authoritative_context":
                    [],

                "supporting_context":
                    [],

                "inventory":
                    sources,

                "authority":
                    resolution,

                "message":
                    f"Selected source not found: "
                    f"{selected_source_path}"
            }

        selected[
            "authority_score"
        ] = 1000

        selected_parent = str(
            Path(
                selected["path"]
            ).parent
        )

        supporting_sources = []

        for source in sources:
            if (
                source["path"]
                == selected["path"]
            ):
                continue

            if isolate_selected_source:
                source_parent = str(
                    Path(
                        source["path"]
                    ).parent
                )

                if (
                    source_parent
                    == selected_parent
                ):
                    continue

            supporting_sources.append(
                source
            )

        resolution = {
            "status":
                "resolved",

            "primary":
                selected,

            "authoritative":
                [
                    selected
                ],

            "supporting":
                supporting_sources,

            "ambiguous":
                [],

            "message":
                "Project authority resolved "
                "from explicit source selection."
        }

    if (
        resolution["status"]
        == "no_sources"
    ):
        return {
            "status":
                "no_sources",

            "current_work":
                None,

            "authoritative_context":
                [],

            "supporting_context":
                [],

            "inventory":
                sources,

            "authority":
                resolution,

            "message":
                resolution[
                    "message"
                ]
        }

    if (
        resolution["status"]
        == "ambiguous"
        and not selected_source_path
    ):
        return {
            "status":
                "ambiguous",

            "current_work":
                None,

            "authoritative_context":
                [],

            "supporting_context":
                [],

            "inventory":
                sources,

            "authority":
                resolution,

            "message":
                resolution[
                    "message"
                ]
        }

    primary = resolution[
        "primary"
    ]

    current_work = (
        read_source_content(
            workspace,
            primary,
            authoritative_limit
        )
        if primary
        else None
    )

    authoritative_context = []

    for source in resolution.get(
        "authoritative",
        []
    ):
        if (
            primary
            and source["path"]
            == primary["path"]
        ):
            continue

        authoritative_context.append(
            read_source_content(
                workspace,
                source,
                authoritative_limit
            )
        )

    supporting_context = []

    for source in resolution.get(
        "supporting",
        []
    ):
        supporting_context.append(
            read_source_content(
                workspace,
                source,
                supporting_limit
            )
        )

    result = {
        "status":
            "resolved",

        "current_work":
            current_work,

        "authoritative_context":
            authoritative_context,

        "supporting_context":
            supporting_context,

        "inventory":
            sources,

        "authority":
            resolution,

        "message":
            resolution[
                "message"
            ]
    }

    return enforce_total_limit(
        result,
        total_limit
    )


def enforce_total_limit(
    context,
    total_limit
):
    current_work = context.get(
        "current_work"
    )

    if current_work:
        primary_size = len(
            current_work[
                "content"
            ]
        )
    else:
        primary_size = 0

    remaining = max(
        0,
        total_limit
        - primary_size
    )

    authoritative = (
        context.get(
            "authoritative_context",
            []
        )
    )

    supporting = (
        context.get(
            "supporting_context",
            []
        )
    )

    selected_authoritative = []
    selected_supporting = []

    for item in authoritative:
        size = len(
            item[
                "content"
            ]
        )

        if size > remaining:
            continue

        selected_authoritative.append(
            item
        )

        remaining -= size

    for item in supporting:
        size = len(
            item[
                "content"
            ]
        )

        if size > remaining:
            continue

        selected_supporting.append(
            item
        )

        remaining -= size

    context[
        "authoritative_context"
    ] = selected_authoritative

    context[
        "supporting_context"
    ] = selected_supporting

    context[
        "context_budget"
    ] = {
        "total_limit":
            total_limit,

        "remaining":
            remaining
    }

    return context


def format_project_context_report(
    context
):
    lines = [
        f"Project context status: "
        f"{context['status']}"
    ]

    current = context.get(
        "current_work"
    )

    if current:
        lines.append(
            "Current work: "
            f"[{current['category']}] "
            f"{current['path']}"
        )

    lines.append("")
    lines.append(
        format_authority_report(
            context[
                "authority"
            ]
        )
    )

    lines.append("")
    lines.append(
        "Source inventory:"
    )

    lines.append(
        format_source_inventory(
            context[
                "inventory"
            ]
        )
    )

    return "\n".join(
        lines
    )


def build_context_text(
    context
):
    if (
        context["status"]
        != "resolved"
    ):
        return ""

    blocks = []

    current = context.get(
        "current_work"
    )

    if current:
        blocks.append(
            "\n".join(
                [
                    "===== CURRENT WORK =====",
                    (
                        f"Source: "
                        f"{current['path']}"
                    ),
                    (
                        f"Category: "
                        f"{current['category']}"
                    ),
                    "",
                    current["content"],
                    "===== END CURRENT WORK ====="
                ]
            )
        )

    authoritative = context.get(
        "authoritative_context",
        []
    )

    for item in authoritative:
        blocks.append(
            "\n".join(
                [
                    "===== AUTHORITATIVE CONTEXT =====",
                    f"Source: {item['path']}",
                    f"Category: {item['category']}",
                    "",
                    item["content"],
                    "===== END AUTHORITATIVE CONTEXT ====="
                ]
            )
        )

    supporting = context.get(
        "supporting_context",
        []
    )

    for item in supporting:
        blocks.append(
            "\n".join(
                [
                    "===== SUPPORTING CONTEXT =====",
                    f"Source: {item['path']}",
                    f"Category: {item['category']}",
                    "",
                    item["content"],
                    "===== END SUPPORTING CONTEXT ====="
                ]
            )
        )

    return "\n\n".join(
        blocks
    )


def selectable_sources(context):
    authority = context.get(
        "authority",
        {}
    )

    ambiguous = authority.get(
        "ambiguous",
        []
    )

    if ambiguous:
        return [
            source["path"]
            for source in ambiguous
        ]

    authoritative = authority.get(
        "authoritative",
        []
    )

    return [
        source["path"]
        for source in authoritative
    ]
