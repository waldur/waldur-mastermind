import decimal

from django.db.models import F, Sum
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_core.structure.permissions import _get_project
from waldur_mastermind.billing.utils import get_current_expression
from waldur_mastermind.common.mixins import PRICE_DECIMAL_PLACES, PRICE_MAX_DIGITS
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.serializers import (
    PaymentProfileSerializer,
    get_payment_profiles,
)
from waldur_mastermind.policy import models as policy_models

from ..invoices import utils
from . import models

PriceEstimateDecimalField = serializers.DecimalField(
    max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES
)


class NestedPriceEstimateSerializer(serializers.HyperlinkedModelSerializer):
    total = serializers.SerializerMethodField()
    current = serializers.SerializerMethodField()
    tax = serializers.SerializerMethodField()
    tax_current = serializers.SerializerMethodField()

    def _parse_period(self):
        request = self.context["request"]

        try:
            year = int(request.query_params.get("year", ""))
            month = int(request.query_params.get("month", ""))

            if not utils.check_past_date(year, month):
                raise ValueError()

        except ValueError:
            year = month = None

        return year, month

    def _get_current_period(self):
        return utils.get_current_year(), utils.get_current_month()

    @extend_schema_field(PriceEstimateDecimalField)
    def get_total(self, obj) -> str:
        year, month = self._parse_period()
        value = obj.get_total(year=year, month=month) if (year and month) else obj.total
        return (
            f"{decimal.Decimal(str(value or 0)).quantize(_PRICE_ESTIMATE_QUANTIZER):f}"
        )

    @extend_schema_field(PriceEstimateDecimalField)
    def get_current(self, obj) -> str:
        year, month = self._parse_period()
        if not year and not month:
            year, month = self._get_current_period()
        value = obj.get_total(
            year=year, month=month, current=(year, month) == self._get_current_period()
        )
        return (
            f"{decimal.Decimal(str(value or 0)).quantize(_PRICE_ESTIMATE_QUANTIZER):f}"
        )

    @extend_schema_field(PriceEstimateDecimalField)
    def get_tax(self, obj) -> str:
        year, month = self._parse_period()
        if not year or not month:
            year, month = self._get_current_period()
        value = obj.get_tax(year=year, month=month)
        return (
            f"{decimal.Decimal(str(value or 0)).quantize(_PRICE_ESTIMATE_QUANTIZER):f}"
        )

    @extend_schema_field(PriceEstimateDecimalField)
    def get_tax_current(self, obj) -> str:
        year, month = self._parse_period()
        if not year and not month:
            year, month = self._get_current_period()
        value = obj.get_tax(
            year=year, month=month, current=(year, month) == self._get_current_period()
        )
        return (
            f"{decimal.Decimal(str(value or 0)).quantize(_PRICE_ESTIMATE_QUANTIZER):f}"
        )

    class Meta:
        model = models.PriceEstimate
        fields = ("total", "current", "tax", "tax_current")


_EMPTY_ESTIMATE = {
    "total": 0.0,
    "current": 0.0,
    "tax": 0.0,
    "tax_current": 0.0,
}

_PRICE_ESTIMATE_QUANTIZER = decimal.Decimal(10) ** -PRICE_DECIMAL_PLACES


def _to_price_estimate_strings(result: dict) -> dict:
    """Convert price estimate dict values to strings matching NestedPriceEstimateSerializer output."""
    return {
        k: f"{decimal.Decimal(str(v)).quantize(_PRICE_ESTIMATE_QUANTIZER):f}"
        for k, v in result.items()
    }


def _parse_period_from_request(request):
    """Parse year/month from request query params."""
    try:
        year = int(request.query_params.get("year", ""))
        month = int(request.query_params.get("month", ""))
        if not utils.check_past_date(year, month):
            return None, None
        return year, month
    except ValueError:
        return None, None


