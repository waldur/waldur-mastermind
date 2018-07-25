from __future__ import unicode_literals

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch
from rest_framework import viewsets, exceptions

from waldur_core.core import views as core_views
from waldur_core.cost_tracking import models, serializers, filters
from waldur_core.structure import SupportedServices
from waldur_core.structure import models as structure_models, permissions as structure_permissions
from waldur_core.structure.filters import ScopeTypeFilterBackend


class PriceEstimateViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.PriceEstimate.objects.all()
    serializer_class = serializers.PriceEstimateSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.PriceEstimateDateFilterBackend,
        filters.PriceEstimateCustomerFilterBackend,
        filters.PriceEstimateScopeFilterBackend,
        ScopeTypeFilterBackend,
    )

    def get_serializer_context(self):
        context = super(PriceEstimateViewSet, self).get_serializer_context()
        try:
            depth = int(self.request.query_params['depth'])
        except (TypeError, KeyError):
            pass  # use default depth if it is not defined or defined wrongly.
        else:
            context['depth'] = min(depth, 10)  # DRF restriction - serializer depth cannot be > 10
        return context

    def get_queryset(self):
        return models.PriceEstimate.objects.filtered_for_user(self.request.user).order_by(
            '-year', '-month')

    def list(self, request, *args, **kwargs):
        """
        To get a list of price estimates, run **GET** against */api/price-estimates/* as authenticated user.
        You can filter price estimates by scope type, scope URL, customer UUID.

        `scope_type` is generic type of object for which price estimate is calculated.
        Currently there are following types: customer, project, service, serviceprojectlink, resource.

        `date` parameter accepts list of dates. `start` and `end` parameters together specify date range.
        Each valid date should in format YYYY.MM

        You can specify GET parameter ?depth to show price estimate children. For example with ?depth=2 customer
        price estimate will shows its children - project and service and grandchildren - serviceprojectlink.
        """
        return super(PriceEstimateViewSet, self).list(request, *args, **kwargs)


class PriceListItemViewSet(viewsets.ModelViewSet):
    queryset = models.PriceListItem.objects.all()
    serializer_class = serializers.PriceListItemSerializer
    lookup_field = 'uuid'
    filter_backends = (filters.PriceListItemServiceFilterBackend,)

    def get_queryset(self):
        return models.PriceListItem.objects.filtered_for_user(self.request.user)

    def _user_can_modify_price_list_item(self, item):
        if self.request.user.is_staff:
            return True

        customer = structure_permissions._get_customer(item)
        return customer.has_user(self.request.user, structure_models.CustomerRole.OWNER)

    def list(self, request, *args, **kwargs):
        """
        To get a list of price list items, run **GET** against */api/price-list-items/* as an authenticated user.
        """
        return super(PriceListItemViewSet, self).list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """
        Run **POST** request against */api/price-list-items/* to create new price list item.
        Customer owner and staff can create price items.

        Example of request:

        .. code-block:: http

            POST /api/price-list-items/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "units": "per month",
                "value": 100,
                "service": "http://example.com/api/oracle/d4060812ca5d4de390e0d7a5062d99f6/",
                "default_price_list_item": "http://example.com/api/default-price-list-items/349d11e28f634f48866089e41c6f71f1/"
            }
        """
        return super(PriceListItemViewSet, self).create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        """
        Run **PATCH** request against */api/price-list-items/<uuid>/* to update price list item.
        Only item_type, key value and units can be updated.
        Only customer owner and staff can update price items.
        """
        return super(PriceListItemViewSet, self).update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Run **DELETE** request against */api/price-list-items/<uuid>/* to delete price list item.
        Only customer owner and staff can delete price items.
        """
        return super(PriceListItemViewSet, self).destroy(request, *args, **kwargs)

    def initial(self, request, *args, **kwargs):
        if self.action in ('partial_update', 'update', 'destroy'):
            price_list_item = self.get_object()
            if not self._user_can_modify_price_list_item(price_list_item.service):
                raise exceptions.PermissionDenied()

        return super(PriceListItemViewSet, self).initial(request, *args, **kwargs)

    def perform_create(self, serializer):
        if not self._user_can_modify_price_list_item(serializer.validated_data['service']):
            raise exceptions.PermissionDenied()

        super(PriceListItemViewSet, self).perform_create(serializer)


class DefaultPriceListItemViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.DefaultPriceListItem.objects.all()
    lookup_field = 'uuid'
    filter_class = filters.DefaultPriceListItemFilter
    serializer_class = serializers.DefaultPriceListItemSerializer

    def list(self, request, *args, **kwargs):
        """
        To get a list of default price list items, run **GET** against */api/default-price-list-items/*
        as authenticated user.

        Price lists can be filtered by:
         - ?key=<string>
         - ?item_type=<string> has to be from list of available item_types
           (available options: 'flavor', 'storage', 'license-os', 'license-application', 'network', 'support')
         - ?resource_type=<string> resource type, for example: 'OpenStack.Instance, 'Oracle.Database')
        """
        return super(DefaultPriceListItemViewSet, self).list(request, *args, **kwargs)


class MergedPriceListItemViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = 'uuid'
    filter_class = filters.DefaultPriceListItemFilter
    serializer_class = serializers.MergedPriceListItemSerializer

    def list(self, request, *args, **kwargs):
        """
        To get a list of price list items, run **GET** against */api/merged-price-list-items/*
        as authenticated user.

        If service is not specified default price list items are displayed.
        Otherwise service specific price list items are displayed.
        In this case rendered object contains {"is_manually_input": true}

        In order to specify service pass query parameters:
        - service_type (Azure, OpenStack etc.)
        - service_uuid

        Example URL: http://example.com/api/merged-price-list-items/?service_type=Azure&service_uuid=cb658b491f3644a092dd223e894319be
        """
        return super(MergedPriceListItemViewSet, self).list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = models.DefaultPriceListItem.objects.all()
        service = self._find_service()
        if service:
            # Filter items by resource type
            resources = SupportedServices.get_related_models(service)['resources']
            content_types = ContentType.objects.get_for_models(*resources).values()
            queryset = queryset.filter(resource_content_type__in=content_types)

            # Attach service-specific items
            price_list_items = models.PriceListItem.objects.filter(service=service)
            prefetch = Prefetch('pricelistitem_set', queryset=price_list_items, to_attr='service_item')
            queryset = queryset.prefetch_related(prefetch)
        return queryset

    def _find_service(self):
        service_type = self.request.query_params.get('service_type')
        service_uuid = self.request.query_params.get('service_uuid')
        if not service_type or not service_uuid:
            return
        rows = SupportedServices.get_service_models()
        if service_type not in rows:
            return
        service_class = rows.get(service_type)['service']
        try:
            return service_class.objects.get(uuid=service_uuid)
        except ObjectDoesNotExist:
            return None
