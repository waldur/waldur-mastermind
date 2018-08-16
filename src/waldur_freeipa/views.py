from __future__ import unicode_literals

from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import decorators, exceptions, response, status

from waldur_core.core import views as core_views
from . import backend, filters, models, serializers, tasks


class CheckExtensionMixin(core_views.CheckExtensionMixin):
    extension_name = 'WALDUR_FREEIPA'


class ProfileViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Profile.objects.all()
    filter_class = filters.ProfileFilter
    serializer_class = serializers.ProfileSerializer
    disabled_actions = ['destroy']
    lookup_field = 'uuid'

    def get_queryset(self):
        qs = super(ProfileViewSet, self).get_queryset()
        if self.request.user.is_staff:
            return qs
        return qs.filter(user=self.request.user)

    @transaction.atomic()
    def perform_create(self, serializer):
        profile = serializer.save()
        try:
            backend.FreeIPABackend().create_profile(profile)
            tasks.schedule_sync()
        except freeipa_exceptions.DuplicateEntry:
            raise exceptions.ValidationError({
                'username': _('Profile with such name already exists.')
            })

    @decorators.detail_route(methods=['post'])
    def update_ssh_keys(self, request, uuid=None):
        profile = self.get_object()
        try:
            backend.FreeIPABackend().update_ssh_keys(profile)
        except freeipa_exceptions.NotFound:
            profile.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return response.Response(status=status.HTTP_200_OK)

    @decorators.detail_route(methods=['post'])
    @transaction.atomic()
    def disable(self, request, uuid=None):
        profile = self.get_object()
        if not profile.is_active:
            raise exceptions.ValidationError({
                'detail': _('Profile is already disabled.')
            })
        try:
            backend.FreeIPABackend().disable_profile(profile)
        except freeipa_exceptions.NotFound:
            profile.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        except freeipa_exceptions.AlreadyInactive:
            profile.is_active = False
            profile.save(update_fields=['is_active'])
            return response.Response(status=status.HTTP_200_OK)
        else:
            profile.is_active = False
            profile.save(update_fields=['is_active'])
            return response.Response(status=status.HTTP_200_OK)

    @decorators.detail_route(methods=['post'])
    @transaction.atomic()
    def enable(self, request, uuid=None):
        profile = self.get_object()
        if profile.is_active:
            raise exceptions.ValidationError({
                'detail': _('Profile is already enabled.')
            })
        try:
            backend.FreeIPABackend().enable_profile(profile)
        except freeipa_exceptions.NotFound:
            profile.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        except freeipa_exceptions.AlreadyActive:
            profile.is_active = True
            profile.save(update_fields=['is_active'])
            return response.Response(status=status.HTTP_200_OK)
        else:
            profile.is_active = True
            profile.save(update_fields=['is_active'])
            return response.Response(status=status.HTTP_200_OK)
