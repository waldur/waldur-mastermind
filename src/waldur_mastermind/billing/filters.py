from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Subquery
from rest_framework.filters import BaseFilterBackend

from waldur_core.core.utils import get_ordering, order_with_nulls
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import utils as invoice_utils

from . import models


class CustomerEstimatedCostFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):

        order_by = get_ordering(request)
        if order_by not in ('estimated_cost', '-estimated_cost'):
            return queryset

        ct = ContentType.objects.get_for_model(structure_models.Customer)
        estimates = models.PriceEstimate.objects.filter(
            content_type=ct, object_id=OuterRef('pk')
        )
        queryset = queryset.annotate(
            estimated_cost=Subquery(estimates.values('total')[:1])
        )
        return order_with_nulls(queryset, order_by)


class CustomerTotalCostFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):

        order_by = get_ordering(request)
        if order_by not in ('total_cost', '-total_cost'):
            return queryset

        year, month = invoice_utils.parse_period(request.query_params)
        invoices = invoice_models.Invoice.objects.filter(
            year=year, month=month, customer=OuterRef('pk')
        )
        queryset = queryset.annotate(
            total_cost=Subquery(invoices.values('total_cost')[:1])
        )
        return order_with_nulls(queryset, order_by)
