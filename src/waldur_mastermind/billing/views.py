import uuid

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions, generics, response, status
from rest_framework import filters as rf_filters

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils

from . import filters, serializers


class TotalCustomerCostView(generics.GenericAPIView):
    filter_backends = []
    serializer_class = serializers.TotalCustomerCostSerializer
    pagination_class = None

    @extend_schema(
        parameters=[
            OpenApiParameter("name", str, OpenApiParameter.QUERY),
            OpenApiParameter("customer_uuid", uuid.UUID, OpenApiParameter.QUERY),
            OpenApiParameter("accounting_is_running", bool, OpenApiParameter.QUERY),
            OpenApiParameter("year", int, OpenApiParameter.QUERY),
            OpenApiParameter("month", int, OpenApiParameter.QUERY),
        ]
    )
    def get(self, request, format=None):
        if not self.request.user.is_staff and not request.user.is_support:
            raise exceptions.PermissionDenied()

        customers = structure_models.Customer.objects.all()
        customers = structure_filters.AccountingStartDateFilter().filter_queryset(
            request, customers, self
        )

        name = request.query_params.get("name", "")
        if name:
            customers = customers.filter(name__icontains=name)

        customer_uuid = request.query_params.get("customer_uuid", "")
        if customer_uuid:
            customers = customers.filter(uuid=customer_uuid)

        year, month = invoice_utils.parse_period(request.query_params)
        invoices = invoices_models.Invoice.objects.filter(customer__in=customers)
        invoices = invoices.filter(year=year, month=month)

        total = sum(invoice.total for invoice in invoices)
        price = sum(invoice.price for invoice in invoices)
        return response.Response(
            {"total": total, "price": price}, status=status.HTTP_200_OK
        )


class FinancialReportView(core_views.ReadOnlyActionsViewSet):
    queryset = structure_models.Customer.objects.all()
    serializer_class = serializers.FinancialReportSerializer
    lookup_field = "uuid"
    filter_backends = (
        filters.CustomerTotalCostFilter,
        filters.CustomerEstimatedCostFilter,
        structure_filters.CustomerAccountingStartDateFilter,
        structure_filters.GenericRoleFilter,
        rf_filters.OrderingFilter,
        DjangoFilterBackend,
    )
    filterset_class = structure_filters.CustomerFilter
    ordering_fields = (
        "abbreviation",
        "accounting_start_date",
        "agreement_number",
        "created",
        "name",
        "native_name",
        "registration_code",
    )

    def get_queryset(self):
        queryset = super().get_queryset()

        customer_uuid = self.request.query_params.get("customer_uuid", "")

        if customer_uuid:
            queryset = queryset.filter(uuid=customer_uuid)

        return queryset