def _bulk_compute_project_estimates(project_ids, year, month):
    """Compute billing price estimates for multiple projects in a single query.

    Returns a dict mapping project_id -> {total, current, tax, tax_current}.
    Uses 1 GROUP BY query instead of 4*N individual queries.
    """
    query_year = year or utils.get_current_year()
    query_month = month or utils.get_current_month()

    qs = invoices_models.InvoiceItem.objects.filter(
        invoice__year=query_year,
        invoice__month=query_month,
        project_id__in=project_ids,
    ).select_related("invoice")

    current_qty = get_current_expression()

    aggregates = qs.values("project_id").annotate(
        total_val=Sum(F("unit_price") * F("quantity")),
        current_val=Sum(F("unit_price") * current_qty),
        tax_val=Sum(F("unit_price") * F("quantity") * F("invoice__tax_percent") / 100),
        tax_current_val=Sum(
            F("unit_price") * current_qty * F("invoice__tax_percent") / 100
        ),
    )

    cache = {}
    for row in aggregates:
        pid = row["project_id"]
        cache[pid] = _to_price_estimate_strings(
            {
                "total": quantize_price(decimal.Decimal(str(row["total_val"] or 0))),
                "current": quantize_price(
                    decimal.Decimal(str(row["current_val"] or 0))
                ),
                "tax": quantize_price(decimal.Decimal(str(row["tax_val"] or 0))),
                "tax_current": quantize_price(
                    decimal.Decimal(str(row["tax_current_val"] or 0))
                ),
            }
        )

    # Fill in projects with no invoice items
    for pid in project_ids:
        if pid not in cache:
            cache[pid] = _to_price_estimate_strings(dict(_EMPTY_ESTIMATE))

    return cache


@extend_schema_field(NestedPriceEstimateSerializer)
def get_price_estimate(serializer, scope):
    # For cases when we want to get project estimates under project cost policies
    if isinstance(scope, policy_models.ProjectEstimatedCostPolicy):
        scope = _get_project(scope)

    request = serializer.context.get("request")

    # Check if bulk optimization is available
    bulk_estimates = serializer.context.get("bulk_data", {}).get("billing_estimates")
    cached = None
    has_cache = False
    if bulk_estimates and scope.id in bulk_estimates:
        cached = bulk_estimates[scope.id]
        has_cache = True
    elif (
        request
        and hasattr(request, "_price_estimates_cache")
        and scope.id in request._price_estimates_cache
    ):
        cached = request._price_estimates_cache[scope.id]
        has_cache = True

    if has_cache:
        # Cache may contain pre-computed dicts (from project bulk),
        # PriceEstimate objects (from customer view bulk loading),
        # or lists (aggregated from multiple project-level estimates for restricted visibility)
        if isinstance(cached, dict):
            return cached
        if isinstance(cached, list):
            result = {"total": 0.0, "current": 0.0, "tax": 0.0, "tax_current": 0.0}
            for proj_estimate in cached:
                data = NestedPriceEstimateSerializer(
                    instance=proj_estimate, context=serializer.context
                ).data
                result["total"] += float(data["total"])
                result["current"] += float(data["current"])
                result["tax"] += float(data["tax"])
                result["tax_current"] += float(data["tax_current"])
            return _to_price_estimate_strings(result)
        if cached is not None:
            return _to_price_estimate_strings(
                NestedPriceEstimateSerializer(
                    instance=cached, context=serializer.context
                ).data
            )
        return _to_price_estimate_strings(dict(_EMPTY_ESTIMATE))

    # For list serialization of Projects, compute all estimates in bulk on first access
    if (
        request
        and isinstance(serializer.parent, serializers.ListSerializer)
        and isinstance(scope, structure_models.Project)
        and not hasattr(request, "_price_estimates_cache")
    ):
        all_scopes = serializer.parent.instance
        if all_scopes:
            scope_ids = [obj.id for obj in all_scopes]
            year, month = _parse_period_from_request(request)
            request._price_estimates_cache = _bulk_compute_project_estimates(
                scope_ids, year, month
            )
            if scope.id in request._price_estimates_cache:
                return request._price_estimates_cache[scope.id]

    # For restricted visibility on detail view, aggregate project-level estimates
    if (
        request
        and hasattr(request, "_user_has_full_visibility")
        and not request._user_has_full_visibility
        and hasattr(scope, "projects")
        and scope.id not in request._direct_customer_ids
    ):
        from django.contrib.contenttypes.models import ContentType

        project_ct = ContentType.objects.get_for_model(structure_models.Project)
        visible_project_ids = list(
            structure_models.Project.available_objects.filter(
                customer=scope, id__in=request._user_project_ids
            ).values_list("id", flat=True)
        )
        if not visible_project_ids:
            return _to_price_estimate_strings(dict(_EMPTY_ESTIMATE))

        project_estimates = models.PriceEstimate.objects.filter(
            content_type=project_ct, object_id__in=visible_project_ids
        )
        result = {"total": 0.0, "current": 0.0, "tax": 0.0, "tax_current": 0.0}
        for proj_estimate in project_estimates:
            data = NestedPriceEstimateSerializer(
                instance=proj_estimate, context=serializer.context
            ).data
            result["total"] += float(data["total"])
            result["current"] += float(data["current"])
            result["tax"] += float(data["tax"])
            result["tax_current"] += float(data["tax_current"])
        return _to_price_estimate_strings(result)

    # Fallback to original query behavior for single-object views
    try:
        estimate = models.PriceEstimate.objects.get(scope=scope)
    except models.PriceEstimate.DoesNotExist:
        return _to_price_estimate_strings(dict(_EMPTY_ESTIMATE))
    else:
        serializer_instance = NestedPriceEstimateSerializer(
            instance=estimate, context=serializer.context
        )
        return _to_price_estimate_strings(serializer_instance.data)


