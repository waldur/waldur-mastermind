import django_filters

from waldur_core.core import filters as core_filters

from . import models


class NotificationFilterSet(django_filters.FilterSet):
    class Meta:
        model = models.Notification
        fields = ('subject',)

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            ('created', 'created'),
            ('subject', 'subject'),
            (('author__first_name', 'author__last_name'), 'author_full_name'),
        )
    )
