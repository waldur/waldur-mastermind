"""Choices for SRAM project rules (no model imports: the OpenAPI settings use them)."""

from django.db import models


class SourceKind(models.TextChoices):
    COLLABORATION = "co", "Collaboration"
    GROUP = "group", "Group"
    ANY = "any", "Any"


class ProjectField(models.TextChoices):
    BACKEND_ID = "backend_id", "Backend ID"
    SLUG = "slug", "Slug"


class MatchType(models.TextChoices):
    EXACT = "exact", "Exact"
    PREFIX = "prefix", "Prefix"
    REGEX = "regex", "Regular expression"
