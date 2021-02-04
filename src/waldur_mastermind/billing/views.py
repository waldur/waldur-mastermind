from rest_framework import exceptions, response, status, views

from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils


class TotalCustomerCostView(views.APIView):
    def get(self, request, format=None):
        if not self.request.user.is_staff and not request.user.is_support:
            raise exceptions.PermissionDenied()

        customers = structure_models.Customer.objects.all()
        customers = structure_filters.AccountingStartDateFilter().filter_queryset(
            request, customers, self
        )

        name = request.query_params.get('name', '')
        if name:
            customers = customers.filter(name__icontains=name)

        year, month = invoice_utils.parse_period(request.query_params)
        invoices = invoices_models.Invoice.objects.filter(customer__in=customers)
        invoices = invoices.filter(year=year, month=month)

        total = sum(invoice.total for invoice in invoices)
        price = sum(invoice.price for invoice in invoices)
        return response.Response(
            {'total': total, 'price': price}, status=status.HTTP_200_OK
        )
