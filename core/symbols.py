"""
Generic, language-neutral matching between a symbol named by a build
diagnostic and the CURRENT authoritative specification text.

This exists so the harness can answer one question deterministically:

    "Is the symbol the compiler says is missing actually something the
     current authoritative spec asked for?"

A missing symbol the spec requested is legitimate EXPECTED RED.
A missing symbol the spec never mentioned is an invented API.

Nothing here knows about any particular benchmark, language, or naming
convention beyond ordinary identifier casing.
"""

import re


IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
)

WORD_PATTERN = re.compile(
    r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+"
)

SEPARATOR_PATTERN = re.compile(
    r"[_\-\s.]+"
)


# Words that carry no discriminating information about *which* API is
# being requested. A spec almost never has to contain them literally for
# a symbol to be the one it described.
GENERIC_WORDS = {
    "a",
    "add",
    "an",
    "and",
    "by",
    "can",
    "create",
    "do",
    "for",
    "get",
    "has",
    "is",
    "new",
    "of",
    "or",
    "set",
    "the",
    "to",
    "try",
    "with",
}

MIN_DISTINCTIVE_LENGTH = 3

MIN_ANCHOR_LENGTH = 4

# Tuned against both existing benchmark spec suites: at this threshold
# every symbol those specs actually introduced still matches (zero false
# negatives, i.e. no legitimate expected-red is ever blocked), while the
# loosest partial-word matches are dropped.
DEFAULT_MATCH_RATIO = 0.75


def split_symbol_words(symbol):
    """
    Split CamelCase / PascalCase / snake_case / kebab-case identifiers
    into lowercase words.

        GetLowBalanceAccounts -> [get, low, balance, accounts]
        transfer_amount       -> [transfer, amount]
        HTTPResponseCode      -> [http, response, code]
    """

    words = []

    for part in SEPARATOR_PATTERN.split(symbol or ""):
        words.extend(
            WORD_PATTERN.findall(part)
        )

    return [
        word.lower()
        for word in words
        if word
    ]


def spec_identifiers(spec_text):
    """
    Every identifier-shaped token in the authoritative spec, original
    case preserved.
    """

    return set(
        IDENTIFIER_PATTERN.findall(
            spec_text or ""
        )
    )


def spec_requests_symbol(
    symbol,
    spec_text,
    match_ratio=DEFAULT_MATCH_RATIO
):
    """
    True when the authoritative spec text plausibly asked for `symbol`.

    Three tiers, cheapest first:

    1. the exact identifier appears in the spec;
    2. the identifier appears case-insensitively;
    3. the identifier's distinctive words are present in the spec.

    Tier 3 exists because specs are prose: a spec may say "add a method
    that closes an account" rather than naming `CloseAccount` literally.
    It is deliberately forgiving — the cost of a false "requested" is a
    missed rejection that later reviewers still see, while the cost of a
    false "not requested" is blocking legitimate test-first work.
    """

    if not symbol or not spec_text:
        return False

    identifiers = spec_identifiers(
        spec_text
    )

    if symbol in identifiers:
        return True

    lowered = {
        value.lower()
        for value in identifiers
    }

    if symbol.lower() in lowered:
        return True

    words = split_symbol_words(
        symbol
    )

    distinctive = [
        word
        for word in words
        if (
            word not in GENERIC_WORDS
            and len(word) >= MIN_DISTINCTIVE_LENGTH
        )
    ]

    if not distinctive:
        return False

    spec_blob = " ".join(
        sorted(lowered)
    )

    matched = [
        word
        for word in distinctive
        if word in spec_blob
    ]

    if not matched:
        return False

    has_anchor = any(
        len(word) >= MIN_ANCHOR_LENGTH
        for word in matched
    )

    ratio = len(matched) / len(distinctive)

    return (
        has_anchor
        and ratio >= match_ratio
    )