def add_price_estimate(sender, fields, **kwargs):
    """Add a billing price estimate field to the serializer."""
    fields["billing_price_estimate"] = serializers.SerializerMethodField()
    setattr(sender, "get_billing_price_estimate", get_price_estimate)

    # Also optimize eager loading for CustomerSerializer
    if sender.__name__ == "CustomerSerializer":
        _optimize_customer_serializer_eager_load(sender)


def _optimize_customer_serializer_eager_load(sender):
    """Optimize eager loading for CustomerSerializer to prefetch price estimates and invoice data."""
    # Check if we already have an optimized eager_load method
    # The flag is on the underlying function if it's a staticmethod
    eager_load_func = getattr(sender.eager_load, "__func__", sender.eager_load)
    if hasattr(eager_load_func, "_billing_optimized"):
        return

    # If credit optimization is already applied, we need to combine them
    if hasattr(eager_load_func, "_credit_optimized"):
        # Get the true original method that was stored by the credit optimization
        original_eager_load = getattr(
            eager_load_func, "_original_eager_load", sender.eager_load
        )

        def combined_eager_load(queryset, request=None):
            # Call the true original method first
            queryset = original_eager_load(queryset, request)

            # Add credit optimizations
            if request:
                fields = request.query_params.getlist("field")
                if "customer_credit" in fields:
                    queryset = queryset.select_related("customercredit")

            # Add billing optimizations
            if request:
                fields = request.query_params.getlist("field")
                if "billing_price_estimate" in fields:
                    queryset._billing_optimization_enabled = True

            return queryset

        # Mark as optimized for both and store original
        combined_eager_load._billing_optimized = True
        combined_eager_load._credit_optimized = True
        combined_eager_load._original_eager_load = original_eager_load

        # Replace the eager_load method
        sender.eager_load = staticmethod(combined_eager_load)
        return

    # Store the original eager_load method
    original_eager_load = sender.eager_load

    def optimized_eager_load(queryset, request=None):
        # Call the original eager_load first
        queryset = original_eager_load(queryset, request)

        # Add optimizations for billing_price_estimate if requested
        if request:
            fields = request.query_params.getlist("field")

            # Optimize billing_price_estimate field by bulk loading estimates
            if "billing_price_estimate" in fields:
                # Store a flag to indicate we should optimize price estimate queries
                # We'll do the actual optimization by bulk-loading in the serializer method
                queryset._billing_optimization_enabled = True

        return queryset

    # Mark as optimized to avoid double optimization and store original
    optimized_eager_load._billing_optimized = True
    optimized_eager_load._original_eager_load = original_eager_load

    # Replace the eager_load method
    sender.eager_load = staticmethod(optimized_eager_load)


class FinancialReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = structure_models.Customer
        fields = (
            "name",
            "uuid",
            "abbreviation",
            "created",
            "accounting_start_date",
            "registration_code",
            "agreement_number",
            "payment_profiles",
            "billing_price_estimate",
        )

    payment_profiles = serializers.SerializerMethodField()
    billing_price_estimate = serializers.SerializerMethodField()

    @extend_schema_field(NestedPriceEstimateSerializer)
    def get_billing_price_estimate(self, customer):
        request = self.context["request"]
        provider_uuid = request.query_params.get("provider_uuid")
        if provider_uuid:
            return utils.get_billing_price_estimate_for_provider(
                customer, provider_uuid
            )
        else:
            return get_price_estimate(self, customer)

    @extend_schema_field(PaymentProfileSerializer(many=True))
    def get_payment_profiles(self, customer):
        return get_payment_profiles(self, customer)


class TotalCustomerCostSerializer(serializers.Serializer):
    total = serializers.FloatField(read_only=True)
    price = serializers.FloatField(read_only=True)
