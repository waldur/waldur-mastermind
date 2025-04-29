from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import decorators, exceptions, response, status

from waldur_core.core import views as core_views

from . import backend, filters, models, serializers, tasks


class CheckExtensionMixin(core_views.ConstanceCheckExtensionMixin):
    extension_name = "FREEIPA"


class ProfileViewSet(CheckExtensionMixin, core_views.ActionsViewSet):
    queryset = models.Profile.objects.all()
    filterset_class = filters.ProfileFilter
    serializer_class = serializers.FreeipaProfileSerializer
    disabled_actions = ["destroy"]
    lookup_field = "uuid"

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_staff:
            return qs
        return qs.filter(user=self.request.user)

    @transaction.atomic()
    def perform_create(self, serializer):
        profile: models.Profile = serializer.save()
        try:
            backend.FreeIPABackend().create_profile(profile)
            tasks.schedule_sync()
        except freeipa_exceptions.DuplicateEntry:
            raise exceptions.ValidationError(
                {"username": _("Profile with such name already exists.")}
            )

    @extend_schema(
        description="Update SSH keys for profile.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_ssh_keys(self, request, uuid=None):
        profile: models.Profile = self.get_object()
        try:
            backend.FreeIPABackend().update_ssh_keys(profile)
        except freeipa_exceptions.NotFound:
            profile.delete()
            return response.Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return response.Response(status=status.HTTP_200_OK)
