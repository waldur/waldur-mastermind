import django_filters
from django.utils import timezone

from . import models


class UserActionFilter(django_filters.FilterSet):
    """Filter for user actions"""

    action_type = django_filters.CharFilter()
    urgency = django_filters.ChoiceFilter(
        choices=models.UserAction.UrgencyChoices.choices
    )
    is_silenced = django_filters.BooleanFilter()
    include_silenced = django_filters.BooleanFilter(method="filter_include_silenced")
    overdue = django_filters.BooleanFilter(method="filter_overdue")
    due_within_days = django_filters.NumberFilter(method="filter_due_within_days")
    created_after = django_filters.DateTimeFilter(
        field_name="created", lookup_expr="gte"
    )
    created_before = django_filters.DateTimeFilter(
        field_name="created", lookup_expr="lte"
    )

    class Meta:
        model = models.UserAction
        fields = [
            "action_type",
            "urgency",
            "is_silenced",
            "include_silenced",
            "overdue",
            "due_within_days",
            "created_after",
            "created_before",
        ]

    def filter_include_silenced(self, queryset, name, value):
        """Filter to include or exclude silenced actions"""
        if not value:
            # Exclude both permanently silenced and temporarily silenced actions
            now = timezone.now()
            queryset = queryset.filter(
                is_silenced=False,
            ).exclude(silenced_until__gt=now)
        return queryset

    def filter_overdue(self, queryset, name, value):
        """Filter for overdue actions"""
        if value:
            queryset = queryset.filter(
                due_date__lt=timezone.now(), due_date__isnull=False
            )
        elif value is False:
            queryset = queryset.exclude(
                due_date__lt=timezone.now(), due_date__isnull=False
            )
        return queryset

    def filter_due_within_days(self, queryset, name, value):
        """Filter for actions due within specified number of days"""
        from datetime import timedelta

        if value is not None:
            due_threshold = timezone.now() + timedelta(days=value)
            queryset = queryset.filter(
                due_date__lte=due_threshold, due_date__isnull=False
            )
        return queryset
