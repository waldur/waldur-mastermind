from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import permissions as rf_permissions
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_core.core import log as core_log
from waldur_core.core import serializers as core_serializers
from waldur_core.core import validators as core_validators
from waldur_core.core.views import ProtectedViewSet, ReadOnlyActionsViewSet
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import has_user
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.models import Customer
from waldur_core.users import filters, models, serializers, tasks
from waldur_core.users.utils import can_manage_invitation_with, parse_invitation_token

User = get_user_model()


class InvitationViewSet(ProtectedViewSet):
    queryset = models.Invitation.objects.all().order_by("-created")
    serializer_class = serializers.InvitationSerializer
    filter_backends = (
        DjangoFilterBackend,
        filters.InvitationScopeFilterBackend,
        filters.InvitationFilterBackend,
    )
    filterset_class = filters.InvitationFilter
    lookup_field = "uuid"

    def perform_create(self, serializer):
        scope = serializer.validated_data["scope"]
        if not can_manage_invitation_with(self.request, scope):
            raise PermissionDenied()

        invitation = serializer.save()
        sender = self.request.user.full_name or self.request.user.username
        if (
            settings.WALDUR_CORE["ONLY_STAFF_CAN_INVITE_USERS"]
            and not self.request.user.is_staff
        ):
            invitation.state = models.Invitation.State.REQUESTED
            invitation.save()
            transaction.on_commit(
                lambda: tasks.send_invitation_requested.delay(
                    invitation.uuid.hex, sender
                )
            )
        else:
            transaction.on_commit(
                lambda: tasks.process_invitation.delay(invitation.uuid.hex, sender)
            )

    @action(detail=False, methods=["post"], permission_classes=[])
    def approve(self, request):
        """
        For user's convenience invitation approval is performed without authentication.
        User UUID and invitation UUID is encoded into cryptographically signed token.
        """
        token = request.data.get("token")
        if not token:
            raise ValidationError("token is required parameter")

        user, invitation = parse_invitation_token(token)
        invitation.approve(user)

        sender = ""
        if invitation.created_by:
            sender = invitation.created_by.full_name or invitation.created_by.username
        transaction.on_commit(
            lambda: tasks.process_invitation.delay(invitation.uuid.hex, sender)
        )

        return Response(
            {"detail": _("Invitation has been approved.")}, status=status.HTTP_200_OK
        )

    @action(detail=False, methods=["post"], permission_classes=[])
    def reject(self, request):
        """
        For user's convenience invitation reject action is performed without authentication.
        User UUID and invitation UUID is encoded into cryptographically signed token.
        """
        token = request.data.get("token")
        if not token:
            raise ValidationError("token is required parameter")
        user, invitation = parse_invitation_token(token)
        invitation.reject()

        sender = ""
        if invitation.created_by:
            sender = invitation.created_by.full_name or invitation.created_by.username
        transaction.on_commit(
            lambda: tasks.send_invitation_rejected.delay(invitation.uuid.hex, sender)
        )

        return Response(
            {"detail": _("Invitation has been rejected.")}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"])
    def send(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if not can_manage_invitation_with(self.request, invitation.scope):
            raise PermissionDenied()
        elif invitation.state not in (
            models.Invitation.State.PENDING,
            models.Invitation.State.EXPIRED,
        ):
            raise ValidationError(
                _("Only pending and expired invitations can be resent.")
            )

        if invitation.state == models.Invitation.State.EXPIRED:
            invitation.state = models.Invitation.State.PENDING
            invitation.created = timezone.now()
            invitation.save()

        sender = request.user.full_name or request.user.username
        tasks.send_invitation_created.delay(invitation.uuid.hex, sender)
        return Response(
            {"detail": _("Invitation sending has been successfully scheduled.")},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if not can_manage_invitation_with(self.request, invitation.scope):
            raise PermissionDenied()
        elif invitation.state != models.Invitation.State.PENDING:
            raise ValidationError(_("Only pending invitation can be canceled."))

        invitation.cancel()
        return Response(
            {"detail": _("Invitation has been successfully canceled.")},
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True, methods=["post"], filter_backends=[filters.PendingInvitationFilter]
    )
    def accept(self, request, uuid=None):
        """Accept invitation for current user.

        To replace user's email with email from invitation - add parameter
        'replace_email' to request POST body.
        """
        invitation: models.Invitation = self.get_object()

        if has_user(invitation.scope, request.user, invitation.role):
            raise ValidationError(_("User has already the same role in this scope."))

        replace_email = False
        if invitation.email != request.user.email:
            if settings.WALDUR_CORE["ENABLE_STRICT_CHECK_ACCEPTING_INVITATION"]:
                raise ValidationError(
                    _("User’s email and email of the invitation are not equal.")
                )

            replace_email = bool(request.data.get("replace_email"))

        if settings.WALDUR_CORE["INVITATION_DISABLE_MULTIPLE_ROLES"]:
            if UserRole.objects.filter(user=request.user, is_active=True).exists():
                raise ValidationError(_("User already has role within another scope."))

        invitation.accept(request.user)
        if replace_email:
            old_mail = request.user.email
            request.user.email = invitation.email
            request.user.save(update_fields=["email"])
            core_log.event_logger.user.info(
                f"User email has been changed via invitation from {old_mail} to {invitation.email}",
                event_type="user_profile_changed",
                event_context={"affected_user": request.user},
            )

        return Response(
            {"detail": _("Invitation has been successfully accepted.")},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], filter_backends=[], permission_classes=[])
    def check(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if invitation.state != models.Invitation.State.PENDING:
            return Response(status=status.HTTP_404_NOT_FOUND)
        elif invitation.civil_number:
            return Response(
                {"email": invitation.email, "civil_number_required": True},
                status=status.HTTP_200_OK,
            )
        else:
            return Response({"email": invitation.email}, status=status.HTTP_200_OK)

    @action(detail=True, filter_backends=[filters.PendingInvitationFilter])
    def details(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()
        serializer = serializers.PendingInvitationDetailsSerializer(instance=invitation)
        return Response(serializer.data)


class GroupInvitationViewSet(ProtectedViewSet):
    queryset = models.GroupInvitation.objects.all().order_by("-created")
    serializer_class = serializers.GroupInvitationSerializer
    filter_backends = (
        filters.InvitationScopeFilterBackend,
        filters.GroupInvitationFilterBackend,
        DjangoFilterBackend,
    )
    permission_classes = (rf_permissions.IsAuthenticated,)
    filterset_class = filters.GroupInvitationFilter
    lookup_field = "uuid"

    @action(detail=True, methods=["get"], filter_backends=[])
    def projects(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()

        if not isinstance(invitation.scope, Customer):
            return Response(status=status.HTTP_404_NOT_FOUND)

        projects = structure_serializers.NestedProjectSerializer(
            instance=invitation.customer.projects.all(),
            read_only=True,
            context={"request": request},
            many=True,
        )
        return Response(projects.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], filter_backends=[])
    def cancel(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()

        if not can_manage_invitation_with(request, invitation.scope):
            raise PermissionDenied()
        elif not invitation.is_active:
            raise ValidationError(_("Only pending invitation can be canceled."))

        invitation.cancel()
        return Response(
            {"detail": _("Invitation has been successfully canceled.")},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], filter_backends=[])
    def request(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()

        if not invitation.is_active:
            raise ValidationError(_("Only pending invitation can be requested."))

        if (
            models.PermissionRequest.objects.filter(
                invitation=invitation, created_by=request.user
            )
            .exclude(state=models.PermissionRequest.States.REJECTED)
            .exists()
        ):
            raise ValidationError(_("Request has been created already."))

        permission_request = models.PermissionRequest.objects.create(
            invitation=invitation,
            created_by=request.user,
        )

        permission_request.submit()
        return Response(
            {"uuid": permission_request.uuid.hex},
            status=status.HTTP_200_OK,
        )

    def perform_create(self, serializer):
        scope = serializer.validated_data["scope"]
        if not can_manage_invitation_with(self.request, scope):
            raise PermissionDenied()

        serializer.save()


class PermissionRequestViewSet(ReadOnlyActionsViewSet):
    queryset = models.PermissionRequest.objects.all().order_by("-created")
    serializer_class = serializers.PermissionRequestSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        filters.PermissionRequestScopeFilterBackend,
        DjangoFilterBackend,
    )
    filterset_class = filters.PermissionRequestFilter
    lookup_field = "uuid"

    def perform_action(self, request, uuid, action_name):
        permission_request: models.PermissionRequest = self.get_object()

        if not can_manage_invitation_with(
            self.request, permission_request.invitation.scope
        ):
            raise PermissionDenied()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")

        getattr(permission_request, action_name)(self.request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        return self.perform_action(request, uuid, "approve")

    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        return self.perform_action(request, uuid, "reject")

    approve_serializer_class = reject_serializer_class = (
        core_serializers.ReviewCommentSerializer
    )
    approve_validators = reject_validators = [
        core_validators.StateValidator(models.PermissionRequest.States.PENDING)
    ]
