"""Loading the documentation corpus that ships in the image.

The image build downloads ``docs-corpus.jsonl.gz`` from the waldur-docs package
registry (waldur/waldur-docs#37). Each line is one section::

    {"path": ..., "anchor": ..., "title": ..., "breadcrumb": ..., "text": ...,
     "audience": "all" | "staff_support" | "staff", "requires": [...],
     "boost": 1.5}

``boost`` is optional. When the download failed there is no file, and the
assistant has no documentation tool (see ``is_available``).
"""

import gzip
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction

from waldur_mastermind.chat.docs.models import DocAudience, DocSection

logger = logging.getLogger(__name__)

DEFAULT_CORPUS_PATH = "/usr/share/waldur/docs-corpus/docs-corpus.jsonl.gz"

_FIELDS = ("title", "breadcrumb", "text", "audience", "requires", "boost")


def corpus_path() -> Path:
    return Path(os.environ.get("WALDUR_DOCS_CORPUS_PATH") or DEFAULT_CORPUS_PATH)


def is_available() -> bool:
    """Whether this image carries a documentation corpus."""
    return corpus_path().is_file()


class CorpusError(ValueError):
    pass


@dataclass
class LoadResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0

    @property
    def written(self) -> int:
        return self.created + self.updated + self.deleted


def _parse_line(raw: str, line_no: int) -> dict:
    try:
        item = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"line {line_no}: not JSON ({exc})") from exc
    if not isinstance(item, dict):
        raise CorpusError(f"line {line_no}: not an object")
    path = item.get("path")
    title = item.get("title")
    if not isinstance(path, str) or not path or not isinstance(title, str):
        raise CorpusError(f"line {line_no}: path and title are required")
    audience = item.get("audience") or DocAudience.ALL
    if audience not in DocAudience.values:
        raise CorpusError(f"line {line_no}: unknown audience {audience!r}")
    requires = item.get("requires") or []
    if not isinstance(requires, list) or not all(
        isinstance(term, str) for term in requires
    ):
        raise CorpusError(f"line {line_no}: requires must be a list of strings")
    try:
        boost = float(item.get("boost", 1.0))
    except (TypeError, ValueError) as exc:
        raise CorpusError(f"line {line_no}: boost must be a number") from exc
    section = {
        "path": path,
        "anchor": item.get("anchor") or "",
        "title": title,
        "breadcrumb": item.get("breadcrumb") or "",
        "text": item.get("text") or "",
        "audience": audience,
        "requires": sorted(set(requires)),
        "boost": boost,
    }
    section["content_hash"] = hashlib.sha256(
        json.dumps(section, sort_keys=True).encode()
    ).hexdigest()
    return section


def read_corpus(path: Path) -> list[dict]:
    """The sections in a corpus file. Later duplicates of a section are skipped."""
    sections: dict[tuple[str, str], dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_no, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            section = _parse_line(raw, line_no)
            key = (section["path"], section["anchor"])
            if key in sections:
                logger.warning("Docs corpus: duplicate section %s#%s", *key)
                continue
            sections[key] = section
    return list(sections.values())


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
    return result


def load_corpus(path: Path | None = None) -> LoadResult | None:
    """Load the corpus at ``path``; ``None`` when there is no corpus file."""
    path = path or corpus_path()
    if not path.is_file():
        return None
    return load_sections(read_corpus(path))
