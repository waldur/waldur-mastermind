"""Loading the documentation corpus that ships in the image.

The image build downloads ``docs-corpus.jsonl.gz`` from the waldur-docs package
registry (waldur/waldur-docs#37). Each line is one section::

    {"path": ..., "anchor": ..., "title": ..., "breadcrumb": ..., "text": ...,
     "audience": "all" | "staff_support" | "staff", "requires": [...],
     "boost": 1.5, "kind": "howto" | "concept" | "reference", "context": ...}

``boost``, ``kind`` and ``context`` are optional; a ``kind`` this code does not
know is dropped, since it only steers ranking. When the download failed there is no file, nothing is
loaded, and the assistant has no documentation tool (see ``is_available``).

Loading runs on every deploy (initdb), so a bad corpus must not stop one. A
line that does not parse, or names an audience or ``requires`` shape this code
does not know, is skipped with a warning: an unknown audience cannot be
filtered safely, so leaving the section out is the fail-closed choice, and the
rest of the corpus still loads. A file that cannot be read at all, or has no
valid line, raises ``CorpusError`` and the loaded sections are left as they
were.
"""

import gzip
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

from django.core.cache import cache
from django.db import transaction

from waldur_core.configuration import config
from waldur_mastermind.chat.docs.models import DocAudience, DocKind, DocSection

logger = logging.getLogger(__name__)

DEFAULT_CORPUS_PATH = "/usr/share/waldur/docs-corpus/docs-corpus.jsonl.gz"

_FIELDS = (
    "title",
    "breadcrumb",
    "text",
    "context",
    "kind",
    "audience",
    "requires",
    "boost",
)


def corpus_path() -> Path:
    return Path(os.environ.get("WALDUR_DOCS_CORPUS_PATH") or DEFAULT_CORPUS_PATH)


LOADED_CACHE_KEY = "waldur_mastermind.chat.docs.loaded"
LOADED_CACHE_TIMEOUT = 5 * 60


def is_available() -> bool:
    """Whether a documentation corpus is loaded, so a search can find anything.

    A corpus file the image carries but that was never loaded, or loaded empty,
    would answer "not covered" to every question; that counts as unavailable.
    """
    loaded = cache.get(LOADED_CACHE_KEY)
    if loaded is None:
        loaded = DocSection.objects.exists()
        cache.set(LOADED_CACHE_KEY, loaded, LOADED_CACHE_TIMEOUT)
    return loaded


def invalidate_cache() -> None:
    cache.delete(LOADED_CACHE_KEY)


def search_enabled() -> bool:
    """Whether documentation search is offered: a corpus is loaded and
    DOCS_SEARCH_ENABLED is on."""
    return bool(config.DOCS_SEARCH_ENABLED) and is_available()


class CorpusError(ValueError):
    pass


@dataclass
class LoadResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: int = 0

    @property
    def written(self) -> int:
        return self.created + self.updated + self.deleted


def _text(item: dict, key: str, line_no: int, max_length: int | None = None) -> str:
    """An optional string field, checked against its column's length: one bad
    value must skip its line, not fail the transaction that loads them all."""
    value = item.get(key) or ""
    if not isinstance(value, str):
        raise CorpusError(f"line {line_no}: {key} must be a string")
    if max_length and len(value) > max_length:
        raise CorpusError(f"line {line_no}: {key} is longer than {max_length}")
    if "\x00" in value:
        # Postgres text cannot hold NUL.
        raise CorpusError(f"line {line_no}: {key} contains a NUL character")
    return value


