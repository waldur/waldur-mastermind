import decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_core.structure.permissions import _get_project
from waldur_mastermind.common.mixins import PRICE_DECIMAL_PLACES, PRICE_MAX_DIGITS
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
    def get_total(self, obj) -> decimal.Decimal:
        year, month = self._parse_period()

        if year and month:
            return obj.get_total(year=year, month=month)

        return obj.total

    @extend_schema_field(PriceEstimateDecimalField)
    def get_current(self, obj) -> decimal.Decimal:
        year, month = self._parse_period()
        if not year and not month:
            year, month = self._get_current_period()
        return obj.get_total(
            year=year, month=month, current=(year, month) == self._get_current_period()
        )

    @extend_schema_field(PriceEstimateDecimalField)
    def get_tax(self, obj) -> decimal.Decimal:
        year, month = self._parse_period()
        if not year or not month:
            year, month = self._get_current_period()

        return obj.get_tax(year=year, month=month)

    @extend_schema_field(PriceEstimateDecimalField)
    def get_tax_current(self, obj) -> decimal.Decimal:
        year, month = self._parse_period()
        if not year and not month:
            year, month = self._get_current_period()
        return obj.get_tax(
            year=year, month=month, current=(year, month) == self._get_current_period()
        )

    class Meta:
        model = models.PriceEstimate
        fields = ("total", "current", "tax", "tax_current")


@extend_schema_field(NestedPriceEstimateSerializer)
def get_price_estimate(serializer, scope):
    # For cases when we want to get project estimates under project cost policies
    if isinstance(scope, policy_models.ProjectEstimatedCostPolicy):
        scope = _get_project(scope)
    try:
        estimate = models.PriceEstimate.objects.get(scope=scope)
    except models.PriceEstimate.DoesNotExist:
        return {
            "total": 0.0,
            "current": 0.0,
            "tax": 0.0,
            "tax_current": 0.0,
        }
    else:
        serializer = NestedPriceEstimateSerializer(
            instance=estimate, context=serializer.context
        )
        return serializer.data


def add_price_estimate(sender, fields, **kwargs):
    fields["billing_price_estimate"] = serializers.SerializerMethodField()
    setattr(sender, "get_billing_price_estimate", get_price_estimate)


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
