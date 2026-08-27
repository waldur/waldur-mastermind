from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.passkeys import policy
from waldur_core.passkeys.models import PasskeyCredential
from waldur_core.passkeys.serializers import (
    PasskeyCredentialSerializer,
    PasskeyCredentialUpdateSerializer,
    PasskeyRevokeSerializer,
)


class PasskeyCredentialViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Manage one's own passkeys.

    Enrolling a credential is a ceremony rather than a create, so there is no
    ``POST`` here; those endpoints land with the login paths. Revocation is a
    soft delete via the ``revoke`` action, so the audit record survives.
    """

    queryset = PasskeyCredential.objects.all()
    serializer_class = PasskeyCredentialSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PasskeyCredential.objects.none()
        return PasskeyCredential.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return PasskeyCredentialUpdateSerializer
        if self.action == "revoke":
            return PasskeyRevokeSerializer
        return PasskeyCredentialSerializer

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not policy.is_enabled():
            raise exceptions.NotFound(
                _("Passkey authentication is not enabled on this deployment.")
            )

    @extend_schema(
        summary="Revoke a passkey",
        request=PasskeyRevokeSerializer,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, uuid=None):
        credential = self.get_object()
        if not credential.is_active:
            raise exceptions.ValidationError(_("This passkey is already revoked."))

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")

        credential.revoke(revoked_by=request.user, reason=reason)

        # The name is user-controlled and emit() runs .format() over the
        # template, so it is passed as a context value rather than inlined.
        event_logger.emit(
            "Passkey {passkey_name} has been revoked for user "
            "{affected_user_username}.",
            event_type=EventType.PASSKEY_REVOKED,
            event_context={
                "affected_user": credential.user,
                "passkey_name": credential.name,
            },
            scopes=[credential.user],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_update(self, serializer):
        credential = serializer.save()
        event_logger.emit(
            "Passkey {passkey_name} has been renamed for user "
            "{affected_user_username}.",
            event_type=EventType.PASSKEY_RENAMED,
            event_context={
                "affected_user": credential.user,
                "passkey_name": credential.name,
            },
            scopes=[credential.user],
        )