def _parse_line(raw: str, line_no: int) -> dict:
    try:
        item = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"line {line_no}: not JSON ({exc})") from exc
    if not isinstance(item, dict):
        raise CorpusError(f"line {line_no}: not an object")
    path = _text(item, "path", line_no, DocSection._meta.get_field("path").max_length)
    title = item.get("title")
    if not path or not isinstance(title, str) or not title:
        raise CorpusError(f"line {line_no}: path and title are required")
    title = _text(item, "title", line_no)
    anchor = _text(
        item, "anchor", line_no, DocSection._meta.get_field("anchor").max_length
    )
    audience = item.get("audience") or DocAudience.ALL
    if audience not in DocAudience.values:
        raise CorpusError(f"line {line_no}: unknown audience {audience!r}")
    requires = item.get("requires") or []
    term_length = DocSection._meta.get_field("requires").base_field.max_length
    if not isinstance(requires, list) or not all(
        isinstance(term, str) and len(term) <= term_length and "\x00" not in term
        for term in requires
    ):
        raise CorpusError(
            f"line {line_no}: requires must be a list of strings of at most "
            f"{term_length} characters"
        )
    try:
        boost = float(item.get("boost", 1.0))
    except (TypeError, ValueError) as exc:
        raise CorpusError(f"line {line_no}: boost must be a number") from exc
    # NaN would sort first for every query.
    if not math.isfinite(boost) or boost < 0:
        raise CorpusError(
            f"line {line_no}: boost must be a finite number, not negative"
        )
    kind = item.get("kind") or ""
    if kind not in DocKind.values:
        kind = ""
    context = _text(item, "context", line_no)
    section = {
        "path": path,
        "anchor": anchor,
        "title": title,
        "breadcrumb": _text(item, "breadcrumb", line_no),
        "text": _text(item, "text", line_no),
        "context": context,
        "kind": kind,
        "audience": audience,
        "requires": sorted(set(requires)),
        "boost": boost,
    }
    section["content_hash"] = hashlib.sha256(
        json.dumps(section, sort_keys=True).encode()
    ).hexdigest()
    return section


def read_corpus(path: Path) -> tuple[list[dict], int]:
    """The valid sections in a corpus file, and how many lines were skipped.

    Invalid lines and later duplicates of a section are skipped with a
    warning. Raises ``CorpusError`` when the file cannot be read or holds no
    valid section, so that the caller keeps what is loaded.
    """
    sections: dict[tuple[str, str], dict] = {}
    skipped = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line_no, raw in enumerate(stream, start=1):
                if not raw.strip():
                    continue
                try:
                    section = _parse_line(raw, line_no)
                except CorpusError as exc:
                    logger.warning("Docs corpus %s: skipped %s", path, exc)
                    skipped += 1
                    continue
                key = (section["path"], section["anchor"])
                if key in sections:
                    logger.warning("Docs corpus: duplicate section %s#%s", *key)
                    continue
                sections[key] = section
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise CorpusError(f"cannot be read ({exc})") from exc
    if not sections:
        raise CorpusError(f"no valid section ({skipped} invalid line(s))")
    return list(sections.values()), skipped


@transaction.atomic
def load_sections(sections: list[dict]) -> LoadResult:
    """Make ``DocSection`` match ``sections``, writing only what changed."""
    result = LoadResult()
    existing = {
        (row.path, row.anchor): row
        for row in DocSection.objects.only("path", "anchor", "content_hash")
    }
    to_create, to_update = [], []
    for section in sections:
        key = (section["path"], section["anchor"])
        row = existing.pop(key, None)
        if row is None:
            to_create.append(DocSection(**section))
        elif row.content_hash != section["content_hash"]:
            for field in (*_FIELDS, "content_hash"):
                setattr(row, field, section[field])
            to_update.append(row)
        else:
            result.unchanged += 1

    if existing:
        result.deleted, _ = DocSection.objects.filter(
            pk__in=[row.pk for row in existing.values()]
        ).delete()
    if to_create:
        DocSection.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        DocSection.objects.bulk_update(
            to_update, [*_FIELDS, "content_hash"], batch_size=500
        )
    result.created = len(to_create)
    result.updated = len(to_update)
    transaction.on_commit(invalidate_cache)
    return result


def load_corpus(path: Path | None = None) -> LoadResult | None:
    """Load the corpus at ``path``; ``None`` when there is no corpus file.

    Raises ``CorpusError``, without touching the loaded sections, when the file
    is unreadable or has no valid section.
    """
    path = path or corpus_path()
    if not path.is_file():
        return None
    sections, skipped = read_corpus(path)
    result = load_sections(sections)
    result.skipped = skipped
    return result
