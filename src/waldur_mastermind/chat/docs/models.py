"""The documentation corpus the AI assistant searches (``search_docs``).

One row per section of the published documentation, loaded from the corpus
that waldur-docs publishes (waldur/waldur-docs#37) by ``load_docs_corpus``.
Postgres does the searching, so nothing of the corpus is held in the memory of
API or Celery processes.
"""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.utils.translation import gettext_lazy as _


class DocAudience(models.TextChoices):
    """Who a section is written for, set by docs CI from the page path."""

    ALL = "all", _("All users")
    # user-guide/staff-users/
    STAFF_SUPPORT = "staff_support", _("Staff and support")
    # admin-guide/
    STAFF = "staff", _("Staff")


class DocSection(models.Model):
    # Docs-relative source path ("user-guide/end-users/personal-access-tokens.md")
    # or site path ("user-guide/end-users/personal-access-tokens/").
    path = models.CharField(max_length=500)
    anchor = models.CharField(max_length=255, blank=True, default="")
    title = models.TextField()
    breadcrumb = models.TextField(blank=True, default="")
    text = models.TextField(blank=True, default="")
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
        expression=(
            SearchVector("title", weight="A", config="english")
            + SearchVector("breadcrumb", weight="B", config="english")
            + SearchVector("text", weight="C", config="english")
        ),
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
