"""Filters for the staff/support anonymous-chat read ViewSets."""

import django_filters
from django.contrib.postgres.search import SearchQuery
from django.db.models import Q

from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.input_guards import SeverityLevel


class AnonymousChatInteractionFilter(django_filters.FilterSet):
    created_after = django_filters.DateFilter(
        field_name="created", lookup_expr="date__gte"
    )
    created_before = django_filters.DateFilter(
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

    # Any feedback, matching the authenticated tab's has_feedback rather than
    # only the negative subset — the narrower version could not answer "which
    # conversations did visitors rate at all".
    has_feedback = django_filters.BooleanFilter(method="filter_has_feedback")

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

    def filter_has_feedback(self, queryset, name, value):
        if value:
            return queryset.filter(feedback__score__isnull=False)
        return queryset.filter(feedback__score__isnull=True)


class AnonymousChatConversationFilter(django_filters.FilterSet):
    """Conversation-level narrowing, applied to the grouped queryset.

    Deliberately separate from :class:`AnonymousChatInteractionFilter`: these
    predicates read aggregates, so django-filter emits them as HAVING and they
    select whole conversations. On the interaction filterset the same names
    would filter turns, and the surviving turns would then be re-aggregated
    into a row that under-reports every other column.

    ``field_name`` targets the ``_sum`` aliases from
    :func:`conversation_queryset` rather than the concrete token columns,
    which share the public field names.
    """

    input_tokens_min = django_filters.NumberFilter(
        field_name="input_tokens_sum", lookup_expr="gte"
    )
    input_tokens_max = django_filters.NumberFilter(
        field_name="input_tokens_sum", lookup_expr="lte"
    )
    output_tokens_min = django_filters.NumberFilter(
        field_name="output_tokens_sum", lookup_expr="gte"
    )
    output_tokens_max = django_filters.NumberFilter(
        field_name="output_tokens_sum", lookup_expr="lte"
    )
    total_tokens_min = django_filters.NumberFilter(
        field_name="total_tokens", lookup_expr="gte"
    )
    total_tokens_max = django_filters.NumberFilter(
        field_name="total_tokens", lookup_expr="lte"
    )
    # Named after the aggregate rather than the column the UI labels
    # "Modified": last_active is Max(created), so a bare date must compare on
    # the date part or an inclusive upper bound would resolve to midnight and
    # drop the whole final day.
    last_active_after = django_filters.DateFilter(
        field_name="last_active", lookup_expr="date__gte"
    )
    last_active_before = django_filters.DateFilter(
        field_name="last_active", lookup_expr="date__lte"
    )

    # Reads the reviewed_count annotation rather than the feedback column, so
    # django-filter emits HAVING and selects whole conversations — the same
    # reason every other predicate on this filterset targets an aggregate.
    is_reviewed = django_filters.BooleanFilter(method="filter_is_reviewed")

    def filter_is_reviewed(self, queryset, name, value):
        if value:
            return queryset.filter(reviewed_count__gt=0)
        return queryset.filter(reviewed_count=0)

    class Meta:
        model = anonymous_models.AnonymousChatInteraction
        fields = []


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
