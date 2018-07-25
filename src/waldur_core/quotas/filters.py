from __future__ import unicode_literals

import django_filters

from waldur_core.quotas import models


class QuotaFilter(django_filters.NumberFilter):
    """
    Filter by quota value
    """

    def __init__(self, quota_name, quota_field, **kwargs):
        super(QuotaFilter, self).__init__(**kwargs)
        self.quota_name = quota_name
        self.quota_field = quota_field

    def filter(self, qs, value):
        return qs.filter(**{'quotas__name': self.quota_name, 'quotas__{}'.format(self.quota_field): value})


class QuotaFilterSet(django_filters.FilterSet):
    """
    FilterSet for quotas view
    """
    name = django_filters.CharFilter(
        lookup_expr='icontains',
    )

    class Meta(object):
        model = models.Quota
        fields = [
            'name'
        ]
