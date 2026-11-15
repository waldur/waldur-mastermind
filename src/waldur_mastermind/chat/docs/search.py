"""Full-text search over the documentation corpus, filtered for the caller.

A section reaches a caller only if its audience covers the caller's role and,
for end users, everything in its ``requires`` is switched on here. Staff and
support also get sections for switched-off features, marked
``enabled_here: False``, so they can answer "why can't my users see X".
"""

import re
from dataclasses import dataclass

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.core.cache import cache
from django.db.models import F, Func

from waldur_core.configuration import config
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat.deployment_profile import TermState
from waldur_mastermind.chat.docs.models import DocAudience, DocSection

DEFAULT_DOCS_URL = "https://docs.waldur.com"
MAX_RESULTS = 5
# The most a caller of the REST endpoint may ask for in one page.
MAX_PAGE_RESULTS = 20
MAX_QUERY_LENGTH = 300
EXCERPT_LENGTH = 800

TERMS_CACHE_KEY = "waldur_mastermind.chat.docs.requires_terms"
TERMS_CACHE_TIMEOUT = 5 * 60


@dataclass
class DocHit:
    title: str
    breadcrumb: str
    excerpt: str
    url: str
    enabled_here: bool

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "breadcrumb": self.breadcrumb,
            "excerpt": self.excerpt,
            "url": self.url,
            "enabled_here": self.enabled_here,
        }


def audiences_for(user) -> list[str]:
    if user.is_staff:
        return list(DocAudience.values)
    if user.is_support:
        return [DocAudience.ALL, DocAudience.STAFF_SUPPORT]
    return [DocAudience.ALL]


def _requires_terms() -> list[str]:
    """Every distinct ``requires`` term in the loaded corpus."""
    terms = cache.get(TERMS_CACHE_KEY)
    if terms is None:
        terms = sorted(
            DocSection.objects.annotate(term=Func(F("requires"), function="unnest"))
            .values_list("term", flat=True)
            .distinct()
        )
        cache.set(TERMS_CACHE_KEY, terms, TERMS_CACHE_TIMEOUT)
    return terms


def invalidate_cache() -> None:
    cache.delete(TERMS_CACHE_KEY)


def docs_base_url() -> str:
    return (config.DOCS_URL or DEFAULT_DOCS_URL).rstrip("/")


def section_url(path: str, anchor: str, base: str | None = None) -> str:
    """The published URL of a section (MkDocs directory URLs)."""
    base = base or docs_base_url()
    page = path.lstrip("/")
    if page.endswith(".md"):
        page = page.removesuffix(".md")
        if page == "index":
            page = ""
        elif page.endswith("/index"):
            page = page.removesuffix("index")
        else:
            page += "/"
    url = f"{base}/{page}"
    return f"{url}#{anchor}" if anchor else url


_WORD = re.compile(r"\w+")


def excerpt(text: str, query: str, length: int = EXCERPT_LENGTH) -> str:
    """About ``length`` characters of ``text``, starting shortly before the
    first word of ``query`` that occurs in it."""
    text = " ".join(text.split())
    if len(text) <= length:
        return text
    lowered = text.lower()
    positions = [
        lowered.find(word)
        for word in (w.lower() for w in _WORD.findall(query))
        if len(word) > 2
    ]
    positions = [p for p in positions if p >= 0]
    start = max(0, min(positions) - length // 4) if positions else 0
    if start:
        # Start on a word boundary.
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < start + 40 else start
    end = start + length
    if end < len(text):
        space = text.rfind(" ", start, end)
        end = space if space > start else end
    prefix = "… " if start else ""
    suffix = " …" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def search(user, query: str, limit: int = MAX_RESULTS) -> list[DocHit]:
    terms = _requires_terms()
    states = deployment_profile.get_term_states(terms)
    off = {term for term, state in states.items() if state == TermState.OFF}
    unknown = {term for term, state in states.items() if state == TermState.UNKNOWN}

    privileged = user.is_staff or user.is_support
    # Unknown terms come from a corpus newer or older than this code. Staff can
    # judge such a section; an end user would be told about a menu that may
    # not exist.
    excluded = set() if privileged else off | unknown

    search_query = SearchQuery(query, search_type="websearch", config="english")
    sections = DocSection.objects.filter(
        search_vector=search_query, audience__in=audiences_for(user)
    )
    if excluded:
        sections = sections.exclude(requires__overlap=sorted(excluded))
    sections = (
        sections.annotate(
            rank=SearchRank(F("search_vector"), search_query) * F("boost")
        )
        .order_by("-rank", "id")
        .only("path", "anchor", "title", "breadcrumb", "text", "requires")[:limit]
    )
    base = docs_base_url()
    return [
        DocHit(
            title=section.title,
            breadcrumb=section.breadcrumb,
            excerpt=excerpt(section.text, query),
            url=section_url(section.path, section.anchor, base),
            enabled_here=not off.intersection(section.requires),
        )
        for section in sections
    ]
