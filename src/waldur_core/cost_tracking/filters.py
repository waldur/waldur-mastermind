from __future__ import unicode_literals

import uuid

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
import django_filters
from django_filters.constants import EMPTY_VALUES
from rest_framework import filters

from waldur_core.core import filters as core_filters
from waldur_core.cost_tracking import models, serializers
from waldur_core.structure import models as structure_models, SupportedServices


class PriceEstimateScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return models.PriceEstimate.get_estimated_models()

    def get_field_name(self):
        return 'scope'


class PriceEstimateDateFilterBackend(filters.BaseFilterBackend):

    def filter_queryset(self, request, queryset, view):
        if 'date' in request.query_params:
            date_serializer = serializers.PriceEstimateDateFilterSerializer(
                data={'date_list': request.query_params.getlist('date')})
            date_serializer.is_valid(raise_exception=True)
            query = Q()
            for year, month in date_serializer.validated_data['date_list']:
                query |= Q(year=year, month=month)
            queryset = queryset.filter(query)

        # Filter by date range
        date_range_serializer = serializers.PriceEstimateDateRangeFilterSerializer(data=request.query_params)
        date_range_serializer.is_valid(raise_exception=True)
        if 'start' in date_range_serializer.validated_data:
            year, month = date_range_serializer.validated_data['start']
            queryset = queryset.filter(Q(year__gt=year) | Q(year=year, month__gte=month))
        if 'end' in date_range_serializer.validated_data:
            year, month = date_range_serializer.validated_data['end']
            queryset = queryset.filter(Q(year__lt=year) | Q(year=year, month__lte=month))

        return queryset


class PriceEstimateCustomerFilterBackend(filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if 'customer' not in request.query_params:
            return queryset

        customer_uuid = request.query_params['customer']
        try:
            uuid.UUID(customer_uuid)
        except ValueError:
            return queryset.none()

        try:
            customer = structure_models.Customer.objects.get(uuid=customer_uuid)
        except structure_models.Customer.DoesNotExist:
            return queryset.none()

        ids = []
        for estimate in models.PriceEstimate.objects.filter(scope=customer):
            ids.append(estimate.pk)
            for child in estimate.collect_children():
                ids.append(child.pk)
        return queryset.filter(pk__in=ids)


class PriceListItemServiceFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return structure_models.Service.get_all_models()

    def get_field_name(self):
        return 'service'


class ResourceTypeFilter(django_filters.CharFilter):

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs
        resource_models = SupportedServices.get_resource_models()
        try:
            model = resource_models[value]
            ct = ContentType.objects.get_for_model(model)
            return super(ResourceTypeFilter, self).filter(qs, ct)
        except (ContentType.DoesNotExist, KeyError):
            return qs.none()


class DefaultPriceListItemFilter(django_filters.FilterSet):
    resource_content_type = core_filters.ContentTypeFilter()
    resource_type = ResourceTypeFilter(name='resource_content_type')

    class Meta:
        model = models.DefaultPriceListItem
        fields = [
            'key',
            'item_type',
            'resource_content_type',
            'resource_type',
        ]
