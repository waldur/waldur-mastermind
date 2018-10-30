from __future__ import unicode_literals

from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Subquery

from waldur_core.core import filters as core_filters
from waldur_core.core.utils import order_with_nulls, get_ordering
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import utils as invoice_utils

from . import models


class PriceEstimateScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return models.PriceEstimate.get_estimated_models()

    def get_field_name(self):
        return 'scope'


class CustomerEstimatedCostFilter(core_filters.BaseExternalFilter):
    def filter(self, request, queryset, view):

        order_by = get_ordering(request)
        if order_by not in ('estimated_cost', '-estimated_cost'):
            return queryset

        ct = ContentType.objects.get_for_model(structure_models.Customer)
        estimates = models.PriceEstimate.objects.filter(content_type=ct, object_id=OuterRef('pk'))
        queryset = queryset.annotate(estimated_cost=Subquery(estimates.values('total')[:1]))
        return order_with_nulls(queryset, order_by)


class CustomerCurrentCostFilter(core_filters.BaseExternalFilter):
    def filter(self, request, queryset, view):

        order_by = get_ordering(request)
        if order_by not in ('current_cost', '-current_cost'):
            return queryset

        year, month = invoice_utils.parse_period(request.query_params)
        invoices = invoice_models.Invoice.objects.filter(year=year, month=month, customer=OuterRef('pk'))
        queryset = queryset.annotate(current_cost=Subquery(invoices.values('current_cost')[:1]))
        return order_with_nulls(queryset, order_by)


structure_filters.ExternalCustomerFilterBackend.register(CustomerEstimatedCostFilter())
structure_filters.ExternalCustomerFilterBackend.register(CustomerCurrentCostFilter())
