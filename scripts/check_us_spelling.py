#!/usr/bin/env python3
"""
Fail when a user-visible string uses British spelling.

User-facing strings are written in US English. The frontend generates its
British English locale from them, and some reach it verbatim (settings and
feature descriptions become its SettingsDescription.ts and
FeaturesDescription.ts), so a British source string shows British spelling to
US users and is never converted for British ones.

Checked: gettext strings, feature descriptions (Feature("...") and the
sections' `description = "..."`), the help_text / verbose_name / label /
description arguments of any call, and the descriptions in
constance_settings.py.
Comments, docstrings, identifiers, migrations and tests are not checked.

Usage: python scripts/check_us_spelling.py src/
"""

import argparse
import ast
import re
import sys
from pathlib import Path

ISE_STEMS = [
    "authoris",
    "categoris",
    "centralis",
    "characteris",
    "customis",
    "finalis",
    "initialis",
    "maximis",
    "minimis",
    "normalis",
    "optimis",
    "organis",
    "personalis",
    "prioritis",
    "realis",
    "recognis",
    "specialis",
    "standardis",
    "summaris",
    "synchronis",
    "utilis",
    "visualis",
]
# Only with an inflection: the bare stems also start words spelled the same
# in both variants (analysis, realistic, specialist, organism).
ISE_SUFFIX = r"(?:e|ed|es|ing|er|ers|able|ably|ation|ations|ational)\b"
# "analyses" is also the US plural of "analysis", so it is not a British form.
YSE_FORM = r"analys(?:e|ed|ing|er|ers|able)\b"
OTHER_FORMS = [
    r"behaviour",
    r"colour",
    r"favour",
    r"honour",
    r"labour\b",
    r"neighbour",
    r"centred",
    r"centre",
    r"cancelled",
    r"cancelling",
    r"labelled",
    r"labelling",
    r"modelled",
    r"modelling",
    r"travelled",
    r"travelling",
    r"enrolment",
    r"fulfilment",
    r"catalogues?\b",
    r"defence",
    r"grey\b",
    r"enrols?\b",
    r"fulfils?\b",
    r"licences?\b",
]
BRITISH = re.compile(
    r"\b(?:(?:"
    + "|".join(ISE_STEMS)
    + ")"
    + ISE_SUFFIX
    + "|"
    + YSE_FORM
    + "|"
    + "|".join(OTHER_FORMS)
    + ")",
    re.IGNORECASE,
)
# Placeholders, identifiers and quoted single words are not prose: a quoted
# word names a value, such as the 'Cancelled' state, which stays as it is.
NOT_PROSE = re.compile(
    r"%\([^)]*\)\w|\{[^}]*\}|\b\w*_\w*\b|https?://\S+|'[^'\s]+'|\"[^\"\s]+\""
)

GETTEXT_FUNCTIONS = {
    "_",
    "gettext",
    "gettext_lazy",
    "ngettext",
    "ngettext_lazy",
    "pgettext",
    "pgettext_lazy",
    # Feature descriptions become the frontend's FeaturesDescription.ts.
    "Feature",
}
TEXT_KEYWORDS = {
    "help_text",
    "verbose_name",
    "verbose_name_plural",
    "label",
    "description",
}


def string_constants(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child


def user_visible_strings(tree, is_constance):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in GETTEXT_FUNCTIONS:
                for arg in node.args:
                    yield from string_constants(arg)
            for keyword in node.keywords:
                if keyword.arg in TEXT_KEYWORDS:
                    yield from string_constants(keyword.value)
        # Class attributes such as a feature section's Meta.description.
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in TEXT_KEYWORDS
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            yield node.value
        # CONSTANCE_CONFIG entries: (default, "description", ...)
        if (
            is_constance
            and isinstance(node, ast.Tuple)
            and len(node.elts) >= 2
            and isinstance(node.elts[1], ast.Constant)
            and isinstance(node.elts[1].value, str)
        ):
            yield node.elts[1]


def find_british(text):
    match = BRITISH.search(NOT_PROSE.sub(" ", text))
    return match.group(0) if match else None


def check(root):
    problems = []
    for path in sorted(root.rglob("*.py")):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seen = set()
        for constant in user_visible_strings(
            tree, path.name == "constance_settings.py"
        ):
            position = (constant.lineno, constant.col_offset)
            if position in seen:
                continue
            seen.add(position)
            word = find_british(constant.value)
            if word:
                problems.append((path, constant.lineno, word, constant.value))
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", type=Path, help="source directory, e.g. src/")
    args = parser.parse_args()

    problems = check(args.root)
    for path, line, word, text in problems:
        excerpt = " ".join(text.split())[:100]
        print(f'{path}:{line}: "{word}" in "{excerpt}"')
    if problems:
        print(
            f"\n{len(problems)} user-visible string(s) use British spelling. "
            "Write them in US English."
        )
        return 1
    print("User-visible strings use US spelling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
