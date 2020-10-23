import django_filters


class NotificationFilterSet(django_filters.FilterSet):
    o = django_filters.OrderingFilter(
        fields=(
            ('created', 'created'),
            ('subject', 'subject'),
            ('author__full_name', 'author_full_name'),
        )
    )
