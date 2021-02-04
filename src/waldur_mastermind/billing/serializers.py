from rest_framework import serializers

from ..invoices import utils
from . import models


class NestedPriceEstimateSerializer(serializers.HyperlinkedModelSerializer):
    total = serializers.SerializerMethodField()
    current = serializers.SerializerMethodField()
    tax = serializers.SerializerMethodField()
    tax_current = serializers.SerializerMethodField()

    def _parse_period(self):
        request = self.context['request']

        try:
            year = int(request.query_params.get('year', ''))
            month = int(request.query_params.get('month', ''))

            if not utils.check_past_date(year, month):
                raise ValueError()

        except ValueError:
            year = month = None

        return year, month

    def _get_current_period(self):
        return utils.get_current_year(), utils.get_current_month()

    def get_total(self, obj):
        year, month = self._parse_period()

        if year and month:
            return obj.get_total(year=year, month=month)

        return obj.total

    def get_current(self, obj):
        year, month = self._parse_period()
        if not year and not month:
            year, month = self._get_current_period()
        return obj.get_total(year=year, month=month, current=True)

    def get_tax(self, obj):
        year, month = self._parse_period()
        if not year or not month:
            year, month = self._get_current_period()

        return obj.get_tax(year=year, month=month)

    def get_tax_current(self, obj):
        year, month = self._get_current_period()
        return obj.get_tax(year=year, month=month, current=True)

    class Meta:
        model = models.PriceEstimate
        fields = ('total', 'current', 'tax', 'tax_current')


def get_price_estimate(serializer, scope):
    try:
        estimate = models.PriceEstimate.objects.get(scope=scope)
    except models.PriceEstimate.DoesNotExist:
        return {
            'total': 0.0,
            'current': 0.0,
            'tax': 0.0,
            'tax_current': 0.0,
        }
    else:
        serializer = NestedPriceEstimateSerializer(
            instance=estimate, context=serializer.context
        )
        return serializer.data


def add_price_estimate(sender, fields, **kwargs):
    fields['billing_price_estimate'] = serializers.SerializerMethodField()
    setattr(sender, 'get_billing_price_estimate', get_price_estimate)
