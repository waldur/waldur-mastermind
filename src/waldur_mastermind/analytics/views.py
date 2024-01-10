import collections
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Subquery, Sum
from rest_framework import status, viewsets
from rest_framework.response import Response

from waldur_core.quotas.models import QuotaUsage
from waldur_core.structure.models import Project
from waldur_mastermind.billing.models import PriceEstimate
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.invoices.utils import get_current_month, get_current_year

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


class ProjectQuotasViewSet(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []

    def list(self, request):
        quota_name = request.query_params.get("quota_name")
        if not quota_name:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        content_type = ContentType.objects.get_for_model(Project)
        if quota_name == "estimated_price":
            projects = self.annotate_estimated_price(content_type)
        elif quota_name == "current_price":
            projects = self.annotate_current_price(content_type)
        else:
            projects = self.annotate_quotas(quota_name, content_type)

        return Response(
            [
                {
                    "project_name": project.name,
                    "customer_name": project.customer.name,
                    "customer_abbreviation": project.customer.abbreviation,
                    "value": project.value,
                }
                for project in projects
            ]
        )

    def annotate_quotas(self, quota_name, content_type):
        quotas = (
            QuotaUsage.objects.filter(
                object_id=OuterRef("pk"),
                content_type=content_type,
                name=quota_name,
            )
            .annotate(usage=Sum("usage"))
            .values("usage")
        )
        subquery = Subquery(quotas)
        return Project.available_objects.annotate(value=subquery)

    def annotate_estimated_price(self, content_type):
        estimates = PriceEstimate.objects.filter(
            object_id=OuterRef("pk"),
            content_type=content_type,
        )
        subquery = Subquery(estimates.values("total")[:1])
        return Project.available_objects.annotate(value=subquery)

    def annotate_current_price(self, content_type):
        projects = Project.available_objects.all()
        year, month = get_current_year(), get_current_month()
        for project in projects:
            items = InvoiceItem.objects.filter(
                invoice__year=year, invoice__month=month, project_id=project.id
            )
            project.value = sum(item.price_current for item in items)
        return projects
