from __future__ import unicode_literals

import collections
from datetime import timedelta

from rest_framework import viewsets
from rest_framework.response import Response

from . import models, serializers


class DailyQuotaHistoryViewSet(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []

    def list(self, request):
        serializer = serializers.DailyHistoryQuotaSerializer(
            data=request.query_params,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        result = self.get_result(serializer.validated_data)
        return Response(result)

    def get_result(self, query):
        scope = query['scope']
        quota_names = query['quota_names']
        start = query['start']
        end = query['end']

        quotas = models.DailyQuotaHistory.objects.filter(
            scope=scope,
            name__in=quota_names,
            date__gte=start,
            date__lte=end,
        ).only(
            'name',
            'date',
            'usage',
        )
        charts = collections.defaultdict(dict)
        for quota in quotas:
            charts[quota.name][quota.date] = quota.usage

        values = collections.defaultdict(list)
        day = timedelta(days=1)
        days = (end - start).days
        for name in quota_names:
            usage = 0
            for i in range(days + 1):
                date = start + i * day
                usage = charts[name].get(date, usage)
                values[name].append(usage)
        return values
