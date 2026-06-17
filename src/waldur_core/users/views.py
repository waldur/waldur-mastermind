from typing import cast

from constance import config
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import permissions as rf_permissions
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_core.core import serializers as core_serializers
from waldur_core.core import validators as core_validators
from waldur_core.core.enums import ReviewStates
from waldur_core.core.views import (
    ActionsViewSet,
    ReadOnlyActionsViewSet,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import has_user, validate_user_restrictions
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.models import Customer, Project
from waldur_core.users import filters, models, serializers, tasks
from waldur_core.users.enums import InvitationState
from waldur_core.users.utils import (
    can_manage_invitation_with,
    can_manage_permission_request,
    get_invitation_duplicates,
    parse_invitation_token,
)


@extend_schema_view(
    list=extend_schema(
        summary="List user invitations",
        description="Retrieve a list of user invitations visible to the current user.",
    ),
    retrieve=extend_schema(
        summary="Retrieve user invitation",
        description="Retrieve details of a specific user invitation.",
    ),
    create=extend_schema(
        summary="Create user invitation",
        description="Create a new user invitation to grant a role in a specific scope (e.g., organization or project).",
    ),
    update=extend_schema(
        summary="Update user invitation",
        description="Update an existing user invitation. Only pending invitations can be edited. Allows changing email and role within the same scope.",
    ),
    partial_update=extend_schema(
        summary="Partially update user invitation",
        description="Partially update an existing user invitation. Only pending invitations can be edited. Allows changing email and role within the same scope.",
    ),
    destroy=extend_schema(
        summary="Delete user invitation",
        description="Delete a user invitation. Only users with invitation management permissions can delete invitations.",
    ),
)
class InvitationViewSet(viewsets.ModelViewSet):
    queryset = models.Invitation.objects.all().order_by("-created")
    serializer_class = serializers.InvitationSerializer
    filter_backends = (
        DjangoFilterBackend,
        filters.InvitationScopeFilterBackend,
        filters.InvitationFilterBackend,
    )
    filterset_class = filters.InvitationFilter
    lookup_field = "uuid"

    def get_serializer_class(self):
        if self.action in ["update", "partial_update"]:
            return serializers.InvitationUpdateSerializer
        return super().get_serializer_class()

    def perform_update(self, serializer):
        invitation = self.get_object()
        if not can_manage_invitation_with(self.request, invitation.scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()

        # Store original values for logging
        original_values = {
            "email": invitation.email,
            "role": invitation.role,
        }

        serializer.save()

        # Log the changes
        self._log_invitation_changes(
            invitation, original_values, serializer.validated_data
        )

    def perform_destroy(self, instance):
        if not can_manage_invitation_with(self.request, instance.scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()

        # Log the deletion
        event_logger.emit(
            "Invitation to {invitation_email} for {scope_name} has been deleted by {user_username}.",
            event_type=EventType.USER_INVITATION_DELETED,
            event_context={
                "invitation_email": instance.email,
                "scope_name": instance.scope.name,
                "user_username": self.request.user.username,
                "invitation": instance,
                "user": self.request.user,
                "scope": instance.scope,
            },
            scopes=[instance.scope],
        )

        super().perform_destroy(instance)

    def _log_invitation_changes(self, invitation, original_values, new_values):
        """Log invitation changes for audit purposes"""
        changes = []
        for field, new_value in new_values.items():
            old_value = original_values.get(field)
            if old_value != new_value:
                if field == "role":
                    old_str = old_value.name if old_value else None
                    new_str = new_value.name if new_value else None
                    changes.append(f"{field}: {old_str} → {new_str}")
                else:
                    changes.append(f"{field}: {old_value} → {new_value}")

        if changes:
            event_logger.emit(
                "Invitation to {invitation_email} for {scope_name} has been updated by {user_username}. Changes: {changes_summary}",
                event_type=EventType.USER_INVITATION_UPDATED,
                event_context={
                    "invitation_email": invitation.email,
                    "scope_name": invitation.scope.name,
                    "user_username": self.request.user.username,
                    "changes_summary": ", ".join(changes),
                    "invitation": invitation,
                    "user": self.request.user,
                    "scope": invitation.scope,
                },
                scopes=[invitation.scope],
            )

    def perform_create(self, serializer):
        scope = serializer.validated_data["scope"]
        if not can_manage_invitation_with(self.request, scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()

        invitation: models.Invitation = serializer.save()
        if isinstance(invitation.scope, Project):
            project = cast(Project, invitation.scope)
            if project.start_date and project.start_date > timezone.now().date():
                invitation.state = InvitationState.PENDING_PROJECT
                invitation.save()
                return

        sender = self.request.user.full_name or self.request.user.username
        if (
            settings.WALDUR_CORE["ONLY_STAFF_CAN_INVITE_USERS"]
            and not self.request.user.is_staff
        ):
            invitation.state = InvitationState.REQUESTED
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

    @extend_schema(
        summary="Check for duplicate invitations",
        description=(
            "Returns pending invitations that already exist for the same email and role "
            "within the given scope."
        ),
        request=serializers.InvitationDuplicateCheckSerializer,
        responses=serializers.InvitationDuplicateCheckResponseSerializer,
    )
    @action(detail=False, methods=["post"], url_path="check-duplicates")
    def check_duplicates(self, request):
        serializer = serializers.InvitationDuplicateCheckSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        scope = serializer.validated_data["scope"]
        if not can_manage_invitation_with(request, scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()

        invitations = serializer.validated_data["invitations"]
        if not invitations:
            return Response({"duplicates": []})

        duplicates = get_invitation_duplicates(scope, invitations)

        response_serializer = serializers.InvitationDuplicateCheckResponseSerializer(
            {"duplicates": duplicates}
        )
        return Response(response_serializer.data)

    @extend_schema(
        summary="Approve a requested invitation",
        description="""
        For user's convenience invitation approval is performed without authentication.
        User UUID and invitation UUID is encoded into cryptographically signed token.
        """,
        request=serializers.TokenSerializer,
        responses=None,
    )
    @action(detail=False, methods=["post"], permission_classes=[])
    def approve(self, request):
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

    @extend_schema(
        summary="Reject a requested invitation",
        description="""
        For user's convenience invitation reject action is performed without authentication.
        User UUID and invitation UUID is encoded into cryptographically signed token.
        """,
        request=serializers.TokenSerializer,
        responses=None,
    )
    @action(detail=False, methods=["post"], permission_classes=[])
    def reject(self, request):
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

    @extend_schema(
        summary="Resend an invitation",
        description="Resends an email for a pending, expired, or canceled invitation. If the invitation was expired or canceled, its state is reset to 'pending' and its creation time is updated.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def send(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if not can_manage_invitation_with(self.request, invitation.scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()
        elif invitation.state not in (
            InvitationState.PENDING,
            InvitationState.EXPIRED,
            InvitationState.CANCELED,
        ):
            raise ValidationError(
                _("Only pending, expired and canceled invitations can be resent.")
            )

        reset_created = False
        if invitation.state in [
            InvitationState.EXPIRED,
            InvitationState.CANCELED,
        ]:
            invitation.state = InvitationState.PENDING
            invitation.created = timezone.now()
            reset_created = True

        if isinstance(invitation.scope, Project):
            project = cast(Project, invitation.scope)
            if project.start_date and project.start_date > timezone.now().date():
                invitation.state = InvitationState.PENDING_PROJECT
                invitation.created = timezone.now()
                invitation.save(update_fields=["state", "created"])
                return Response(
                    {
                        "detail": _(
                            "Invitation sending has been successfully scheduled."
                        )
                    },
                    status=status.HTTP_200_OK,
                )

        if reset_created:
            invitation.save(update_fields=["state", "created"])

        sender = request.user.full_name or request.user.username
        tasks.send_invitation_created.delay(invitation.uuid.hex, sender)
        return Response(
            {"detail": _("Invitation sending has been successfully scheduled.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Cancel an invitation",
        description="Cancels a pending or planned (pending_project) invitation.",
        request=None,
        responses={200: {"description": "Invitation has been successfully canceled."}},
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if not can_manage_invitation_with(self.request, invitation.scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()
        elif invitation.state not in [
            InvitationState.PENDING,
            InvitationState.PENDING_PROJECT,
        ]:
            raise ValidationError(
                _("Only pending or planned invitations can be canceled.")
            )

        invitation.cancel()
        return Response(
            {"detail": _("Invitation has been successfully canceled.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Delete an invitation (staff only)",
        description="Deletes an invitation. This action is restricted to staff users.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def delete(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if not self.request.user.is_staff:
            raise PermissionDenied()

        invitation.delete()
        return Response(
            {"detail": _("Invitation has been successfully deleted.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Accept an invitation",
        description="Accepts an invitation for the currently authenticated user. This grants the user the specified role in the invitation's scope.",
        request=None,
        responses={200: None},
    )
    @action(
        detail=True, methods=["post"], filter_backends=[filters.PendingInvitationFilter]
    )
    def accept(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if has_user(invitation.scope, request.user, invitation.role):
            raise ValidationError(_("User has already the same role in this scope."))

        if invitation.email.casefold() != request.user.email.casefold():
            if config.ENABLE_STRICT_CHECK_ACCEPTING_INVITATION:
                raise ValidationError(
                    _("User’s email and email of the invitation are not equal.")
                )

        if config.INVITATION_DISABLE_MULTIPLE_ROLES:
            if UserRole.objects.filter(
                user=request.user,
                is_active=True,
                content_type=invitation.content_type,
                object_id=invitation.object_id,
            ).exists():
                raise ValidationError(_("User already has role within this scope."))

        # Validate user against scope's email/affiliation restrictions
        validate_user_restrictions(invitation.scope, request.user)

        invitation.accept(request.user)

        return Response(
            {"detail": _("Invitation has been successfully accepted.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Check invitation validity",
        description="Checks if an invitation is pending and returns its email and whether a civil number is required for acceptance. This endpoint is public and does not require authentication.",
        request=None,
        responses=serializers.InvitationCheckSerializer,
        parameters=[],
    )
    @action(detail=True, methods=["post"], filter_backends=[], permission_classes=[])
    def check(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()

        if invitation.state != InvitationState.PENDING:
            return Response(status=status.HTTP_404_NOT_FOUND)
        elif invitation.civil_number:
            return Response(
                {"email": invitation.email, "civil_number_required": True},
                status=status.HTTP_200_OK,
            )
        else:
            return Response({"email": invitation.email}, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get public invitation details",
        description="Retrieves public-facing details of an invitation. This is used to show information to a user before they accept it.",
        request=None,
        responses=serializers.VisibleInvitationDetailsSerializer,
    )
    @action(detail=True, filter_backends=[filters.VisibleInvitationFilter])
    def details(self, request, uuid=None):
        invitation: models.Invitation = self.get_object()
        serializer = serializers.VisibleInvitationDetailsSerializer(instance=invitation)
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(
        summary="List group invitations",
        description="Retrieve a list of group invitations. Unauthenticated users can only see public invitations.",
    ),
    retrieve=extend_schema(
        summary="Retrieve group invitation",
        description="Retrieve details of a specific group invitation. Unauthenticated users can only see public invitations.",
    ),
    create=extend_schema(
        summary="Create group invitation",
        description="Create a new group invitation, which acts as a template for users to request permissions.",
    ),
    update=extend_schema(
        summary="Update a group invitation",
        description="Update an active group invitation. Only active invitations can be edited.",
    ),
    partial_update=extend_schema(
        summary="Partially update a group invitation",
        description="Partially update an active group invitation. Only active invitations can be edited.",
    ),
    destroy=extend_schema(
        summary="Delete a group invitation",
        description="Deletes an inactive group invitation. Only invitations that have been canceled (is_active=False) can be deleted.",
    ),
)
class GroupInvitationViewSet(ActionsViewSet):
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

    def get_permissions(self):
        """Allow unauthenticated access for list and retrieve of public invitations."""
        if self.action in ("list", "retrieve"):
            return []
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action in ["update", "partial_update"]:
            return serializers.GroupInvitationUpdateSerializer
        return super().get_serializer_class()

    def perform_update(self, serializer):
        invitation = self.get_object()
        if not can_manage_invitation_with(self.request, invitation.scope):
            raise NotFound()

        if not invitation.is_active:
            raise ValidationError(_("Only active invitations can be edited."))

        serializer.save()

        event_logger.emit(
            "Group invitation for {scope_name} has been updated by {user_username}.",
            event_type=EventType.USER_GROUP_INVITATION_UPDATED,
            event_context={
                "scope_name": invitation.scope.name,
                "user_username": self.request.user.username,
                "invitation": invitation,
                "user": self.request.user,
                "scope": invitation.scope,
            },
            scopes=[invitation.scope],
        )

    @extend_schema(
        summary="List projects for a customer-scoped group invitation",
        description="For a group invitation scoped to a customer, this endpoint lists all projects within that customer.",
        request=None,
        responses=structure_serializers.NestedProjectSerializer(
            many=True, read_only=True
        ),
        filters=False,
    )
    @action(detail=True, methods=["get"], filter_backends=[])
    def projects(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()

        if not isinstance(invitation.scope, Customer):
            return Response(status=status.HTTP_404_NOT_FOUND)

        projects = structure_serializers.NestedProjectSerializer(
            instance=Project.available_objects.filter(customer=invitation.customer),
            read_only=True,
            context={"request": request},
            many=True,
        )
        return Response(projects.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Cancel a group invitation",
        description="Cancels an active group invitation, preventing new permission requests from being created.",
        request=None,
        responses={200: None},
        parameters=[],
    )
    @action(detail=True, methods=["post"], filter_backends=[])
    def cancel(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()

        if not can_manage_invitation_with(request, invitation.scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()
        elif not invitation.is_active:
            raise ValidationError(_("Only pending invitation can be canceled."))

        invitation.cancel()
        return Response(
            {"detail": _("Invitation has been successfully canceled.")},
            status=status.HTTP_200_OK,
        )

    def destroy(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()

        if not can_manage_invitation_with(request, invitation.scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()
        elif invitation.is_active:
            raise ValidationError(_("Only canceled invitation can be deleted."))

        invitation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Submit a permission request",
        description="Creates a permission request based on a group invitation for the currently authenticated user. If the invitation has auto_approve enabled and the user matches the required patterns, the request is automatically approved.",
        request=serializers.SubmitRequestSerializer,
        responses={200: serializers.SubmitRequestResponseSerializer},
    )
    @action(detail=True, methods=["post"], filter_backends=[])
    def submit_request(self, request, uuid=None):
        invitation: models.GroupInvitation = self.get_object()
        user = request.user

        request_serializer = serializers.SubmitRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)

        if not invitation.is_active:
            raise ValidationError(_("Only pending invitation can be requested."))

        # Check if user already has the requested role in the scope
        if has_user(invitation.scope, user, invitation.role):
            raise ValidationError(_("User already has this role in the scope."))

        # Check if multiple roles are disabled for this scope
        if config.INVITATION_DISABLE_MULTIPLE_ROLES:
            if UserRole.objects.filter(
                user=user,
                is_active=True,
                content_type=invitation.content_type,
                object_id=invitation.object_id,
            ).exists():
                raise ValidationError(_("User already has role within this scope."))

        if not invitation.allow_multiple_requests:
            if models.PermissionRequest.objects.filter(
                invitation__content_type=invitation.content_type,
                invitation__object_id=invitation.object_id,
                created_by=user,
                state__in=[ReviewStates.PENDING, ReviewStates.APPROVED],
            ).exists():
                raise ValidationError(
                    _("Permission request already exists for this scope.")
                )

        allowed = invitation in models.GroupInvitation.get_objects_by_user_patterns(
            user, required=False
        )

        if not allowed:
            raise ValidationError(
                "You are not allowed to accept this invitation. "
                "Your email or organization must match the invitation restrictions."
            )

        # Validate user against scope's email/affiliation restrictions
        validate_user_restrictions(invitation.scope, user)

        # Only use custom project details if the invitation allows it
        project_name = ""
        project_description = ""
        if invitation.allow_custom_project_details:
            project_name = request_serializer.validated_data.get("project_name", "")
            project_description = request_serializer.validated_data.get(
                "project_description", ""
            )

        permission_request = models.PermissionRequest.objects.create(
            invitation=invitation,
            created_by=request.user,
            project_name=project_name,
            project_description=project_description,
        )

        permission_request.submit()

        # Auto-approve if invitation is configured for auto-approval
        auto_approved = False
        project_uuid = None
        project_created = None
        if invitation.auto_approve:
            result = permission_request.approve(request.user)
            auto_approved = True
            if result and result.get("project") is not None:
                project_uuid = result["project"].uuid.hex
                project_created = bool(result.get("project_created"))

        # Get scope details safely
        scope_name = ""
        scope_uuid = ""
        if invitation.scope:
            scope_name = getattr(invitation.scope, "name", str(invitation.scope))
            scope_uuid = str(invitation.scope.uuid)

        # Use the serializer to validate and format the response
        response_serializer = serializers.SubmitRequestResponseSerializer(
            data={
                "uuid": permission_request.uuid.hex,
                "scope_name": scope_name,
                "scope_uuid": scope_uuid,
                "auto_approved": auto_approved,
                "project_uuid": project_uuid,
                "project_created": project_created,
            }
        )
        response_serializer.is_valid(raise_exception=True)

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )

    def perform_create(self, serializer):
        scope = serializer.validated_data["scope"]
        if not can_manage_invitation_with(self.request, scope):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()

        serializer.save()


@extend_schema_view(
    list=extend_schema(
        summary="List permission requests",
        description="Retrieve a list of permission requests visible to the user.",
    ),
    retrieve=extend_schema(
        summary="Retrieve permission request",
        description="Retrieve details of a specific permission request.",
    ),
    destroy=extend_schema(
        summary="Delete a permission request (staff only)",
        description="Deletes a permission request. This action is restricted to staff users.",
    ),
)
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

        if not can_manage_permission_request(
            self.request, permission_request.invitation
        ):
            # Raise NotFound instead of PermissionDenied to hide invitation existence
            raise NotFound()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")

        getattr(permission_request, action_name)(self.request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Approve a permission request",
        description="Approves a pending permission request, granting the requesting user the permissions defined in the associated group invitation.",
        request=core_serializers.ReviewCommentSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        return self.perform_action(request, uuid, "approve")

    @extend_schema(
        summary="Reject a permission request",
        description="Rejects a pending permission request.",
        request=core_serializers.ReviewCommentSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        return self.perform_action(request, uuid, "reject")

    @extend_schema(
        summary="Cancel a permission request",
        description="Cancels a pending or draft permission request. This can be done by the user who created the request or by a staff member.",
        request=None,
        responses=serializers.CancelRequestResponseSerializer,
    )
    @action(detail=True, methods=["post"])
    def cancel_request(self, request, uuid=None):
        """Cancel permission request. Users can cancel their own requests, staff can cancel any request."""
        permission_request: models.PermissionRequest = self.get_object()

        # Check that the user canceling is the same user who created the request OR is staff
        if permission_request.created_by != request.user and not request.user.is_staff:
            raise PermissionDenied(
                _("You can only cancel your own permission requests.")
            )

        # Check that the request is in a state that can be canceled
        if permission_request.state not in [ReviewStates.PENDING, ReviewStates.DRAFT]:
            raise ValidationError(_("Only pending or draft requests can be canceled."))

        permission_request.cancel()

        # Get scope details safely
        invitation = permission_request.invitation
        scope_name = ""
        scope_uuid = ""
        if invitation.scope:
            scope_name = getattr(invitation.scope, "name", str(invitation.scope))
            scope_uuid = str(invitation.scope.uuid)

        # Use the serializer to validate and format the response
        response_serializer = serializers.CancelRequestResponseSerializer(
            data={
                "uuid": permission_request.uuid.hex,
                "scope_name": scope_name,
                "scope_uuid": scope_uuid,
            }
        )
        response_serializer.is_valid(raise_exception=True)

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )

    disabled_actions = ["create", "update", "partial_update"]

    @extend_schema(
        summary="Delete a permission request (staff only)",
        description="Deletes a permission request. This action is restricted to staff users.",
        responses={204: None},
    )
    def destroy(self, request, uuid=None):
        permission_request = self.get_object()

        if not request.user.is_staff:
            raise PermissionDenied()

        permission_request.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    approve_serializer_class = reject_serializer_class = (
        core_serializers.ReviewCommentSerializer
    )
    approve_validators = reject_validators = [
        core_validators.StateValidator(ReviewStates.PENDING, state_enum=ReviewStates)
    ]
