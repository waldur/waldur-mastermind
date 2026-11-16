"""Full-text search over the documentation corpus, filtered for the caller.

A section reaches a caller only if its audience covers the caller's role and,
for end users, everything in its ``requires`` is switched on here. Staff and
support also get the other sections, marked ``enabled_here: False``, so they
can answer "why can't my users see X". ``enabled_here`` is true only when every
term is known to be on: a term this code does not know (a corpus newer or
older than the release) cannot be confirmed, so it counts as not enabled.
"""

import re
from dataclasses import dataclass, field

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.core.cache import cache
from django.db.models import Case, F, FloatField, Func, Value, When

import waldur_core
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat.deployment_profile import TermState
from waldur_mastermind.chat.docs import corpus
from waldur_mastermind.chat.docs.models import (
    DocAudience,
    DocKind,
    DocSection,
    written_vector,
)

# docs.waldur.com is versioned (mike): every page lives under /latest/, built
# from waldur-docs master, or under /<release>/, and the bare path is a 404. A
# release image carries the corpus of its own tag (Dockerfile,
# DOCS_CORPUS_VERSION), so its links go to that release's pages; every other
# build carries the `latest` corpus.
DOCS_SITE_URL = "https://docs.waldur.com"
LATEST_DOCS_VERSION = "latest"
_RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+")
MAX_RESULTS = 5
# The most a caller of the REST endpoint may ask for in one page.
MAX_PAGE_RESULTS = 20
MAX_QUERY_LENGTH = 300
# How many hidden sections are looked at to name the switched-off features a
# query is about.
HIDDEN_MATCHES = 3
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
    # Kept for switched_off_here(); not part of what callers are given.
    requires: tuple[str, ...] = field(default=(), repr=False)

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
    corpus.invalidate_cache()


def docs_version(release: str | None = None) -> str:
    """The docs.waldur.com version matching the loaded corpus: the release
    for a release build (X.Y.Z), else ``latest``."""
    release = waldur_core.__version__ if release is None else release
    if _RELEASE_VERSION.fullmatch(release or ""):
        return release
    return LATEST_DOCS_VERSION


def docs_base_url() -> str:
    # Not DOCS_URL: that is the portal header's link, usually the site root,
    # and not a base the corpus paths resolve under.
    return f"{DOCS_SITE_URL}/{docs_version()}"


def section_url(path: str, anchor: str, base: str | None = None) -> str:
    """The published URL of a section (MkDocs directory URLs). MkDocs
    publishes a README.md, like an index.md, as its directory's page."""
    base = base or docs_base_url()
    page = path.lstrip("/")
    if page.endswith(".md"):
        page = page.removesuffix(".md")
        head, _, name = page.rpartition("/")
        if name in ("index", "README"):
            page = f"{head}/" if head else ""
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
    # The word or a short inflection of it ("adds", "adding"), not a longer
    # word it starts: "add" must not start the excerpt inside "address". Only
    # when no word occurs that way, any occurrence ("admin" in
    # "administrator", "enabled" in "PAT_ENABLED").
    words = [w for w in _WORD.findall(query) if len(w) > 2]
    positions = [
        match.start()
        for word in words
        if (match := re.search(rf"\b{re.escape(word)}\w{{0,3}}\b", text, re.IGNORECASE))
    ]
    if not positions:
        lowered = text.lower()
        positions = [p for w in words if (p := lowered.find(w.lower())) >= 0]
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


# A query that asks what something is, or how two things differ, wants the
# section that explains it; one that asks how to do something does not. A
# blanket weight on concept pages pulled them into how-to answers, so the
# weight applies only to queries worded as a concept question. Both the
# user's question and the keywords a model searches with keep these words
# ("organization project difference"). "Why" is left out: "why can't I sign
# in" wants a how-to or troubleshooting section, not a concept page.
_CONCEPT_QUERY = re.compile(
    # "How X works" and "how does X work", but not "how do I work with X".
    r"\b(what\s+(is|are|does|happens)|what's|how\s+(\w+\s+){1,3}works|"
    r"how\s+(does|do)\s+(?!I\b|we\b|you\b)(\w+\s+){1,3}work|"
    r"difference|differences|differ|differs|"
    r"vs|versus|compared? (to|with)|comparison|meaning|means?|explain|explained|"
    r"concepts?)\b",
    re.IGNORECASE,
)
# Chosen on the validation questions (evaluation_questions_validation.yaml,
# waldur/waldur-docs#37): 2.0 brings the concept page up without costing
# how-to questions; 1.5 did not help, 3.0 cost staff questions.
CONCEPT_BOOST = 2.0


