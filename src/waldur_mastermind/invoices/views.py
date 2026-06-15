import datetime
import decimal
import uuid
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import F, Q, QuerySet, Sum
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.serializers import DetailSerializer, StatusSerializer
from waldur_core.core.utils import is_uuid_like
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.permissions import IsStaffOrSupportUser
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices.models import InvoiceItem

from . import filters, models, serializers, tasks, utils


class InvoiceViewSet(core_views.HistoryViewSetMixin, core_views.ReadOnlyActionsViewSet):
    queryset = models.Invoice.objects.order_by("-year", "-month")
    serializer_class = serializers.InvoiceSerializer
    lookup_field = "uuid"
    filter_backends = (
        structure_filters.GenericRoleFilter,
        structure_filters.CustomerAccountingStartDateFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.InvoiceFilter

    def _is_invoice_created(invoice):
        if invoice.state != models.Invoice.States.CREATED:
            raise exceptions.ValidationError(
                _("Notification only for the created invoice can be sent.")
            )

    @extend_schema(
        summary="Get invoice items",
        description="Retrieve a list of invoice items for the specified invoice.",
        responses=serializers.InvoiceItemSerializer,
        parameters=[
            OpenApiParameter("query", str, OpenApiParameter.QUERY),
            OpenApiParameter(
                "provider_uuid",
                uuid.UUID,
                OpenApiParameter.QUERY,
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
            OpenApiParameter(
                "project_uuid",
                uuid.UUID,
                OpenApiParameter.QUERY,
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter(
                "offering_uuid",
                uuid.UUID,
                OpenApiParameter.QUERY,
                extensions={
                    "x-waldur-operation-id": "marketplace_public_offerings_list"
                },
            ),
            OpenApiParameter(
                "conceal_compensation_items",
                bool,
                OpenApiParameter.QUERY,
                description="Conceal compensation items",
            ),
            OpenApiParameter(
                "o",
                {
                    "type": "string",
                    "enum": [
                        "project_name",
                        "-project_name",
                        "resource_name",
                        "-resource_name",
                        "provider_name",
                        "-provider_name",
                        "name",
                        "-name",
                    ],
                },
                OpenApiParameter.QUERY,
                description="Order results by field",
            ),
        ],
    )
    @action(detail=True)
    def items(self, request, uuid=None):
        invoice: models.Invoice = self.get_object()
        queryset = invoice.items.all()
        query = request.query_params.get("query", "").strip()
        provider_uuid = request.query_params.get("provider_uuid", None)
        project_uuid = request.query_params.get("project_uuid", None)
        offering_uuid = request.query_params.get("offering_uuid", None)
        conceal_compensation_items = (
            request.query_params.get("conceal_compensation_items") == "true"
        )
        ordering = request.query_params.get("o", None)

        items = utils.filter_invoice_items(
            queryset,
            query,
            provider_uuid,
            project_uuid,
            offering_uuid,
            conceal_compensation_items,
            ordering,
        )
        serializer = serializers.InvoiceItemSerializer(
            items, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @extend_schema(
        responses={status.HTTP_200_OK: DetailSerializer},
        request=None,
        summary="Send invoice notification",
        description="Schedule sending of a notification for the specified invoice.",
    )
    @action(detail=True, methods=["post"])
    def send_notification(self, request, uuid=None):
        invoice: models.Invoice = self.get_object()
        tasks.send_invoice_notification.delay(invoice.uuid.hex)

        return Response(
            {
                "detail": _(
                    "Invoice notification sending has been successfully scheduled."
                )
            },
            status=status.HTTP_200_OK,
        )

    send_notification_permissions = [structure_permissions.is_staff]
    send_notification_validators = [_is_invoice_created]

    @extend_schema(
        responses={status.HTTP_200_OK: None},
        description="Mark invoice as paid and optionally create payment record with proof of payment.",
        request=serializers.PaidSerializer,
    )
    @transaction.atomic
    @action(detail=True, methods=["post"])
    def paid(self, request, uuid=None):
        invoice: models.Invoice = self.get_object()

        if request.data:
            serializer = serializers.PaidSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            try:
                profile = models.PaymentProfile.objects.get(
                    is_active=True, organization=invoice.customer
                )
            except models.PaymentProfile.DoesNotExist:
                raise exceptions.ValidationError(
                    _("The active profile for this customer does not exist.")
                )

            payment = models.Payment.objects.create(
                date_of_payment=serializer.validated_data["date"],
                sum=invoice.total_current,
                profile=profile,
                invoice=invoice,
            )

            proof = serializer.validated_data.get("proof")

            if proof:
                payment.proof = proof

            payment.save()

            event_logger.emit(
                'Payment for invoice ({month}/{year}) has been added."',
                event_type=EventType.PAYMENT_CREATED,
                event_context={
                    "month": invoice.month,
                    "year": invoice.year,
                    "customer": invoice.customer,
                    "invoice": invoice,
                },
                scopes=[invoice, invoice.customer],
            )

        invoice.state = models.Invoice.States.PAID
        invoice.save(update_fields=["state"])
        return Response(status=status.HTTP_200_OK)

    paid_permissions = [structure_permissions.is_staff]
    paid_validators = [core_validators.StateValidator(models.Invoice.States.CREATED)]

    @extend_schema(
        description="Spendings grouped by offerings and filtered by provider.",
        responses=serializers.InvoiceStatsSerializer,
        parameters=[
            OpenApiParameter(
                "provider_uuid",
                uuid.UUID,
                OpenApiParameter.QUERY,
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            )
        ],
    )
    @action(detail=True)
    def stats(self, request, uuid=None):
        invoice: models.Invoice = self.get_object()
        offerings = {}
        requested_provider_uuid = request.query_params.get("provider_uuid")
        if requested_provider_uuid:
            items = invoice.items.filter(
                resource__offering__customer__uuid=requested_provider_uuid
            )
        else:
            items = invoice.items.all()

        for item in items:
            if not item.resource:
                continue

            resource = item.resource
            offering = resource.offering
            customer = offering.customer

            service_category_title = offering.category.title
            service_provider_name = customer.name
            service_provider_uuid = customer.serviceprovider.uuid.hex

            if offering.uuid.hex not in offerings.keys():
                offerings[offering.uuid.hex] = {
                    "offering_name": offering.name,
                    "aggregated_price": item.price,
                    "aggregated_tax": item.tax,
                    "aggregated_total": item.total,
                    "service_category_title": service_category_title,
                    "service_provider_name": service_provider_name,
                    "service_provider_uuid": service_provider_uuid,
                }
            else:
                offerings[offering.uuid.hex]["aggregated_price"] += item.price
                offerings[offering.uuid.hex]["aggregated_tax"] += item.tax
                offerings[offering.uuid.hex]["aggregated_total"] += item.total

        queryset = [dict(uuid=key, **details) for (key, details) in offerings.items()]

        for item in queryset:
            item["aggregated_price"] = quantize_price(
                decimal.Decimal(item["aggregated_price"])
            )
            item["aggregated_tax"] = quantize_price(
                decimal.Decimal(item["aggregated_tax"])
            )
            item["aggregated_total"] = quantize_price(
                decimal.Decimal(item["aggregated_total"])
            )

        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(page)

    @extend_schema(
        description="Analyze invoice trends over time by comparing monthly totals for major customers versus others over the past year.",
        responses=serializers.InvoiceGrowthSerializer,
        parameters=[
            OpenApiParameter("accounting_is_running", bool, OpenApiParameter.QUERY),
            OpenApiParameter("customers_count", int, OpenApiParameter.QUERY),
            OpenApiParameter("accounting_mode", str, OpenApiParameter.QUERY),
        ],
    )
    @action(detail=False)
    def growth(self, request):
        queryset = structure_models.Customer.objects.all()
        customers = filter_queryset_for_user(queryset, request.user)
        customers = structure_filters.AccountingStartDateFilter().filter_queryset(
            request, customers, self
        )

        customers_count = 4
        if "customers_count" in request.query_params:
            try:
                customers_count = int(request.query_params["customers_count"])
            except ValueError:
                raise exceptions.ValidationError("customers_count is not a number")

        if customers_count > 20:
            raise exceptions.ValidationError(
                "customers_count should not be greater than 20"
            )

        is_accounting_mode = request.query_params.get("accounting_mode") == "accounting"

        today = datetime.date.today()
        year_ago = today - relativedelta(months=12)

        majors = list(
            models.Invoice.objects.filter(customer__in=customers, created__gte=year_ago)
            .values("customer_id")
            .annotate(total_value=Sum("total_cost"))
            .order_by("-total_value")
            .values_list("customer_id", flat=True)[:customers_count]
        )

        minors = customers.exclude(id__in=majors)

        periods = []
        for i in range(13):
            month = year_ago + relativedelta(months=i)
            periods.append(f"{month.year}-{month.month}")

        field = is_accounting_mode and "total_price" or "total_cost"
        total_values = {
            f"{row['year']}-{row['month']}": row["total_value"]
            for row in models.Invoice.objects.filter(customer__in=customers)
            .values("year", "month")
            .annotate(total_value=Sum(field))
        }
        other_values = {
            f"{row['year']}-{row['month']}": row["total_value"]
            for row in models.Invoice.objects.filter(customer__in=minors)
            .values("year", "month")
            .annotate(total_value=Sum(field))
        }
        customer_periods = {
            f"{row.customer_id}-{row.year}-{row.month}": getattr(row, field)
            for row in models.Invoice.objects.filter(customer__in=majors)
        }
        customer_periods = [
            {
                "name": customer.name,
                "periods": [
                    customer_periods.get(f"{customer.id}-{period}", 0)
                    for period in periods
                ],
            }
            for customer in structure_models.Customer.objects.filter(id__in=majors)
        ]

        result = {
            "periods": periods,
            "total_periods": [total_values.get(period, 0) for period in periods],
            "other_periods": [other_values.get(period, 0) for period in periods],
            "customer_periods": customer_periods,
        }

        return Response(result, status=status.HTTP_200_OK)

    @extend_schema(
        description="Set backend ID for invoice.",
        request=serializers.BackendIdSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    set_backend_id_permissions = [structure_permissions.is_staff]
    set_backend_id_serializer_class = serializers.BackendIdSerializer

    @extend_schema(
        description="Set payment URL for invoice.",
        request=serializers.PaymentURLSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def set_payment_url(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    set_payment_url_permissions = [structure_permissions.is_staff]
    set_payment_url_serializer_class = serializers.PaymentURLSerializer

    @extend_schema(
        description="Set reference number for invoice.",
        request=serializers.ReferenceNumberSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def set_reference_number(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    set_reference_number_permissions = [structure_permissions.is_staff]
    set_reference_number_serializer_class = serializers.ReferenceNumberSerializer

    @extend_schema(
        summary="Import usage data",
        description="Import component usage items as JSON data for multiple customers. "
        "Creates invoice items for the specified billing period. "
        "Items are deduplicated by name, customer, and billing period to prevent duplicates.",
        request=serializers.ImportUsageSerializer,
        responses=serializers.ImportUsageResponseSerializer,
    )
    @transaction.atomic
    @action(detail=False, methods=["post"])
    def import_usage(self, request):
        serializer = serializers.ImportUsageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        year = serializer.validated_data["year"]
        month = serializer.validated_data["month"]
        items = serializer.validated_data["items"]

        created = 0
        skipped = 0
        errors = []

        # Pre-fetch all customers for name lookup (single query)
        customers = list(structure_models.Customer.objects.all())
        all_customers = {c.name.lower(): c for c in customers}
        all_customers_by_uuid = {c.uuid.hex: c for c in customers}

        for item in items:
            customer = None
            customer_identifier = None

            # Try to find customer by UUID first, then by name
            if item.get("customer_uuid"):
                customer_uuid = str(item["customer_uuid"]).replace("-", "")
                customer = all_customers_by_uuid.get(customer_uuid)
                customer_identifier = customer_uuid
            elif item.get("customer_name"):
                customer_name = item["customer_name"].strip()
                customer = all_customers.get(customer_name.lower())
                customer_identifier = customer_name

            if not customer:
                errors.append(
                    {
                        "customer_name": customer_identifier or "Unknown",
                        "reason": "Customer not found",
                    }
                )
                continue

            # Skip zero amounts
            unit_price = item["unit_price"]
            if unit_price == Decimal("0"):
                skipped += 1
                continue

            # Get or create invoice for customer + year/month
            invoice, _ = models.Invoice.objects.get_or_create(
                customer=customer,
                year=year,
                month=month,
            )

            # Validate invoice state - only allow adding items to mutable invoices
            if invoice.state not in models.Invoice.States.MUTABLE_STATES:
                errors.append(
                    {
                        "customer_name": customer.name,
                        "reason": f"Invoice is in '{invoice.state}' state, items can only be added to mutable invoices",
                    }
                )
                continue

            # Build details dict for additional metadata
            details = {}
            if item.get("service_provider_name"):
                details["service_provider_name"] = item["service_provider_name"]
            if item.get("offering_name"):
                details["offering_name"] = item["offering_name"]
            if item.get("plan_name"):
                details["plan_name"] = item["plan_name"]

            # Check for duplicate items (idempotency)
            existing_item = models.InvoiceItem.objects.filter(
                invoice=invoice,
                name=item["name"],
                article_code=item.get("article_code", ""),
            ).first()

            if existing_item:
                skipped += 1
                continue

            # Create invoice item
            invoice_item = models.InvoiceItem.objects.create(
                invoice=invoice,
                name=item["name"],
                unit_price=unit_price,
                quantity=1,
                unit=models.InvoiceItem.Units.QUANTITY,
                article_code=item.get("article_code", ""),
                details=details,
            )

            event_logger.emit(
                "Invoice item {invoice_item_name} has been imported for {customer_name} ({month}/{year}).",
                event_type=EventType.INVOICE_ITEM_CREATED,
                event_context={
                    "invoice_item": invoice_item,
                    "invoice_item_name": invoice_item.name,
                    "customer": customer,
                    "month": month,
                    "year": year,
                },
                scopes=[invoice, customer],
            )

            created += 1

        return Response(
            {"created": created, "skipped": skipped, "errors": errors},
            status=status.HTTP_200_OK,
        )

    import_usage_permissions = [structure_permissions.is_staff]
    import_usage_serializer_class = serializers.ImportUsageSerializer


class InvoiceItemViewSet(core_views.ActionsViewSet):
    disabled_actions = ["create"]
    queryset = models.InvoiceItem.objects.select_related(
        "invoice",
        "invoice__customer",
        "resource",
        "resource__offering",
        "plan_component",
        "plan_component__component",
    ).order_by("start")
    serializer_class = serializers.InvoiceItemDetailSerializer
    lookup_field = "uuid"
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.InvoiceItemFilter

    @extend_schema(
        description="Calculate total price for filtered invoice items.",
        responses=serializers.InvoiceItemTotalPriceSerializer,
        filters=True,
    )
    @action(detail=False, methods=["get"], filterset_class=filters.InvoiceItemFilter)
    def total_price(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        total_price = queryset.aggregate(
            total_price=Sum(F("unit_price") * F("quantity"))
        )["total_price"]
        total_price = total_price or 0
        serializer = serializers.InvoiceItemTotalPriceSerializer(
            {"total_price": total_price}
        )
        return Response(serializer.data)

    @extend_schema(
        responses={status.HTTP_201_CREATED: serializers.InvoiceItemUUIDSerializer},
        request=serializers.InvoiceItemCompensationSerializer,
        description="Create compensation invoice item for selected invoice item.",
    )
    @transaction.atomic
    @action(detail=True, methods=["post"])
    def create_compensation(self, request, **kwargs):
        invoice_item: models.InvoiceItem = self.get_object()

        serializer = self.get_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering_component_name = serializer.validated_data["offering_component_name"]

        if invoice_item.unit_price < 0:
            return Response(
                "Can not create compensation for invoice item with negative unit price.",
                status=status.HTTP_400_BAD_REQUEST,
            )
        year, month = utils.get_current_year(), utils.get_current_month()
        invoice, _ = models.Invoice.objects.get_or_create(
            customer=invoice_item.invoice.customer,
            month=month,
            year=year,
        )

        # Fill new invoice item details
        if not invoice_item.details:
            invoice_item.details = {}
        invoice_item.details["original_invoice_item_uuid"] = invoice_item.uuid.hex
        invoice_item.details["offering_component_name"] = offering_component_name

        # Save new invoice item to database
        invoice_item.invoice = invoice
        invoice_item.pk = None
        invoice_item.uuid = uuid.uuid4()
        invoice_item.unit_price *= -1
        invoice_item.save()

        return Response(
            {"invoice_item_uuid": invoice_item.uuid.hex},
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.InvoiceItemUUIDSerializer},
        description="Move invoice item from one invoice to another one.",
        request=serializers.InvoiceItemMigrateToSerializer,
    )
    @action(detail=True, methods=["post"])
    def migrate_to(self, request, **kwargs):
        invoice_item: models.InvoiceItem = self.get_object()
        serializer = self.get_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = serializer.validated_data["invoice"]

        if invoice.customer != invoice_item.invoice.customer:
            raise exceptions.ValidationError(
                _(
                    "Moving of items is possible only between invoices of same the customer."
                )
            )

        if invoice == invoice_item.invoice:
            raise exceptions.ValidationError(
                _("Invoice item is already in the target invoice.")
            )

        old_invoice = invoice_item.invoice
        invoice_item.invoice = invoice
        invoice_item.save()

        event_logger.emit(
            f"Invoice item has been migrated from {old_invoice} to {invoice}",
            event_type=EventType.INVOICE_ITEM_UPDATED,
            event_context={
                "invoice_item": invoice_item,
            },
            scopes=[invoice_item.invoice, invoice_item.invoice.customer],
        )

        return Response(
            {"invoice_item_uuid": invoice_item.uuid.hex},
            status=status.HTTP_200_OK,
        )

    def _get_current_month_items(self, project_uuid, user):
        now = datetime.date.today()
        items = filter_queryset_for_user(
            InvoiceItem.objects.filter(
                project_uuid=project_uuid,
                invoice__year=now.year,
                invoice__month=now.month,
                unit_price__gt=0,
            ),
            user,
        ).values("name", "unit_price", "unit", "quantity", "measured_unit")
        return [
            {
                "name": item["name"],
                "unit_price": float(item["unit_price"]),
                "unit": item["unit"],
                "quantity": float(item["quantity"]),
                "measured_unit": item["measured_unit"],
                "price": float(item["unit_price"] * item["quantity"]),
            }
            for item in items
        ]

    def _get_costs_data(self, paginated_invoices, current_month_items=None):
        now = datetime.date.today()
        result_list = []
        for invoice in paginated_invoices:
            data = {
                "price": "{:.2f}".format(invoice["price"]),
                "compensation": "{:.2f}".format(invoice["compensation"]),
                "incurred": "{:.2f}".format(invoice["incurred"]),
                "year": invoice["invoice__year"],
                "month": invoice["invoice__month"],
            }
            if (
                current_month_items is not None
                and invoice["invoice__year"] == now.year
                and invoice["invoice__month"] == now.month
            ):
                data["items"] = current_month_items
            result_list.append(data)
        return result_list

    @extend_schema(
        description="Get costs breakdown for a project by year and month.",
        parameters=[
            OpenApiParameter(
                "project_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the project for which statistics should be calculated.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            )
        ],
        responses=serializers.InvoiceCostSerializer(many=True),
    )
    @action(detail=False, methods=["get"], filter_backends=[])
    def costs(self, request, *args, **kwargs):
        project_uuid = request.GET.get("project_uuid", "")
        if not is_uuid_like(project_uuid):
            raise exceptions.ValidationError("project_uuid is not a valid UUID.")
        current_month_items = self._get_current_month_items(project_uuid, request.user)
        invoices = filter_queryset_for_user(
            InvoiceItem.objects.filter(project_uuid=project_uuid),
            request.user,
        )
        invoices = (
            invoices.values("invoice__year", "invoice__month")
            .annotate(
                price=Sum(F("unit_price") * F("quantity")),
                compensation=Sum(
                    F("unit_price") * F("quantity"),
                    filter=Q(unit_price__lt=0),
                    default=0,
                ),
                incurred=Sum(
                    F("unit_price") * F("quantity"),
                    filter=Q(unit_price__gt=0),
                    default=0,
                ),
            )
            .values(
                "invoice__year", "invoice__month", "price", "compensation", "incurred"
            )
            .order_by("-invoice__year", "-invoice__month")
        )
        page = self.paginate_queryset(invoices)
        if page is not None:
            data = self._get_costs_data(page, current_month_items)
            return self.get_paginated_response(data)
        data = self._get_costs_data(invoices, current_month_items)
        return Response(data)

    def _get_costs_for_periods_data(
        self, invoices: QuerySet, period: int, month_start: datetime.date
    ) -> dict[str, str]:
        PERIOD_LENGTHS = {
            models.PeriodMixin.Periods.MONTH_1: 1,
            models.PeriodMixin.Periods.MONTH_3: 3,
            models.PeriodMixin.Periods.MONTH_12: 12,
        }
        period_length = PERIOD_LENGTHS.get(period, 0)

        query = Q()

        for n in range(period_length):
            previous_month_date = month_start - relativedelta(months=n)
            query |= Q(
                invoice__month=previous_month_date.month,
                invoice__year=previous_month_date.year,
            )

        total_price: Decimal | None = (
            invoices.filter(query).aggregate(
                total_price=Sum(F("unit_price") * F("quantity"))
            )["total_price"]
            or 0
        )

        start_date = month_start - relativedelta(months=period_length)

        return {
            "total_price": f"{total_price:.2f}",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": month_start.strftime("%Y-%m-%d"),
        }

    def _get_costs_for_entity(self, invoices, period, month_start):
        if period == models.PeriodMixin.Periods.TOTAL:
            total_price = (
                invoices.aggregate(total_price=Sum(F("unit_price") * F("quantity")))[
                    "total_price"
                ]
                or 0
            )
            return {
                "total_price": f"{total_price:.2f}",
                "start_date": "N/A",
                "end_date": "N/A",
            }

        return self._get_costs_for_periods_data(invoices, period, month_start)

    @extend_schema(
        description="Get resource cost breakdown for a project over a specified period.",
        parameters=[
            OpenApiParameter(
                name="period",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Period for which statistics should be calculated (1, 3 or 12 months).",
            ),
            OpenApiParameter(
                name="project_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the project for which statistics should be calculated.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
        ],
        responses=serializers.CostsForPeriodSerializer,
    )
    @action(detail=False, methods=["get"], filter_backends=[])
    def project_costs_for_period(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.GET, context={"request": request})
        serializer.is_valid(raise_exception=True)
        project_uuid = serializer.validated_data["project_uuid"]
        period = serializer.validated_data["period"]
        month_start = datetime.date.today().replace(day=1)

        invoices = (
            InvoiceItem.objects.filter(project_uuid=project_uuid.hex)
            .values("invoice__year", "invoice__month")
            .annotate(price=Sum(F("unit_price") * F("quantity")))
            .distinct()
            .order_by("-invoice__year", "-invoice__month")
        )

        invoices = filter_queryset_for_user(invoices, request.user)

        data = self._get_costs_for_entity(invoices, period, month_start)
        return Response(data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="period",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Period for which statistics should be calculated.",
            ),
            OpenApiParameter(
                name="customer_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the project for which statistics should be calculated.",
            ),
        ],
        responses=serializers.CostsForPeriodSerializer,
    )
    @action(detail=False, methods=["get"], filter_backends=[])
    def customer_costs_for_period(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.GET, context={"request": request})
        serializer.is_valid(raise_exception=True)
        customer_uuid = serializer.validated_data["customer_uuid"]
        period = serializer.validated_data["period"]
        month_start = datetime.date.today().replace(day=1)

        invoices = (
            InvoiceItem.objects.filter(invoice__customer__uuid=customer_uuid.hex)
            .values("invoice__year", "invoice__month")
            .annotate(price=Sum(F("unit_price") * F("quantity")))
            .values("invoice__year", "invoice__month", "price")
            .order_by("-invoice__year", "-invoice__month")
        )

        invoices = filter_queryset_for_user(invoices, request.user)
        data = self._get_costs_for_entity(invoices, period, month_start)
        return Response(data)

    create_compensation_serializer_class = serializers.InvoiceItemCompensationSerializer

    update_serializer_class = serializers.InvoiceItemUpdateSerializer

    partial_update_serializer_class = serializers.InvoiceItemUpdateSerializer

    migrate_to_serializer_class = serializers.InvoiceItemMigrateToSerializer

    project_costs_for_period_serializer_class = (
        serializers.InvoiceItemProjectCostsForPeriodSerializer
    )

    customer_costs_for_period_serializer_class = (
        serializers.InvoiceItemCustomerCostsForPeriodSerializer
    )

    create_compensation_permissions = update_permissions = (
        partial_update_permissions
    ) = destroy_permissions = migrate_to_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={
            status.HTTP_200_OK: serializers.CustomerCreditConsumptionByMonthSerializer(
                many=True
            )
        }
    )
    @action(detail=True, methods=["get"])
    def consumptions(self, request, uuid=None):
        customer_credit = self.get_object()
        consumptions = (
            models.InvoiceItem.objects.filter(credit=customer_credit, unit_price__lt=0)
            .values("invoice__year", "invoice__month")
            .annotate(price=Sum("price"))
            .order_by("invoice__year", "invoice__month")
        )

        return Response(
            [
                {
                    "month": f"{item['invoice__year']}-{item['invoice__month']:02d}",
                    "price": item["price"],
                }
                for item in consumptions
            ]
        )


class PaymentProfileViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.PaymentProfileFilterBackend,
    )
    filterset_class = filters.PaymentProfileFilter
    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = enable_permissions = [structure_permissions.is_staff]
    queryset = models.PaymentProfile.objects.all().order_by("name")
    serializer_class = serializers.PaymentProfileSerializer

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer}, request=None)
    @action(detail=True, methods=["post"])
    def enable(self, request, uuid=None):
        profile: models.PaymentProfile = self.get_object()
        profile.is_active = True
        profile.save(update_fields=["is_active"])

        return Response(
            {"detail": _("Payment profile has been enabled.")},
            status=status.HTTP_200_OK,
        )


class PaymentViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.PaymentFilter
    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = link_to_invoice_permissions = unlink_from_invoice_permissions = [
        structure_permissions.is_staff
    ]
    queryset = models.Payment.objects.all().order_by("created")
    serializer_class = serializers.PaymentSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: DetailSerializer},
        description="Link a payment to an invoice. Payment can be linked to an invoice only if they belong to the same customer.",
    )
    @action(detail=True, methods=["post"])
    def link_to_invoice(self, request, uuid=None):
        payment: models.Payment = self.get_object()
        serializer = self.get_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice: models.Invoice = serializer.validated_data["invoice"]

        if invoice.customer != payment.profile.organization:
            raise exceptions.ValidationError(
                _("The passed invoice does not belong to the selected customer.")
            )

        payment.invoice = invoice
        payment.save(update_fields=["invoice"])

        event_logger.emit(
            "Payment for invoice ({month}/{year}) has been added.",
            event_type=EventType.PAYMENT_CREATED,
            event_context={
                "month": invoice.month,
                "year": invoice.year,
                "customer": invoice.customer,
                "invoice": invoice,
            },
            scopes=[invoice, invoice.customer],
        )

        return Response(
            {"detail": _("An invoice has been linked to payment.")},
            status=status.HTTP_200_OK,
        )

    def _link_to_invoice_exists(payment):
        if payment.invoice:
            raise exceptions.ValidationError(_("Link to an invoice exists."))

    link_to_invoice_validators = [_link_to_invoice_exists]
    link_to_invoice_serializer_class = serializers.LinkToInvoiceSerializer

    def _link_to_invoice_does_not_exist(payment):
        if not payment.invoice:
            raise exceptions.ValidationError(_("Link to an invoice does not exist."))

    @extend_schema(
        responses={status.HTTP_200_OK: DetailSerializer},
        request=None,
        description="Unlink a payment from an invoice. Remove connection between payment and existing linked invoice.",
    )
    @action(detail=True, methods=["post"])
    def unlink_from_invoice(self, request, uuid=None):
        payment: models.Payment = self.get_object()
        invoice = payment.invoice
        if not invoice:
            raise exceptions.ValidationError(_("Payment is not linked to any invoice."))
        payment.invoice = None
        payment.save(update_fields=["invoice"])

        event_logger.emit(
            "Payment for invoice ({month}/{year}) has been removed.",
            event_type=EventType.PAYMENT_REMOVED,
            event_context={
                "month": invoice.month,
                "year": invoice.year,
                "customer": invoice.customer,
                "invoice": invoice,
            },
            scopes=[invoice, invoice.customer],
        )

        return Response(
            {"detail": _("An invoice has been unlinked from payment.")},
            status=status.HTTP_200_OK,
        )

    unlink_from_invoice_validators = [_link_to_invoice_does_not_exist]

    def perform_create(self, serializer):
        super().perform_create(serializer)
        payment: models.Payment = serializer.instance
        event_logger.emit(
            "Payment for {customer_name} in the amount of {amount} has been added.",
            event_type=EventType.PAYMENT_ADDED,
            event_context={
                "amount": payment.sum,
                "customer": payment.profile.organization,
                "invoice": payment.invoice,
            },
            scopes=[payment.invoice, payment.profile.organization],
        )

    def perform_destroy(self, instance: models.Payment):
        customer = instance.profile.organization
        amount = instance.sum
        super().perform_destroy(instance)

        event_logger.emit(
            "Payment for {customer_name} in the amount of {amount} has been removed.",
            event_type=EventType.PAYMENT_REMOVED,
            event_context={
                "amount": amount,
                "customer": customer,
                "invoice": instance.invoice,
            },
            scopes=[customer, instance.invoice],
        )


@extend_schema(
    request=serializers.FinancialReportEmailSerializer,
    responses={status.HTTP_202_ACCEPTED: StatusSerializer},
)
@api_view(["POST"])
@permission_classes((IsStaffOrSupportUser,))
def send_financial_report_by_mail(request):
    serializer = serializers.FinancialReportEmailSerializer(data=request.data)
    if serializer.is_valid():
        year = serializer.data["year"]
        month = serializer.data["month"]
        emails = serializer.data["emails"]
        tasks.send_invoice_report.delay(year, month, emails, False)
        return Response(
            {"status": _("The dispatch of reports has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomerCreditViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.CustomerCreditFilter
    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]
    queryset = models.CustomerCredit.objects.all().order_by("created")
    serializer_class = serializers.CustomerCreditSerializer
    create_serializer_class = update_serializer_class = (
        partial_update_serializer_class
    ) = serializers.CreateCustomerCreditSerializer

    @extend_schema(responses={status.HTTP_200_OK: None}, request=None)
    @transaction.atomic
    @action(detail=True, methods=["post"])
    def apply_compensations(self, request, uuid=None):
        customer_credit: models.CustomerCredit = self.get_object()
        compensations.MonthlyCompensation(
            customer_credit.customer
        ).apply_compensations()

    @extend_schema(responses={status.HTTP_200_OK: None}, request=None)
    @transaction.atomic
    @action(detail=True, methods=["post"])
    def clear_compensations(self, request, uuid=None):
        customer_credit: models.CustomerCredit = self.get_object()
        compensations.MonthlyCompensation(
            customer_credit.customer
        ).clear_compensations()

    apply_compensations_permissions = clear_compensations_permissions = [
        structure_permissions.is_staff
    ]

    @extend_schema(
        description="Get credit consumption history grouped by month.",
        responses=serializers.CustomerCreditConsumptionSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def consumptions(self, request, uuid=None):
        customer_credit = self.get_object()
        items = models.InvoiceItem.objects.filter(
            credit=customer_credit, unit_price__lt=0
        ).order_by("invoice__year", "invoice__month")
        consumptions = {}

        for item in items:
            date = datetime.date(int(item.invoice.year), int(item.invoice.month), 1)
            if date in consumptions:
                consumptions[date] += item.price
            else:
                consumptions[date] = item.price

        serializer = self.get_serializer(
            [
                {"date": str(date), "price": price * -1}
                for date, price in consumptions.items()
            ],
            many=True,
        )
        return Response(serializer.data)

    consumptions_serializer_class = serializers.CustomerCreditConsumptionSerializer


class ProjectCreditViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    update_permissions = partial_update_permissions = destroy_permissions = [
        structure_permissions.is_owner
    ]
    queryset = models.ProjectCredit.objects.exclude(
        project__customer__customercredit__isnull=True
    ).order_by("created")
    serializer_class = serializers.ProjectCreditSerializer
    filterset_class = filters.ProjectCreditFilter
