from __future__ import unicode_literals

from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.core import views as core_views
from waldur_core.structure import permissions as structure_permissions

from . import serializers, models, filters


class BaseMarketplaceView(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    update_permissions = \
        partial_update_permissions = \
        destroy_permissions = \
        [structure_permissions.is_owner]


class ServiceProviderViewSet(BaseMarketplaceView):
    queryset = models.ServiceProvider.objects.all()
    serializer_class = serializers.ServiceProviderSerializer
    filter_class = filters.ServiceProviderFilter


class CategoryViewSet(core_views.ActionsViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.CategorySerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)

    create_permissions = \
        update_permissions = \
        partial_update_permissions = \
        destroy_permissions = \
        [structure_permissions.is_staff]


class OfferingViewSet(BaseMarketplaceView):
    queryset = models.Offering.objects.all()
    serializer_class = serializers.OfferingSerializer
    filter_class = filters.OfferingFilter


class ScreenshotViewSet(BaseMarketplaceView):
    queryset = models.Screenshots.objects.all()
    serializer_class = serializers.ScreenshotSerializer
    filter_class = filters.ScreenshotFilter
