import django_filters
from django.utils import timezone

from waldur_core.core import filters as core_filters

from . import models


class BroadcastMessageFilterSet(django_filters.FilterSet):
    subject = django_filters.CharFilter(lookup_expr="icontains")

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            ("created", "created"),
            ("subject", "subject"),
            (("author__first_name", "author__last_name"), "author_full_name"),
        )
    )

    class Meta:
        model = models.BroadcastMessage
        fields = ("state",)


class MessageTemplateFilterSet(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")


class AdminAnnouncementFilterSet(django_filters.FilterSet):
    description = django_filters.CharFilter(lookup_expr="icontains")
    type = django_filters.MultipleChoiceFilter(
        choices=models.AdminAnnouncement.Type.CHOICES
    )
    is_active = django_filters.BooleanFilter(method="filter_is_active")

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            ("created", "created"),
            ("name", "name"),
            ("type", "type"),
            ("active_from", "active_from"),
            ("active_to", "active_to"),
        )
    )

    def filter_is_active(self, queryset, name, value):
        now = timezone.now()
        if value in [True, "true", "True"]:
            return queryset.filter(active_from__lte=now, active_to__gt=now)
        else:
            return queryset.exclude(active_from__lte=now, active_to__gt=now)

    class Meta:
        model = models.AdminAnnouncement
        fields = ()
