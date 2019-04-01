from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions, response, status, views

from waldur_core.core import views as core_views
from waldur_core.structure import models as structure_models
from waldur_core.structure import filters as structure_filters
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils

from . import filters, models, serializers


class PriceEstimateViewSet(core_views.ActionsViewSet):
    disabled_actions = ['create', 'destroy']
    queryset = models.PriceEstimate.objects.all()
    serializer_class = serializers.PriceEstimateSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.PriceEstimateScopeFilterBackend,
    )

    def get_queryset(self):
        return models.PriceEstimate.objects.filtered_for_user(self.request.user)

    def is_owner_or_staff(request, view, obj=None):
        if not obj:
            return False

        if request.user.is_staff:
            return True

        if isinstance(obj.scope, structure_models.Customer):
            raise exceptions.PermissionDenied(
                _('Only staff is allowed to modify policy for the customer.')
            )

        elif isinstance(obj.scope, structure_models.Project):
            customer = obj.scope.customer
            if not customer.has_user(request.user, structure_models.CustomerRole.OWNER):
                raise exceptions.PermissionDenied(
                    _('Only staff and customer owner is allowed to modify policy for the project.')
                )

    update_permissions = [is_owner_or_staff]


class TotalCustomerCostView(views.APIView):
    def get(self, request, format=None):
        if not self.request.user.is_staff and not request.user.is_support:
            raise exceptions.PermissionDenied()

        customers = structure_models.Customer.objects.all()
        customers = structure_filters.AccountingStartDateFilter().filter_queryset(request, customers, self)

        name = request.query_params.get('name', '')
        if name:
            customers = customers.filter(name__icontains=name)

        year, month = invoice_utils.parse_period(request.query_params)
        invoices = invoices_models.Invoice.objects.filter(customer__in=customers)
        invoices = invoices.filter(year=year, month=month)

        total = sum(invoice.total for invoice in invoices)
        price = sum(invoice.price for invoice in invoices)
        return response.Response({'total': total, 'price': price}, status=status.HTTP_200_OK)
