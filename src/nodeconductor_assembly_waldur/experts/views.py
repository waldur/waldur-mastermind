from __future__ import unicode_literals

from django.conf import settings
from rest_framework import decorators, permissions, status, response, viewsets
from django_filters.rest_framework import DjangoFilterBackend

from nodeconductor.core import views as core_views
from nodeconductor.structure import filters as structure_filters
from nodeconductor.structure import models as structure_models

from . import serializers, models, filters


class ExpertProviderViewSet(viewsets.ModelViewSet):
    queryset = models.ExpertProvider.objects.all()
    serializer_class = serializers.ExpertProviderSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExpertProviderFilter


class ExpertRequestViewSet(core_views.ActionsViewSet):
    queryset = models.ExpertRequest.objects.all()
    serializer_class = serializers.ExpertRequestSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExpertRequestFilter
    disabled_actions = ['destroy']

    def get_queryset(self):
        qs = super(ExpertRequestViewSet, self).get_queryset()
        if self.request.user.is_staff:
            return qs

        is_expert_manager = models.ExpertProvider.objects.filter(
            customer__permissions__is_active=True,
            customer__permissions__user=self.request.user,
            customer__permissions__role=structure_models.CustomerRole.OWNER,
        ).exists()

        if is_expert_manager:
            return qs

        return qs.filter(
            project__customer__permissions__is_active=True,
            project__customer__permissions__user=self.request.user,
            project__customer__permissions__role=structure_models.CustomerRole.OWNER,
        )

    @decorators.list_route()
    def configured(self, request):
        return response.Response(settings.WALDUR_SUPPORT['OFFERINGS'], status=status.HTTP_200_OK)
