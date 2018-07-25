import django_filters
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.structure import models as structure_models

from .models import ResourceSlaStateTransition
from .utils import get_period


class ResourceScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return structure_models.ResourceMixin.get_all_models()

    def get_field_name(self):
        return 'scope'


class ResourceStateFilter(django_filters.FilterSet):
    class Meta:
        model = ResourceSlaStateTransition
        fields = ('timestamp', 'period', 'state')


class SlaFilter(BaseFilterBackend):
    """
    SLA filter
    ^^^^^^^^^^

    Allows to filter or sort resources by actual_sla
    Default period is current year and month.

    Example query parameters for filtering list of OpenStack instances:

    .. code-block:: http

        /api/openstack-instances/?actual_sla=90&period=2016-02

    Example query parameters for sorting list of OpenStack instances:

    .. code-block:: http

        /api/openstack-instances/?o=actual_sla&period=2016-02
    """

    def filter_queryset(self, request, queryset, view):
        period = get_period(request)

        if 'actual_sla' in request.query_params:
            value = request.query_params.get('actual_sla')
            return queryset.filter(sla_items__value=value, sla_items__period=period)

        elif request.query_params.get('o') == 'actual_sla':
            return queryset.filter(sla_items__period=period).order_by('sla_items__value')

        else:
            return queryset


class MonitoringItemFilter(BaseFilterBackend):
    """
    Monitoring filter
    ^^^^^^^^^^^^^^^^^

    Filter and order resources by monitoring item.
    For example, given query dictionary

    .. code-block:: http

        {
            'monitoring__installation_state': True
        }

    it produces following query

    .. code-block:: http

        {
            'monitoring_item__name': 'installation_state',
            'monitoring_item__value': True
        }

    Example query parameters for sorting list of OpenStack instances:

    .. code-block:: http

        /api/openstack-instances/?o=monitoring__installation_state
    """

    def filter_queryset(self, request, queryset, view):
        for key in request.query_params.keys():
            item_name = self._get_item_name(key)
            if item_name:
                value = request.query_params.get(key)
                queryset = queryset.filter(monitoring_items__name=item_name,
                                           monitoring_items__value=value)

        order_by = request.query_params.get('o')
        item_name = self._get_item_name(order_by)
        if item_name:
            queryset = queryset.filter(monitoring_items__name=item_name)\
                               .order_by('monitoring_items__value')

        return queryset

    def _get_item_name(self, key):
        if key and key.startswith('monitoring__'):
            _, item_name = key.split('__', 1)
            return item_name
