"""Filters for the staff/support anonymous-chat read ViewSets."""

import django_filters
from django.contrib.postgres.search import SearchQuery
from django.db.models import Q

from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.input_guards import SeverityLevel


class AnonymousChatInteractionFilter(django_filters.FilterSet):
    created_from = django_filters.DateFilter(
        field_name="created", lookup_expr="date__gte"
    )
    created_to = django_filters.DateFilter(
        field_name="created", lookup_expr="date__lte"
    )

    query = django_filters.CharFilter(method="filter_by_query")

    is_flagged = django_filters.BooleanFilter(field_name="is_flagged")

    severity = django_filters.ChoiceFilter(
        choices=[(s.value, s.value.title()) for s in SeverityLevel],
        field_name="severity",
    )
    session_id = django_filters.CharFilter(field_name="session_id")
    user_slug = django_filters.CharFilter(field_name="user_slug")

    has_negative_feedback = django_filters.BooleanFilter(
        method="filter_has_negative_feedback"
    )

    o = django_filters.OrderingFilter(
        fields=(
            "created",
            "result_count",
        )
    )

    class Meta:
        model = anonymous_models.AnonymousChatInteraction
        fields = ["is_flagged", "severity"]

    def filter_by_query(self, queryset, name, value):
        """Search transcript content and the per-actor slug.

        Content search uses Postgres full-text over ``search_vector``
        (a stored generated column), which covers both the user's question
        and the assistant's reply. `websearch` syntax gives staff
        quoted-phrase support and tolerates arbitrary input without erroring.
        There is no thread title on the anon path, so ``user_slug`` stands in
        for the auth side's name/user-identity matching.
        """
        # No .distinct(): both branches match columns on the interaction row
        # itself (no row-multiplying join), so a row can appear at most once.
        return queryset.filter(
            Q(user_slug__icontains=value)
            | Q(
                search_vector=SearchQuery(
                    value, search_type="websearch", config="english"
                )
            )
        )

    def filter_has_negative_feedback(self, queryset, name, value):
        if value:
            return queryset.filter(feedback__score=-1)
        return queryset.exclude(feedback__score=-1)


class AnonymousChatFeedbackFilter(django_filters.FilterSet):
    score = django_filters.NumberFilter(field_name="score")
    category = django_filters.CharFilter(field_name="category")
    submitted_from = django_filters.DateFilter(
        field_name="submitted_at", lookup_expr="date__gte"
    )
    submitted_to = django_filters.DateFilter(
        field_name="submitted_at", lookup_expr="date__lte"
    )
    has_comment = django_filters.BooleanFilter(method="filter_has_comment")

    o = django_filters.OrderingFilter(
        fields=(
            "submitted_at",
            "score",
            "llm_resolution_score",
        )
    )

    class Meta:
        model = anonymous_models.AnonymousChatFeedback
        fields = ["score", "category"]

    def filter_has_comment(self, queryset, name, value):
        if value:
            return queryset.exclude(comment="")
        return queryset.filter(comment="")
