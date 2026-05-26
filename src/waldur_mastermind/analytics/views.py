import collections
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import F, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet
from django.utils.translation import gettext_lazy as _
from drf_spectacular.openapi import OpenApiParameter
from drf_spectacular.plumbing import (
    OpenApiTypes,
    build_array_type,
    build_basic_type,
)
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import generics, permissions, status, viewsets
from rest_framework import serializers as rf_serializers
from rest_framework.response import Response

from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.utils import SubquerySum, get_ordering
from waldur_core.quotas.models import QuotaUsage
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.billing.models import PriceEstimate

from . import models as analytics_models
from . import serializers


class QuotaName(models.TextChoices):
    NC_RESOURCE_COUNT = "nc_resource_count", _("Resources")
    ESTIMATED_PRICE = "estimated_price", _("Estimated price per month")
    VPC_CPU_COUNT = "vpc_cpu_count", _("VPC vCPU")
    VPC_RAM_SIZE = "vpc_ram_size", _("VPC RAM")
    VPC_STORAGE_SIZE = "vpc_storage_size", _("VPC block storage size")
    VPC_FLOATING_IP_COUNT = "vpc_floating_ip_count", _("VPC floating IP count")
    VPC_INSTANCE_COUNT = "vpc_instance_count", _("VPC instance count")
    OS_CPU_COUNT = "os_cpu_count", _("Cloud vCPU")
    OS_RAM_SIZE = "os_ram_size", _("Cloud RAM")
    OS_STORAGE_SIZE = "os_storage_size", _("Cloud block storage size")


QUOTA_NAME_PARAMETER = OpenApiParameter(
    name="quota_name",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Name of the quota",
    enum=QuotaName.values,
    extensions={
        "x-enum-descriptions": [str(label) for _, label in QuotaName.choices],
    },
)


class DailyQuotaHistoryViewSet(generics.GenericAPIView):
    filter_backends = []
    pagination_class = None
    serializer_class = EmptySerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="scope",
                description="UUID of the scope object",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="quota_names",
                description="List of quota names",
                type=build_array_type(build_basic_type(OpenApiTypes.STR)),
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="start",
                description="Start date in format YYYY-MM-DD",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="end",
                description="End date in format YYYY-MM-DD",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
            ),
        ],
        responses={200: dict[str, list[int]]},
    )
    def get(self, request):
        serializer = serializers.DailyHistoryQuotaSerializer(
            data=request.query_params, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        result = self.get_result(serializer.validated_data)
        return Response(result)

    def get_result(self, query):
        scope = query["scope"]
        quota_names = query["quota_names"]
        start = query["start"]
        end = query["end"]
        content_type = ContentType.objects.get_for_model(scope)

        quotas = analytics_models.DailyQuotaHistory.objects.filter(
            object_id=scope.id,
            content_type=content_type,
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
    permission_classes = (permissions.IsAuthenticated,)

    model = None

    def get_queryset(self) -> QuerySet:
        qs = self.model
        if hasattr(qs, "available_objects"):
            return filter_queryset_for_user(qs.available_objects, self.request.user)
        else:
            return filter_queryset_for_user(qs.objects, self.request.user)

    def get_content_type(self):
        return ContentType.objects.get_for_model(self.model)

    @extend_schema(parameters=[QUOTA_NAME_PARAMETER])
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
        quotas = QuotaUsage.objects.filter(
            object_id=OuterRef("pk"),
            content_type=self.get_content_type(),
            name=quota_name,
        )
        value = Coalesce(SubquerySum(quotas, "delta"), Value(0))
        return self.get_queryset().annotate(value=value)

    def annotate_estimated_price(self):
        estimates = PriceEstimate.objects.filter(
            object_id=OuterRef("pk"),
            content_type=self.get_content_type(),
        )
        subquery = Subquery(estimates.values("total")[:1])
        return self.get_queryset().annotate(value=subquery)


@extend_schema_view(
    list=extend_schema(
        description="List project quotas.",
        responses=serializers.ProjectQuotasSerializer,
        parameters=[QUOTA_NAME_PARAMETER],
    )
)
class ProjectQuotasViewSet(BaseQuotasViewSet):
    model = Project


@extend_schema_view(
    list=extend_schema(
        description="List customer quotas.",
        responses=serializers.CustomerQuotasSerializer,
        parameters=[QUOTA_NAME_PARAMETER],
    )
)
class CustomerQuotasViewSet(BaseQuotasViewSet):
    model = Customer
