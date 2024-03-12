import collections
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import F, OuterRef, Subquery, Sum
from django.db.models.query import QuerySet
from rest_framework import permissions, status, viewsets
from rest_framework.response import Response

from waldur_core.core.utils import get_ordering
from waldur_core.quotas.models import QuotaUsage
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.permissions import IsStaffOrSupportUser
from waldur_mastermind.billing.models import PriceEstimate

from . import models, serializers


class DailyQuotaHistoryViewSet(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []

    def list(self, request):
        serializer = serializers.DailyHistoryQuotaSerializer(
            data=request.query_params,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        result = self.get_result(serializer.validated_data)
        return Response(result)

    def get_result(self, query):
        scope = query["scope"]
        quota_names = query["quota_names"]
        start = query["start"]
        end = query["end"]

        quotas = models.DailyQuotaHistory.objects.filter(
            scope=scope,
            name__in=quota_names,
            date__gte=start,
            date__lte=end,
        ).only(
            "name",
            "date",
            "usage",
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


class BaseQuotasViewSet(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []
    permission_classes = (
        permissions.IsAuthenticated,
        IsStaffOrSupportUser,
    )

    model = None

    def get_queryset(self) -> QuerySet:
        qs = self.model
        if hasattr(qs, "available_objects"):
            return getattr(qs, "available_objects")
        else:
            return qs.objects

    def get_content_type(self):
        return ContentType.objects.get_for_model(self.model)

    def list(self, request):
        quota_name = request.query_params.get("quota_name")
        if not quota_name:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        ordering_field = get_ordering(request) or "value"
        if ordering_field not in ("value", "name"):
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if quota_name == "estimated_price":
            queryset = self.annotate_estimated_price()
        else:
            queryset = self.annotate_quotas(quota_name)

        queryset = queryset.order_by(F(ordering_field).desc(nulls_last=True))

        queryset = self.paginate_queryset(queryset)
        if self.model is Project:
            serializer_class = serializers.ProjectQuotasSerializer
        else:
            serializer_class = serializers.CustomerQuotasSerializer
        serializer = serializer_class(queryset, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data)

    def annotate_quotas(self, quota_name):
        quotas = (
            QuotaUsage.objects.filter(
                object_id=OuterRef("pk"),
                content_type=self.get_content_type(),
                name=quota_name,
            )
            .annotate(usage=Sum("delta"))
            .values("usage")
        )
        # Workaround for Django bug:
        # https://code.djangoproject.com/ticket/28296
        # It allows to remove extra GROUP BY clause from the subquery.
        quotas.query.group_by = []
        subquery = Subquery(quotas)
        return self.get_queryset().annotate(value=subquery)

    def annotate_estimated_price(self):
        estimates = PriceEstimate.objects.filter(
            object_id=OuterRef("pk"),
            content_type=self.get_content_type(),
        )
        subquery = Subquery(estimates.values("total")[:1])
        return self.get_queryset().annotate(value=subquery)


class ProjectQuotasViewSet(BaseQuotasViewSet):
    model = Project


class CustomerQuotasViewSet(BaseQuotasViewSet):
    model = Customer