def is_concept_query(query: str) -> bool:
    return bool(_CONCEPT_QUERY.search(query))


def _ranked(sections, search_query, concept=False):
    weight = F("boost")
    if concept and CONCEPT_BOOST != 1.0:
        weight = F("boost") * Case(
            When(kind=DocKind.CONCEPT, then=Value(CONCEPT_BOOST)),
            default=Value(1.0),
            output_field=FloatField(),
        )
    return (
        sections.filter(search_vector=search_query)
        .annotate(rank=SearchRank(F("search_vector"), search_query) * weight)
        .order_by("-rank", "id")
        .only("path", "anchor", "title", "breadcrumb", "text", "requires")
    )


def search(user, query: str, limit: int = MAX_RESULTS) -> list[DocHit]:
    states = deployment_profile.get_term_states(_requires_terms())
    on = {term for term, state in states.items() if state == TermState.ON}

    privileged = user.is_staff or user.is_support
    sections = DocSection.objects.filter(audience__in=audiences_for(user))
    if not privileged:
        # An end user gets a section only if every term it requires is known
        # to be on. Unknown terms come from a corpus newer or older than this
        # code, and a term missing from the cached list (a corpus loaded since)
        # is not known either: both hide the section rather than describe a
        # menu that may not exist. Staff get them, marked enabled_here: false.
        sections = sections.filter(requires__contained_by=sorted(on))

    # Every word first. A how-to rarely uses the guide's own words ("new
    # project" against "Project creation"), so when that finds too little the
    # rest is filled with sections that match any of the words, the ones
    # matching more of them ranking higher.
    concept = is_concept_query(query)
    every_word = SearchQuery(query, search_type="websearch", config="english")
    hits = list(_ranked(sections, every_word, concept)[:limit])
    words = _WORD.findall(query)
    if len(hits) < limit and len(words) > 1:
        any_word = SearchQuery(" | ".join(words), search_type="raw", config="english")
        hits += _ranked(
            sections.exclude(pk__in=[h.pk for h in hits]), any_word, concept
        )[: limit - len(hits)]

    # Terms of a corpus loaded since the term list was cached are not in
    # `on` yet. End users were already filtered on it (failing closed); for
    # what staff are shown, ask about those terms now.
    uncached = {t for hit in hits for t in hit.requires} - set(states)
    if uncached:
        on |= {
            term
            for term, state in deployment_profile.get_term_states(
                sorted(uncached)
            ).items()
            if state == TermState.ON
        }
    base = docs_base_url()
    return [
        DocHit(
            title=section.title,
            breadcrumb=section.breadcrumb,
            excerpt=excerpt(section.text, query),
            url=section_url(section.path, section.anchor, base),
            enabled_here=set(section.requires) <= on,
            requires=tuple(section.requires),
        )
        for section in hits
    ]


def switched_off_here(user, query: str, hits: list[DocHit]) -> list[dict]:
    """Features switched off on this portal that the query is about.

    End users get no section of a switched-off feature, which left the
    assistant unable to tell "not enabled here" from "the portal cannot do
    that"; it told users Waldur has no chat. So the query is also matched,
    every word, against the sections hidden from the caller, and the features
    that hid them are named, never their content. For staff and support the
    features come from the returned sections marked ``enabled_here: False``.
    Only terms known to be off are named: an unknown term may well be on.
    """
    states = deployment_profile.get_term_states(_requires_terms())
    off = {term for term, state in states.items() if state == TermState.OFF}
    if not off:
        return []
    found = set()
    for hit in hits:
        if not hit.enabled_here:
            found |= off.intersection(hit.requires)
    if not (user.is_staff or user.is_support):
        hidden = DocSection.objects.filter(
            audience__in=audiences_for(user), requires__overlap=sorted(off)
        )
        every_word = SearchQuery(query, search_type="websearch", config="english")
        # Matched on what the section says, not on its `context`: the
        # questions written for a section are broad ("How do I get access to
        # the API?"), and naming a switched-off feature the user did not ask
        # about is worse than naming none. search_vector holds everything
        # written_vector() does, so the index narrows the rows first.
        matches = (
            hidden.filter(search_vector=every_word)
            .annotate(written=written_vector())
            .filter(written=every_word)
            .annotate(rank=SearchRank(F("written"), every_word))
            .order_by("-rank", "id")
            .only("requires")
        )
        for section in matches[:HIDDEN_MATCHES]:
            found |= off.intersection(section.requires)
    names: dict[str, str] = {}
    for term in sorted(found):
        names.setdefault(deployment_profile.term_name(term), term)
    return [{"name": name, "term": term} for name, term in sorted(names.items())]
