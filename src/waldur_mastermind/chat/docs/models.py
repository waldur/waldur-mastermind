"""The documentation corpus the AI assistant searches (``search_docs``).

One row per section of the published documentation, loaded from the corpus
that waldur-docs publishes (waldur/waldur-docs#37) by ``load_docs_corpus``.
Postgres does the searching, so nothing of the corpus is held in the memory of
API or Celery processes.
"""

import functools
import operator

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.utils.translation import gettext_lazy as _

# What the search matches, in order and with weights. The order matters:
# ts_rank scores an every-word match by how close the words are, so moving a
# field moves rankings; this is the order the search was measured with
# (waldur/waldur-docs#37).
SEARCH_FIELDS = (("title", "A"), ("breadcrumb", "B"), ("context", "B"), ("text", "C"))


def _vector(fields):
    return functools.reduce(
        operator.add,
        (
            SearchVector(name, weight=weight, config="english")
            for name, weight in fields
        ),
    )


def written_vector():
    """What a section says: ``search_vector`` without the generated questions
    in ``context`` (``search.switched_off_here`` matches on this)."""
    return _vector(f for f in SEARCH_FIELDS if f[0] != "context")


class DocAudience(models.TextChoices):
    """Who a section is written for, set by docs CI from the page path."""

    ALL = "all", _("All users")
    # user-guide/staff-users/
    STAFF_SUPPORT = "staff_support", _("Staff and support")
    # admin-guide/
    STAFF = "staff", _("Staff")


class DocKind(models.TextChoices):
    """What a section is for, in the Diátaxis sense, set by docs CI."""

    HOWTO = "howto", _("How-to")
    CONCEPT = "concept", _("Concept")
    REFERENCE = "reference", _("Reference")


class DocSection(models.Model):
    # Docs-relative source path ("user-guide/end-users/personal-access-tokens.md")
    # or site path ("user-guide/end-users/personal-access-tokens/").
    path = models.CharField(max_length=500)
    anchor = models.CharField(max_length=255, blank=True, default="")
    title = models.TextField()
    breadcrumb = models.TextField(blank=True, default="")
    text = models.TextField(blank=True, default="")
    # Words that place the section without being shown: the page's
    # description and the questions it answers, from the page's front matter.
    context = models.TextField(blank=True, default="")
    # Blank when the docs do not say.
    kind = models.CharField(
        max_length=20, choices=DocKind.choices, blank=True, default=""
    )
    audience = models.CharField(
        max_length=20, choices=DocAudience.choices, default=DocAudience.ALL
    )
    # Site configuration the section depends on, in the vocabulary of
    # chat.deployment_profile: "setting:PAT_ENABLED", "!feature:x.y", ...
    requires = ArrayField(models.CharField(max_length=255), default=list, blank=True)
    # The page's search boost from waldur-docs' hooks/search_weights.py.
    boost = models.FloatField(default=1.0)
    # sha256 of the corpus line, so reloading an unchanged corpus writes nothing.
    content_hash = models.CharField(max_length=64)
    # A generated column rather than a trigger, so it also exists when the
    # schema is built from the models (pytest --no-migrations, migrate_fresh).
    search_vector = models.GeneratedField(
        expression=_vector(SEARCH_FIELDS),
        output_field=SearchVectorField(),
        db_persist=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["path", "anchor"], name="chat_docsection_path_anchor"
            ),
        ]
        indexes = [
            GinIndex(fields=["search_vector"], name="chat_docsection_search_gin"),
        ]

    def __str__(self):
        return f"{self.path}#{self.anchor}" if self.anchor else self.path
