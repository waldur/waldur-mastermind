import hashlib
import hmac
import logging
import re
import secrets
from urllib.parse import quote

import httpx
import yaml
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.http import FileResponse, Http404
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import permissions, status, views
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from waldur_core.core import permissions as core_permissions
from waldur_core.core.auth_utils import (
    AUTH_METHOD_SESSION,
    AUTH_METHOD_TOKEN,
    get_auth_method,
)
from waldur_core.core.models import User
from waldur_core.core.utils import _strip_port
from waldur_core.core.views import ActionsViewSet
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.server.middleware import add_public_cors_headers
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.models import Project

from . import (
    appservice_registration,
    bot_state,
    crypto_setup,
    filters,
    livekit_client,
    matrix_client,
    models,
    room_provisioning,
    serializers,
    tasks,
)
from .managers import (
    can_manage_room,
    filter_exports_for_request,
    get_accessible_room_ids,
    get_unlinked_room_users,
)

logger = logging.getLogger(__name__)

# Matrix Application Service spec v1 endpoint the homeserver PUTs transactions to.
# The registration YAML's `url:` is the base URL; Synapse appends this path itself.
MATRIX_APPSERVICE_WEBHOOK_PATH = "/_matrix/app/v1/transactions/{txnId}"

# Per-call budget for diagnostics httpx.get(). Set tighter than the cumulative
# 5s/check so a single hung connection can't monopolize the staff's request.
DIAGNOSTICS_TIMEOUT = httpx.Timeout(connect=3.0, read=2.0, write=2.0, pool=2.0)

# Unlinked users named in diagnostics; the rest are only counted.
UNLINKED_USERS_SHOWN = 10


def _token_fingerprint(token):
    """Return a short SHA-256 fingerprint of a secret token for display."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


class MatrixEnabledWriteGuardMixin:
    """Reject mutating requests while the Matrix integration is disabled.

    Reads stay open so the rooms list remains viewable, but a write would only
    enqueue a Celery task against a non-existent homeserver: the row is left
    stranded in a transient state (creating/disabling) that never resolves. The
    frontend hides these actions; this is the backstop for direct or stale calls.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if (
            request.method not in permissions.SAFE_METHODS
            and not matrix_client.is_enabled()
        ):
            raise ValidationError("Matrix chat is disabled.")


class MatrixRoomViewSet(MatrixEnabledWriteGuardMixin, ActionsViewSet):
    queryset = models.MatrixRoom.objects.all().order_by("-created")
    serializer_class = serializers.MatrixRoomSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.MatrixRoomFilter
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update"]

    create_serializer_class = serializers.MatrixRoomCreateSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return queryset.none()
        if user.is_staff or user.is_support:
            return queryset
        return queryset.filter(id__in=get_accessible_room_ids(user))

    @extend_schema(
        request=serializers.MatrixRoomCreateSerializer,
        responses={201: serializers.MatrixRoomSerializer},
        summary="Create a Matrix room for a project",
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project = serializer.validated_data["project"]

        room = room_provisioning.provision_project_room(
            project, created_by=request.user
        )
        if room is None:
            # Lost the race against a concurrent create for the same project;
            # the serializer's uniqueness check passed before the row landed.
            raise ValidationError("A Matrix room already exists for this project.")

        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="List projects the caller can create a Matrix room for",
        responses={200: serializers.EligibleProjectSerializer(many=True)},
        parameters=[
            OpenApiParameter(
                "customer_uuid",
                str,
                OpenApiParameter.QUERY,
                description="Limit results to projects under this customer.",
                required=False,
            ),
        ],
        description="Returns projects where the caller holds MATRIX_ROOM.CREATE "
        "on the project or its organization (staff and support see all) and no "
        "MatrixRoom row exists yet. Existing archived rooms still block "
        "creation, so projects with any room are excluded.",
    )
    @action(detail=False, methods=["get"])
    def eligible_projects(self, request):
        user = request.user
        projects = room_provisioning.creatable_projects(user)

        customer_uuid = request.query_params.get("customer_uuid")
        if customer_uuid:
            projects = projects.filter(customer__uuid=customer_uuid)

        project_ct = ContentType.objects.get_for_model(Project)
        taken_project_ids = models.MatrixRoom.objects.filter(
            content_type=project_ct
        ).values_list("object_id", flat=True)
        projects = projects.exclude(id__in=taken_project_ids).select_related("customer")

        data = [
            {
                "uuid": p.uuid.hex,
                "name": p.name,
                "customer_uuid": p.customer.uuid.hex,
                "customer_name": p.customer.name,
            }
            for p in projects
        ]
        serializer = serializers.EligibleProjectSerializer(data, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="List room members",
        responses={200: serializers.MatrixRoomMemberSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def members(self, request, uuid=None):
        room = self.get_object()
        queryset = room.members.select_related("user").order_by(
            "membership_state", "created"
        )
        page = self.paginate_queryset(queryset)
        serializer = serializers.MatrixRoomMemberSerializer(
            page or queryset, many=True, context=self.get_serializer_context()
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @extend_schema(
        summary="Force sync room membership with project members",
        request=None,
        responses={202: None},
    )
    @action(detail=True, methods=["post"])
    def sync_members(self, request, uuid=None):
        room = self.get_object()
        room_uuid = str(room.uuid)
        transaction.on_commit(
            lambda: tasks.sync_project_members_to_room.delay(room_uuid)
        )
        return Response(status=status.HTTP_202_ACCEPTED)

    sync_members_permissions = [can_manage_room]

    @extend_schema(
        summary="Trigger manual history export",
        request=None,
        responses={202: serializers.MatrixHistoryExportSerializer},
    )
    @action(detail=True, methods=["post"])
    def export_history(self, request, uuid=None):
        room = self.get_object()
        export = models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=models.ExportTypes.MANUAL,
        )
        export_uuid = str(export.uuid)
        transaction.on_commit(lambda: tasks.export_room_history.delay(export_uuid))
        output_serializer = serializers.MatrixHistoryExportSerializer(
            export, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    export_history_permissions = [can_manage_room]

    @extend_schema(
        summary="Retry a stuck or failed room operation",
        request=None,
        responses={202: serializers.MatrixRoomSerializer},
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, uuid=None):
        room = self.get_object()
        retryable_states = (
            models.RoomStates.ERROR,
            models.RoomStates.CREATING,
            models.RoomStates.DISABLING,
        )
        room_uuid = str(room.uuid)
        with transaction.atomic():
            # Lock the row before reading state — DRF state validators are not
            # transactional, so concurrent calls can pass the eligibility check
            # and either double-dispatch the celery task or step on each
            # other's state transitions (500 instead of a clean 409).
            room = models.MatrixRoom.objects.select_for_update().get(pk=room.pk)
            if room.state not in retryable_states:
                return Response(
                    {
                        "detail": (
                            "Only rooms in error, creating or disabling state can be retried."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            if room.state == models.RoomStates.ERROR:
                try:
                    room.retry_creating()
                except TransitionNotAllowed:
                    raise ValidationError(
                        f"Cannot retry room in state {room.get_state_display()}."
                    )
                room.save(update_fields=["state", "error_message"])
            if room.state == models.RoomStates.DISABLING:
                # The original delete_history choice isn't persisted on the row,
                # so retry defaults to False (non-destructive).
                transaction.on_commit(
                    lambda: tasks.disable_room.delay(
                        room_uuid,
                        delete_history=False,
                    )
                )
            else:
                transaction.on_commit(lambda: tasks.create_room.delay(room_uuid))
        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    retry_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Disable an active chat room",
        request=serializers.MatrixRoomDisableSerializer,
        responses={202: serializers.MatrixRoomSerializer},
    )
    @action(detail=True, methods=["post"])
    def disable(self, request, uuid=None):
        room = self.get_object()
        disable_serializer = serializers.MatrixRoomDisableSerializer(data=request.data)
        disable_serializer.is_valid(raise_exception=True)
        room_uuid = str(room.uuid)
        delete_history = disable_serializer.validated_data["delete_history"]
        with transaction.atomic():
            room = models.MatrixRoom.objects.select_for_update().get(pk=room.pk)
            try:
                room.begin_disabling()
            except TransitionNotAllowed:
                raise ValidationError(
                    f"Cannot disable room in state {room.get_state_display()}."
                )
            room.save(update_fields=["state"])
            transaction.on_commit(
                lambda: tasks.disable_room.delay(
                    room_uuid,
                    delete_history=delete_history,
                )
            )
        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    disable_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Re-enable an archived chat room",
        request=None,
        responses={202: serializers.MatrixRoomSerializer},
    )
    @action(detail=True, methods=["post"])
    def reactivate(self, request, uuid=None):
        room = self.get_object()
        room_uuid = str(room.uuid)
        with transaction.atomic():
            room = models.MatrixRoom.objects.select_for_update().get(pk=room.pk)
            try:
                room.reactivate()
            except TransitionNotAllowed:
                raise ValidationError(
                    f"Cannot reactivate room in state {room.get_state_display()}."
                )
            room.save(update_fields=["state"])
            transaction.on_commit(
                lambda: tasks.sync_project_members_to_room.delay(room_uuid)
            )
            # Mirror the "Chat room was deactivated" marker on the way back up.
            # Posted by the bot without attribution: only staff can reactivate.
            transaction.on_commit(
                lambda: tasks.send_room_notification.delay(
                    room_uuid, "Chat room was reactivated"
                )
            )
        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    reactivate_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Open a chat room's conversation",
        request=None,
        responses={
            200: serializers.MatrixRoomOpenSerializer,
            403: OpenApiResponse(description="The caller is not a member of the room."),
            409: OpenApiResponse(description="The room is not active."),
        },
        description="Returns the Matrix room ID to a current member of the room, "
        "accepting their pending invite. Other callers get 403; a room that is "
        "not active gets 409.",
    )
    @action(detail=True, methods=["post"])
    def open(self, request, uuid=None):
        room = self.get_object()
        member = models.MatrixRoomMember.objects.filter(
            room=room,
            user=request.user,
            membership_state__in=[
                models.MembershipStates.INVITED,
                models.MembershipStates.JOINED,
            ],
        ).first()
        # Role-based access to the room (staff, a customer owner who is not a
        # project member) grants managing it, not reading the conversation.
        if member is None:
            raise PermissionDenied("You are not a member of this room.")
        if room.state != models.RoomStates.ACTIVE or not room.room_id:
            return Response(
                {"detail": "Only active rooms can be opened."},
                status=status.HTTP_409_CONFLICT,
            )

        if member.membership_state == models.MembershipStates.INVITED:
            try:
                matrix_client.join_room_as_user(room.room_id, member.matrix_user_id)
            except (matrix_client.MatrixClientError, httpx.HTTPError):
                # The drawer joins on its own once synced, so a homeserver
                # hiccup here should not block opening the room.
                logger.warning(
                    "Failed to accept invite of %s to %s",
                    member.matrix_user_id,
                    room.room_id,
                )
            else:
                # Only an invite still pending: a sync or leave may have moved
                # the row on while the join was in flight.
                models.MatrixRoomMember.objects.filter(
                    pk=member.pk, membership_state=models.MembershipStates.INVITED
                ).update(membership_state=models.MembershipStates.JOINED)

        serializer = serializers.MatrixRoomOpenSerializer({"room_id": room.room_id})
        return Response(serializer.data)

    @extend_schema(
        summary="Join a chat room as staff",
        request=None,
        responses={202: serializers.MatrixRoomSerializer},
        description="Staff self-join: provisions the caller, adds them with a "
        "Moderator badge (power level 50), and posts a bot announcement.",
    )
    @action(detail=True, methods=["post"])
    def join(self, request, uuid=None):
        room = self.get_object()
        if room.state != models.RoomStates.ACTIVE:
            return Response(
                {"detail": "Only active rooms can be joined."},
                status=status.HTTP_409_CONFLICT,
            )
        room_uuid = str(room.uuid)
        user_uuid = str(request.user.uuid)
        transaction.on_commit(lambda: tasks.staff_join_room.delay(room_uuid, user_uuid))
        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    join_permissions = [structure_permissions.is_staff_or_support]

    @extend_schema(
        summary="Leave a chat room as staff",
        request=None,
        responses={202: serializers.MatrixRoomSerializer},
        description="Staff self-leave: posts a bot announcement and removes the "
        "caller from the room.",
    )
    @action(detail=True, methods=["post"])
    def leave(self, request, uuid=None):
        room = self.get_object()
        if room.state != models.RoomStates.ACTIVE:
            return Response(
                {"detail": "Only active rooms can be left."},
                status=status.HTTP_409_CONFLICT,
            )
        room_uuid = str(room.uuid)
        user_uuid = str(request.user.uuid)
        transaction.on_commit(
            lambda: tasks.staff_leave_room.delay(room_uuid, user_uuid)
        )
        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    leave_permissions = [structure_permissions.is_staff_or_support]

    def destroy(self, request, uuid=None):
        room = self.get_object()
        if room.state not in (
            models.RoomStates.ERROR,
            models.RoomStates.CREATING,
            models.RoomStates.ARCHIVED,
        ):
            return Response(
                {
                    "detail": "Only rooms in error, creating, or archived state can be deleted."
                },
                status=status.HTTP_409_CONFLICT,
            )
        room.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    destroy_permissions = [structure_permissions.is_staff]


class MatrixHistoryExportViewSet(ActionsViewSet):
    queryset = models.MatrixHistoryExport.objects.all().order_by("-created")
    serializer_class = serializers.MatrixHistoryExportSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.MatrixHistoryExportFilter
    lookup_field = "uuid"
    disabled_actions = ["create", "destroy", "update", "partial_update"]

    def get_queryset(self):
        return filter_exports_for_request(super().get_queryset(), self.request)


class MatrixCredentialsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    # Per-user rate limit: a malicious authenticated user can otherwise flood
    # the homeserver with auto-provisioning calls.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_credentials"

    @extend_schema(
        summary="Get Matrix login credentials",
        responses={200: serializers.MatrixCredentialsSerializer},
        description="Returns what an external Matrix client needs to sign the "
        "authenticated user in, per MATRIX_EXTERNAL_LOGIN_METHOD. Never a "
        "password or an access token: in password mode the user generates a "
        "password with POST /api/matrix/credentials/password/.",
    )
    def get(self, request):
        # Don't auto-provision a Matrix account for callers when the integration
        # is disabled. 404 hides the endpoint's existence on flag-off; staff
        # see "Not Found" too, matching the homeport behavior on the flag.
        if not matrix_client.is_enabled():
            raise Http404
        # Provisioning on demand fails on the homeserver's side, as in the
        # session view, so it is answered the same way; the homeserver's own
        # text stays in the log.
        try:
            matrix_client.ensure_user_exists(request.user)
        except (matrix_client.MatrixClientError, httpx.HTTPError, ValueError) as e:
            logger.warning("Could not provision %s on Matrix: %s", request.user, e)
            return Response(
                {"detail": "Chat is unavailable right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            credentials = matrix_client.get_user_matrix_credentials(request.user)
        except matrix_client.MatrixClientError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(credentials)


class MatrixPasswordView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_password"

    @extend_schema(
        summary="Generate a Matrix password",
        request=None,
        responses={
            200: serializers.MatrixPasswordSerializer,
            403: OpenApiResponse(
                description="The user was deactivated while the request ran."
            ),
            404: OpenApiResponse(description="Matrix chat is not enabled."),
            409: OpenApiResponse(description="Matrix passwords are not in use."),
            503: OpenApiResponse(
                description="The homeserver did not set it: either the Waldur bot "
                "is not a homeserver admin yet, the homeserver's admin API is "
                "not reachable or the account is a homeserver admin's, which "
                "needs an operator, or the homeserver failed and a later try "
                "may succeed. The detail says which."
            ),
        },
        description="Sets a new random password on the authenticated user's "
        "Matrix account and returns it. It is not stored, so it is shown only "
        "in this response; generating again replaces it. Only in password mode.",
    )
    def post(self, request):
        if not matrix_client.is_enabled():
            raise Http404
        if config.MATRIX_EXTERNAL_LOGIN_METHOD != "password":
            return Response(
                {"detail": "Matrix passwords are not in use on this site."},
                status=status.HTTP_409_CONFLICT,
            )
        password = matrix_client.new_password()
        try:
            matrix_user_id = matrix_client.ensure_user_exists(request.user)
            matrix_client.set_password(matrix_user_id, password)
        except (
            matrix_client.MatrixAdminRequired,
            matrix_client.MatrixAccountIsHomeserverAdmin,
        ) as e:
            # Stays so until an operator acts: makes the bot a homeserver
            # admin, or takes the user off an admin's account.
            logger.warning(
                "Could not set the Matrix password of %s: %s", request.user, e
            )
            return Response(
                {
                    "detail": "Matrix passwords are not available yet; ask your "
                    "administrator."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except (matrix_client.MatrixClientError, httpx.HTTPError, ValueError) as e:
            logger.warning(
                "Could not set the Matrix password of %s: %s", request.user, e
            )
            return Response(
                {"detail": "Chat is unavailable right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # A deactivation committed meanwhile may have found no Matrix account
        # to lock yet, and would never lock the one provisioned here.
        if not User.all_objects.filter(pk=request.user.pk, is_active=True).exists():
            tasks.end_matrix_access.delay(request.user.uuid.hex)
            raise PermissionDenied("This account has been deactivated.")

        event_logger.emit(
            "User {affected_user_username} has generated a Matrix password.",
            event_type=EventType.MATRIX_PASSWORD_GENERATED,
            event_context={"affected_user": request.user},
            scopes=[request.user],
        )

        serializer = serializers.MatrixPasswordSerializer(
            {
                "homeserver_url": matrix_client.get_public_homeserver_url(),
                "matrix_user_id": matrix_user_id,
                "password": password,
            }
        )
        response = Response(serializer.data)
        response["Cache-Control"] = "no-store"
        return response


class MatrixSessionView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    # Called when the chat drawer connects and when its Matrix refresh is
    # rejected; each call is an appservice login and a new device.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_session"

    @extend_schema(
        summary="Start a Matrix web chat session",
        request=None,
        responses={
            200: serializers.MatrixSessionSerializer,
            404: OpenApiResponse(description="Matrix chat is not enabled."),
            503: OpenApiResponse(
                description="The homeserver could not start a session: either it "
                "failed and a later try may succeed, or the user's Matrix account "
                "is locked, which needs an operator. The detail says which."
            ),
        },
        description="Signs the caller in to Matrix on a new web device and returns "
        "short-lived tokens for the chat drawer. The tokens are not stored.",
    )
    def post(self, request):
        if not matrix_client.is_enabled():
            raise Http404
        try:
            matrix_user_id = matrix_client.ensure_user_exists(request.user)
            session = matrix_client.create_web_session(matrix_user_id)
        except matrix_client.MatrixUserLocked as e:
            # A deactivation committed meanwhile, whose task was faster.
            if not User.all_objects.filter(pk=request.user.pk, is_active=True).exists():
                raise PermissionDenied("This account has been deactivated.")
            # Waldur locks an account at deactivation and unlocks it only at
            # reactivation, so this stays so until an operator acts: the unlock
            # failed, or the account was a deleted user's.
            logger.warning(
                "The Matrix account of %s is locked: %s. Deactivate and "
                "reactivate the user, or unlock the account on the homeserver",
                request.user,
                e,
            )
            return Response(
                {"detail": "Your chat account is locked; ask your administrator."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except (matrix_client.MatrixClientError, httpx.HTTPError, ValueError) as e:
            # An upstream or configuration fault; 503 marks it as temporary and
            # the homeserver's own text stays in the log. Provisioning parses
            # success bodies itself, so a garbled one surfaces as a ValueError.
            logger.warning(
                "Could not start a Matrix web session for %s: %s", request.user, e
            )
            return Response(
                {"detail": "Chat is unavailable right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # A deactivation committed meanwhile may have listed the user's devices
        # before this one existed, and would never sign it out.
        if not User.all_objects.filter(pk=request.user.pk, is_active=True).exists():
            try:
                matrix_client.logout_device(matrix_user_id, session["device_id"])
            except matrix_client.MatrixClientError as e:
                logger.warning(
                    "Could not sign out session of deactivated %s: %s", request.user, e
                )
            # It may also have found no Matrix account to lock yet.
            tasks.end_matrix_access.delay(request.user.uuid.hex)
            raise PermissionDenied("This account has been deactivated.")

        tasks.prune_web_devices.delay(matrix_user_id, session["device_id"])
        recovery_key = (
            None
            if _is_delegated(request)
            else models.MatrixUserProfile.objects.filter(user=request.user)
            .values_list("recovery_key", flat=True)
            .first()
        )
        serializer = serializers.MatrixSessionSerializer(
            {
                "homeserver_url": matrix_client.get_public_homeserver_url(),
                "matrix_user_id": matrix_user_id,
                "recovery_key": recovery_key or None,
                **session,
            }
        )
        return _no_store(Response(serializer.data))


# The ways the web UI authenticates: the user's Waldur API token, or a session.
# Refused: a personal access token (scoped for a script) and an OIDC access
# token (which may belong to another client of the identity provider). The API
# token is not proof of an interactive sign-in, though: the user can copy it
# into a script, and staff can fetch it. Binding key access to the sign-in
# itself is a separate change.
FIRST_PARTY_AUTH_METHODS = (AUTH_METHOD_TOKEN, AUTH_METHOD_SESSION)


def _is_delegated(request):
    """Whether the caller authenticated other than the way the web UI does.

    An allowlist, so a new kind of authentication is refused until it is
    considered. Staff impersonating a user come in with a token but act on the
    user's behalf, so they are refused too.
    """
    return get_auth_method(request.auth) not in FIRST_PARTY_AUTH_METHODS or bool(
        getattr(request.user, "impersonator", None)
    )


def _refuse_delegated(request):
    if _is_delegated(request):
        raise PermissionDenied(
            "Chat encryption keys are only available to the user's own sign-in."
        )


def _no_store(response):
    # The response carries a secret; keep it out of every cache on the way.
    response["Cache-Control"] = "no-store"
    return response


def _crypto_conflict(e):
    headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
    return Response(
        {"state": e.state, "detail": e.detail},
        status=status.HTTP_409_CONFLICT,
        headers=headers,
    )


def _caller_profile(request):
    if not matrix_client.is_enabled():
        raise Http404
    matrix_client.ensure_user_exists(request.user)
    return models.MatrixUserProfile.objects.get(user=request.user)


_CRYPTO_UNAVAILABLE = OpenApiResponse(
    description="The homeserver could not be asked, or the bot is not a "
    "homeserver admin (needed for a reset)."
)


class MatrixCryptoLeaseView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_crypto"

    @extend_schema(
        summary="Take the lease to set up or reset chat encryption",
        request=serializers.MatrixCryptoLeaseRequestSerializer,
        responses={
            200: serializers.MatrixCryptoLeaseSerializer,
            404: OpenApiResponse(description="Matrix chat is not enabled."),
            409: serializers.MatrixCryptoConflictSerializer,
            503: _CRYPTO_UNAVAILABLE,
        },
        description="Admits one browser at a time to setting up the caller's "
        "end-to-end encryption (`bootstrap`), or to replacing an identity Waldur "
        "can't unlock (`reset`). A reset also returns a temporary password for "
        "the homeserver's interactive auth; it is replaced once the new recovery "
        "key is escrowed, or when the lease runs out. 409 says why the request "
        "can't proceed: `set_up`, `locked` (set up without a key Waldur holds), "
        "`not_locked`, or `in_progress` (with Retry-After).",
    )
    def post(self, request):
        _refuse_delegated(request)
        data = serializers.MatrixCryptoLeaseRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        kind = data.validated_data["kind"]
        try:
            profile = _caller_profile(request)
            lease, expires_at, password = crypto_setup.acquire_lease(profile, kind)
        except crypto_setup.CryptoConflict as e:
            return _crypto_conflict(e)
        except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
            logger.warning(
                "Could not start the encryption %s of %s: %s", kind, request.user, e
            )
            return Response(
                {"detail": "Chat is unavailable right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if password:
            tasks.scrub_temporary_matrix_password.apply_async(
                (profile.matrix_user_id, lease),
                countdown=int(crypto_setup.LEASE_TTL.total_seconds()),
            )
            event_logger.emit(
                "User {affected_user_username} has started resetting their chat "
                "encryption.",
                event_type=EventType.MATRIX_ENCRYPTION_RESET_STARTED,
                event_context={"affected_user": request.user},
                scopes=[request.user],
            )
        return _no_store(
            Response(
                serializers.MatrixCryptoLeaseSerializer(
                    {
                        "lease": lease,
                        "expires_at": expires_at,
                        "temporary_password": password,
                    }
                ).data
            )
        )


class MatrixCryptoEscrowView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_crypto"

    @extend_schema(
        summary="Escrow the recovery key of chat encryption",
        request=serializers.MatrixCryptoEscrowSerializer,
        responses={
            204: None,
            404: OpenApiResponse(description="Matrix chat is not enabled."),
            409: serializers.MatrixCryptoConflictSerializer,
        },
        description="Stores the caller's new secret-storage recovery key. Only "
        "the holder of a current lease may; the drawer calls this before it "
        "uploads any key, so Waldur never loses a key the homeserver depends on.",
    )
    def post(self, request):
        _refuse_delegated(request)
        data = serializers.MatrixCryptoEscrowSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            profile = _caller_profile(request)
            kind = crypto_setup.escrow(
                profile,
                data.validated_data["lease"],
                data.validated_data["recovery_key"],
            )
        except crypto_setup.CryptoConflict as e:
            return _crypto_conflict(e)
        except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
            logger.warning("Could not escrow the key of %s: %s", request.user, e)
            return Response(
                {"detail": "Chat is unavailable right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if kind == models.CryptoLeaseKinds.RESET:
            tasks.scrub_temporary_matrix_password.delay(
                profile.matrix_user_id, data.validated_data["lease"]
            )
        event_logger.emit(
            "User {affected_user_username} has stored a new chat encryption "
            "recovery key.",
            event_type=EventType.MATRIX_RECOVERY_KEY_ESCROWED,
            event_context={"affected_user": request.user},
            scopes=[request.user],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class MatrixCryptoLeaseReleaseView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_crypto"

    @extend_schema(
        summary="Release the lease to set up or reset chat encryption",
        request=serializers.MatrixCryptoLeaseReleaseSerializer,
        responses={204: None, 404: OpenApiResponse(description="Chat disabled.")},
        description="Ends the caller's lease once its setup or reset is done, "
        "or has failed, so another window need not wait for it to run out. A "
        "lease that is no longer held is ignored.",
    )
    def post(self, request):
        _refuse_delegated(request)
        data = serializers.MatrixCryptoLeaseReleaseSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        if not matrix_client.is_enabled():
            raise Http404
        profile = models.MatrixUserProfile.objects.filter(user=request.user).first()
        lease = data.validated_data["lease"]
        if profile and crypto_setup.release_lease(profile, lease) == (
            models.CryptoLeaseKinds.RESET
        ):
            # A reset that ended, done or failed, needs its password no more.
            tasks.scrub_temporary_matrix_password.delay(profile.matrix_user_id, lease)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _is_from_homeserver(request):
    """Check the homeserver token in the Authorization header.

    A function of its own, so the token is not among the view's locals that
    Sentry records for an error raised later in the request.
    """
    hs_token = config.MATRIX_APPSERVICE_HS_TOKEN
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    # Constant-time, as a naive `!=` leaks the token byte by byte. Bytes,
    # because compare_digest raises on non-ASCII text and anyone can send this
    # header.
    if hs_token and hmac.compare_digest(provided.encode(), hs_token.encode()):
        return True

    # A homeserver holding another hs_token gets a 403 on every call, and bot
    # commands and invites just stop; without this nothing on Waldur's side
    # says why.
    if not hs_token:
        reason = "MATRIX_APPSERVICE_HS_TOKEN is not set"
    elif not provided:
        reason = "no token"
    else:
        reason = (
            "the token is not MATRIX_APPSERVICE_HS_TOKEN; if this is the "
            "homeserver, register the appservice there again"
        )
    logger.warning("Rejected a homeserver call to %s: %s", request.path, reason)
    return False


class MatrixAppserviceWebhookView(views.APIView):
    authentication_classes = ()
    permission_classes = ()
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_webhook"

    @extend_schema(
        summary="Matrix Application Service transaction webhook",
        description="Receives event transactions from the Matrix homeserver. "
        "Authenticated via hs_token in the Authorization header.",
        request=None,
        responses={200: None},
    )
    def put(self, request, txn_id):
        # No-op transactions when Matrix is disabled. 200 keeps the homeserver
        # from retrying — there's nothing to recover.
        if not matrix_client.is_enabled():
            return Response({}, status=status.HTTP_200_OK)

        if not _is_from_homeserver(request):
            return Response(status=status.HTTP_403_FORBIDDEN)

        events = (
            request.data.get("events", []) if isinstance(request.data, dict) else None
        )
        if not isinstance(events, list):
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # Idempotency check
        _, created = models.MatrixAppserviceTransaction.objects.get_or_create(
            txn_id=txn_id,
            defaults={"event_count": len(events)},
        )
        if not created:
            return Response({}, status=status.HTTP_200_OK)

        # Nothing more to do: the bot reads the rooms' events, commands included,
        # from its own sync, where it can decrypt them. The homeserver still
        # needs each transaction acknowledged.
        return Response({}, status=status.HTTP_200_OK)


class MatrixAppservicePingView(views.APIView):
    authentication_classes = ()
    permission_classes = ()
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_webhook"

    @extend_schema(
        summary="Matrix Application Service ping",
        description="Called by the homeserver when Waldur asks it to ping the "
        "appservice (MSC2659). Authenticated via hs_token in the Authorization "
        "header.",
        request=None,
        responses={200: None},
    )
    def post(self, request):
        if not _is_from_homeserver(request):
            return Response(status=status.HTTP_403_FORBIDDEN)
        return Response({}, status=status.HTTP_200_OK)


class MatrixAppserviceSetupView(views.APIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(
        summary="Setup Matrix appservice registration",
        request=serializers.MatrixAppserviceSetupSerializer,
        responses={200: serializers.MatrixAppserviceSetupResponseSerializer},
        description="Generates fresh appservice tokens (rotating any existing ones), "
        "enables the appservice, and returns registration YAML.",
    )
    def post(self, request):
        serializer = serializers.MatrixAppserviceSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Compute the effective config (request body + existing Constance
        # values) and validate the whole thing BEFORE writing anything. The
        # previous flow wrote partial values to Constance, then validated;
        # Constance does not participate in Django transactions, so a
        # ValidationError after a write left the admin locked into a
        # half-configured state until they could correct it via the admin panel.
        # Keys that MUST resolve to a non-empty value before tokens are
        # generated.
        required_constance_keys = {
            "homeserver_url": "MATRIX_HOMESERVER_URL",
            "homeserver_domain": "MATRIX_HOMESERVER_DOMAIN",
            "user_registration_secret": "MATRIX_USER_REGISTRATION_SECRET",
        }
        # Optional keys that can be persisted from this endpoint but never
        # gate setup completion.
        optional_constance_keys = {
            "homeserver_public_url": "MATRIX_HOMESERVER_PUBLIC_URL",
        }
        constance_keys = {**required_constance_keys, **optional_constance_keys}
        effective = {
            constance_key: serializer.validated_data.get(request_key)
            or getattr(config, constance_key)
            for request_key, constance_key in constance_keys.items()
        }

        missing = [
            key
            for key, value in effective.items()
            if not value and key in required_constance_keys.values()
        ]
        if missing:
            raise ValidationError(
                {
                    "detail": (
                        f"Matrix prerequisites missing: {', '.join(missing)}. "
                        "Set them in the Settings tab or include them in the "
                        "setup request."
                    )
                }
            )

        sender_localpart = (
            serializer.validated_data.get("sender_localpart")
            or config.MATRIX_APPSERVICE_SENDER_LOCALPART
        )

        # Re-validate the final values that will be interpolated into the
        # registration regex. Values can arrive via Constance writes that
        # bypass this endpoint's serializer (admin panel, prior CLI), so
        # checking them at the point of use blocks namespace-claiming
        # regexes like ".*".
        serializers.validate_sender_localpart(sender_localpart)
        serializers.validate_homeserver_domain(effective["MATRIX_HOMESERVER_DOMAIN"])

        # Generate fresh AS/HS tokens. The dialog warns the admin that
        # re-running Setup rotates tokens and invalidates the previous
        # registration YAML on the homeserver.
        as_token = secrets.token_hex(32)
        hs_token = secrets.token_hex(32)

        with transaction.atomic():
            # Persist any prerequisite fields the caller provided. Each key is
            # only written when the request supplied a non-empty value —
            # pre-configured values are never overwritten via this endpoint.
            for request_key, constance_key in constance_keys.items():
                value = serializer.validated_data.get(request_key)
                if value and not getattr(config, constance_key):
                    setattr(config, constance_key, value)
            setattr(config, "MATRIX_APPSERVICE_AS_TOKEN", as_token)
            setattr(config, "MATRIX_APPSERVICE_HS_TOKEN", hs_token)
            if serializer.validated_data.get("sender_localpart"):
                setattr(config, "MATRIX_APPSERVICE_SENDER_LOCALPART", sender_localpart)

        # registration["url"] must be the base Waldur URL — the homeserver
        # appends MATRIX_APPSERVICE_WEBHOOK_PATH to it when delivering events.
        url = serializer.validated_data.get("url", "").rstrip("/")
        webhook_url = (
            f"{url}{MATRIX_APPSERVICE_WEBHOOK_PATH}"
            if url
            else MATRIX_APPSERVICE_WEBHOOK_PATH
        )

        registration = appservice_registration.build_registration(
            url=url,
            as_token=as_token,
            hs_token=hs_token,
            sender_localpart=sender_localpart,
            homeserver_domain=effective["MATRIX_HOMESERVER_DOMAIN"],
        )
        registration_yaml = yaml.dump(registration, default_flow_style=False)

        # Best-effort bot provisioning, outside the DB transaction (this makes
        # external HTTP calls). Many homeservers auto-claim AS users on first
        # contact, but some require explicit registration before the bot can
        # post — logged-only so the setup response stays a 200 either way.
        bot_provision_status = "skipped"
        try:
            matrix_client.ensure_bot_user_exists()
            bot_provision_status = "ok"
        except Exception as exc:
            logger.warning("Bot autoprovision failed during setup: %s", exc)
            bot_provision_status = f"failed: {exc}"

        response_data = {
            "registration_yaml": registration_yaml,
            "as_token": as_token,
            "hs_token": hs_token,
            "sender_localpart": sender_localpart,
            "webhook_url": webhook_url,
            "bot_provision_status": bot_provision_status,
        }
        return Response(response_data, status=status.HTTP_200_OK)


class MatrixAppserviceStatusView(views.APIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(
        summary="Get Matrix appservice status",
        responses={200: serializers.MatrixAppserviceStatusSerializer},
        description="Returns the current appservice configuration state.",
    )
    def get(self, request):
        sender_localpart = config.MATRIX_APPSERVICE_SENDER_LOCALPART
        homeserver_domain = config.MATRIX_HOMESERVER_DOMAIN
        bot_user_id = (
            f"@{sender_localpart}:{homeserver_domain}"
            if sender_localpart and homeserver_domain
            else ""
        )
        webhook_path = MATRIX_APPSERVICE_WEBHOOK_PATH
        transaction_count = models.MatrixAppserviceTransaction.objects.count()

        response_data = {
            "enabled": bool(
                config.MATRIX_APPSERVICE_AS_TOKEN and config.MATRIX_APPSERVICE_HS_TOKEN
            ),
            "as_token_configured": bool(config.MATRIX_APPSERVICE_AS_TOKEN),
            "hs_token_configured": bool(config.MATRIX_APPSERVICE_HS_TOKEN),
            "sender_localpart": sender_localpart,
            "bot_user_id": bot_user_id,
            "webhook_path": webhook_path,
            "homeserver_url": matrix_client.get_public_homeserver_url(),
            "homeserver_domain": homeserver_domain,
            "transaction_count": transaction_count,
        }
        return Response(response_data, status=status.HTTP_200_OK)


def _latest_spec_version(versions):
    """The newest numbered Matrix spec version in a /versions list, e.g. v1.19.
    Homeservers list them in no guaranteed order, and v1.10 sorts before v1.9
    as text."""
    numbered = [v for v in versions if re.fullmatch(r"v\d+\.\d+", v)]
    if not numbered:
        return versions[-1] if versions else "unknown"
    return max(numbered, key=lambda v: tuple(int(n) for n in v[1:].split(".")))


def _get_json_object(url):
    """GET a JSON object; {} on any failure, as the caller only adds detail."""
    try:
        resp = httpx.get(url, timeout=DIAGNOSTICS_TIMEOUT)
        body = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _homeserver_software(homeserver_url):
    """The homeserver's name and version, e.g. "Tuwunel 1.9.0", or "".

    The standard endpoint belongs to the federation API, which a homeserver
    with federation switched off refuses, so Tuwunel's own one is asked next.
    """
    standard = _get_json_object(f"{homeserver_url}/_matrix/federation/v1/version")
    server = standard.get("server")
    if not isinstance(server, dict) or not server.get("name"):
        server = _get_json_object(f"{homeserver_url}/_tuwunel/server_version")
    if not server.get("name"):
        return ""
    return f"{server['name']} {server.get('version', '')}".strip()


def _bot_rooms(homeserver_url, auth_headers):
    """How many rooms the bot is in, as an addition to the bot check's detail.
    The listing uses the same token as the check, so it gets no row of its
    own."""
    try:
        resp = httpx.get(
            f"{homeserver_url}/_matrix/client/v3/joined_rooms",
            headers=auth_headers,
            timeout=DIAGNOSTICS_TIMEOUT,
        )
    except httpx.HTTPError as e:
        return f"; could not list its rooms ({e})"
    if resp.status_code != 200:
        return f"; could not list its rooms (HTTP {resp.status_code})"
    return f", in {len(resp.json().get('joined_rooms', []))} room(s)"


def _matrix_errcode(response):
    try:
        body = response.json()
    except ValueError:
        # A proxy's error page.
        return ""
    return body.get("errcode", "") if isinstance(body, dict) else ""


def _check_appservice_acts_for_users(user, homeserver_url, auth_headers):
    """Diagnostics: can the appservice act as `user`? Returns (ok, detail).

    Chat sessions, device pruning and room joins all act as the user with the
    AS token, and the homeserver refuses each with M_EXCLUSIVE when the
    appservice's user namespace does not cover them. The bot check cannot see
    that, as the bot is always covered. The ID need not exist: the homeserver
    answers on the namespace alone.
    """
    profile = models.MatrixUserProfile.objects.filter(
        user=user, provisioned=True
    ).first()
    user_id = (
        profile.matrix_user_id
        if profile
        else matrix_client.generate_matrix_user_id(user)
    )

    try:
        resp = httpx.get(
            f"{homeserver_url}/_matrix/client/v3/account/whoami",
            params={"user_id": user_id},
            headers=auth_headers,
            timeout=DIAGNOSTICS_TIMEOUT,
        )
    except httpx.ConnectError:
        return False, "Connection refused"
    except httpx.TimeoutException:
        return False, "Timed out"
    except httpx.HTTPError as e:
        return False, str(e)

    if resp.status_code == 200:
        return True, f"OK — can act as {user_id}"
    if _matrix_errcode(resp) == "M_EXCLUSIVE":
        detail = (
            f"{user_id} is outside the user namespace of the homeserver's "
            "appservice registration, so chat sessions and room joins fail; "
            "register the appservice on the homeserver again with Waldur's "
            "registration"
        )
    else:
        detail = f"HTTP {resp.status_code}: {resp.text[:200]}"

    invited = models.MatrixRoomMember.objects.filter(
        room__state=models.RoomStates.ACTIVE,
        membership_state=models.MembershipStates.INVITED,
    ).count()
    if invited:
        detail += f" ({invited} room member(s) recorded as invited, not joined)"
    return False, detail


def _check_bot_is_homeserver_admin(homeserver_url, auth_headers, bot_user_id):
    """Diagnostics: whether the bot may call the homeserver's admin API, which
    locks accounts and sets passwords. Returns (ok, detail)."""
    encoded_bot_user_id = quote(bot_user_id, safe="")
    try:
        resp = httpx.get(
            f"{homeserver_url}/_synapse/admin/v2/users/{encoded_bot_user_id}",
            headers=auth_headers,
            timeout=DIAGNOSTICS_TIMEOUT,
        )
    except httpx.ConnectError:
        return False, "Connection refused"
    except httpx.TimeoutException:
        return False, "Timed out"
    except httpx.HTTPError as e:
        return False, str(e)

    if resp.status_code == 200:
        return True, f"OK — {bot_user_id} is a homeserver admin"
    if resp.status_code == 403:
        return False, (
            f"{bot_user_id} is not a homeserver admin, so Waldur cannot lock the "
            "Matrix accounts of deactivated and deleted users or replace a "
            "deleted user's password, and users cannot generate Matrix "
            "passwords. On Tuwunel, run "
            f"`!admin users make-user-admin {bot_user_id}` in the admin room."
        )
    if resp.status_code in (404, 405):
        return False, (
            "The homeserver's admin API is not reachable at "
            "MATRIX_HOMESERVER_URL: blocked by a proxy, or not "
            "supported by the homeserver."
        )
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


class MatrixDiagnosticsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(
        summary="Run Matrix connectivity diagnostics",
        responses={200: serializers.MatrixDiagnosticsResponseSerializer},
        description="Performs live connectivity checks against the configured "
        "Matrix homeserver and returns results for each check.",
    )
    def get(self, request):
        checks = []
        homeserver_url = config.MATRIX_HOMESERVER_URL

        # Check 1: Homeserver URL configured
        checks.append(
            {
                "name": "homeserver_configured",
                "label": "Homeserver URL configured",
                "ok": bool(homeserver_url),
                "detail": homeserver_url or "Not set",
            }
        )

        # Check 2: Homeserver domain configured. Distinct from the URL: the
        # domain is the server_name used to build Matrix user IDs (@user:domain)
        # and the appservice regex, so an empty value silently breaks user
        # provisioning even when the URL is reachable.
        homeserver_domain = config.MATRIX_HOMESERVER_DOMAIN
        checks.append(
            {
                "name": "homeserver_domain_configured",
                "label": "Homeserver domain configured",
                "ok": bool(homeserver_domain),
                "detail": homeserver_domain or "Not set",
            }
        )

        # Check 3: Homeserver reachable (/_matrix/client/versions)
        server_reachable = False
        if homeserver_url:
            try:
                resp = httpx.get(
                    f"{homeserver_url}/_matrix/client/versions",
                    timeout=DIAGNOSTICS_TIMEOUT,
                )
                server_reachable = resp.status_code == 200
                if server_reachable:
                    spec = _latest_spec_version(resp.json().get("versions", []))
                    software = _homeserver_software(homeserver_url)
                    detail = (
                        f"OK — {software}, Matrix up to {spec}"
                        if software
                        else f"OK — Matrix up to {spec}"
                    )
                else:
                    detail = f"HTTP {resp.status_code}"
            except httpx.ConnectError:
                detail = f"Connection refused — is the homeserver running at {homeserver_url}?"
            except httpx.TimeoutException:
                detail = "Connection timed out"
            except Exception as e:
                detail = str(e)
        else:
            detail = "Skipped — no homeserver URL"

        checks.append(
            {
                "name": "homeserver_reachable",
                "label": "Homeserver reachable",
                "ok": server_reachable,
                "detail": detail,
            }
        )

        # Check 3b/c: public homeserver URL. The browser uses this URL — when
        # it's a Docker-internal name or otherwise unreachable from outside
        # the backend, the chat drawer fails silently. We surface that here.
        public_url = matrix_client.get_public_homeserver_url()
        public_distinct = (
            bool(config.MATRIX_HOMESERVER_PUBLIC_URL) and public_url != homeserver_url
        )
        checks.append(
            {
                "name": "public_homeserver_configured",
                "label": "Public homeserver URL configured",
                "ok": bool(public_url),
                "detail": (
                    public_url + (" (overrides internal)" if public_distinct else "")
                    if public_url
                    else "Not set"
                ),
            }
        )

        if public_distinct:
            public_reachable = False
            try:
                resp = httpx.get(
                    f"{public_url}/_matrix/client/versions",
                    timeout=DIAGNOSTICS_TIMEOUT,
                )
                public_reachable = resp.status_code == 200
                if public_reachable:
                    detail = f"OK — HTTP {resp.status_code}"
                else:
                    detail = f"HTTP {resp.status_code}"
            except httpx.ConnectError:
                detail = (
                    f"Connection refused — is the homeserver reachable at {public_url}?"
                )
            except httpx.TimeoutException:
                detail = "Connection timed out"
            except Exception as e:
                detail = str(e)
        else:
            public_reachable = server_reachable
            detail = "Same as internal — no separate check"

        checks.append(
            {
                "name": "public_homeserver_reachable",
                "label": "Public homeserver reachable (browser path)",
                "ok": public_reachable,
                "detail": detail,
            }
        )

        # Check 4: AS token configured. Show a SHA-256 fingerprint instead of a
        # token prefix so the diagnostic doesn't narrow an offline brute-force.
        as_token = config.MATRIX_APPSERVICE_AS_TOKEN
        checks.append(
            {
                "name": "as_token_configured",
                "label": "Appservice token (AS) configured",
                "ok": bool(as_token),
                "detail": _token_fingerprint(as_token) if as_token else "Not set",
            }
        )

        # Check 5: HS token configured
        hs_token = config.MATRIX_APPSERVICE_HS_TOKEN
        checks.append(
            {
                "name": "hs_token_configured",
                "label": "Homeserver token (HS) configured",
                "ok": bool(hs_token),
                "detail": _token_fingerprint(hs_token) if hs_token else "Not set",
            }
        )

        # Check 6: Registration secret configured
        reg_secret = config.MATRIX_USER_REGISTRATION_SECRET
        checks.append(
            {
                "name": "registration_secret_configured",
                "label": "Registration secret configured",
                "ok": bool(reg_secret),
                "detail": "Set" if reg_secret else "Not set",
            }
        )

        # Check 7: Bot whoami — verify appservice token works
        bot_ok = False
        bot_user_id = ""
        auth_headers = {"Authorization": f"Bearer {as_token}"} if as_token else {}
        if homeserver_url and as_token:
            try:
                resp = httpx.get(
                    f"{homeserver_url}/_matrix/client/v3/account/whoami",
                    headers=auth_headers,
                    timeout=DIAGNOSTICS_TIMEOUT,
                )
                if resp.status_code == 200:
                    bot_user_id = resp.json().get("user_id", "")
                    bot_ok = True
                    detail = f"OK — authenticated as {bot_user_id}" + _bot_rooms(
                        homeserver_url, auth_headers
                    )
                elif resp.status_code == 403:
                    detail = "403 Forbidden — AS token not recognized by homeserver (check appservice registration)"
                elif resp.status_code == 401:
                    detail = "401 Unauthorized — AS token rejected"
                else:
                    detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.ConnectError:
                detail = "Connection refused"
            except httpx.TimeoutException:
                detail = "Timed out"
            except Exception as e:
                detail = str(e)
        else:
            detail = "Skipped — homeserver URL or AS token not configured"

        checks.append(
            {
                "name": "bot_whoami",
                "label": "Bot authentication (whoami)",
                "ok": bot_ok,
                "detail": detail,
            }
        )

        # Check 7b: the appservice can act for users, not only for the bot.
        if bot_ok:
            users_ok, detail = _check_appservice_acts_for_users(
                request.user, homeserver_url, auth_headers
            )
        else:
            users_ok, detail = False, "Skipped — bot authentication failed"
        checks.append(
            {
                "name": "appservice_user_namespace",
                "label": "Appservice can act for users",
                "ok": users_ok,
                "detail": detail,
            }
        )

        # Check 8a: chat drawer token lifetime. Without access_token_ttl the
        # homeserver gives the drawer tokens that last a week (Tuwunel) or
        # forever, and a leaked one stays valid that long. Measured with a login
        # like the drawer's, as the staff user running diagnostics.
        profile = models.MatrixUserProfile.objects.filter(
            user=request.user, provisioned=True
        ).first()
        lifetime_ok = False
        if not profile:
            # Nothing was measured, so nothing failed.
            lifetime_ok = True
            detail = "Skipped — open the chat once as this user, then run again"
        else:
            try:
                lifetime_ms = matrix_client.probe_web_token_lifetime(
                    profile.matrix_user_id
                )
            except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
                detail = f"Could not measure: {e}"
            else:
                if lifetime_ms is None:
                    detail = (
                        "Chat drawer tokens never expire; set access_token_ttl "
                        "(e.g. 300) on the homeserver"
                    )
                elif lifetime_ms > 3600 * 1000:
                    detail = (
                        f"Chat drawer tokens live {lifetime_ms // 1000} s; set "
                        "access_token_ttl to a few minutes (e.g. 300)"
                    )
                else:
                    lifetime_ok = True
                    detail = f"Chat drawer tokens live {lifetime_ms // 1000} s"
        checks.append(
            {
                "name": "web_token_lifetime",
                "label": "Chat drawer tokens expire",
                "ok": lifetime_ok,
                "detail": detail,
            }
        )

        # Bot admin check: the bot locks the accounts of deactivated users and
        # sets passwords through the homeserver's admin API, which only answers
        # server admins.
        if bot_ok and bot_user_id:
            bot_admin, detail = _check_bot_is_homeserver_admin(
                homeserver_url, auth_headers, bot_user_id
            )
        else:
            bot_admin, detail = False, "Skipped — bot authentication failed"
        checks.append(
            {
                "name": "bot_homeserver_admin",
                "label": "Bot is a homeserver admin",
                "ok": bot_admin,
                "detail": detail,
            }
        )

        # Check 9: the homeserver can call Waldur. Every check above talks from
        # Waldur to the homeserver; this one has the homeserver call back with
        # the hs_token, so it catches a wrong hs_token or an appservice URL the
        # homeserver cannot reach, which otherwise only show as missing events.
        ping_ok = False
        if bot_ok:
            try:
                duration = appservice_registration.ping_appservice(
                    homeserver_url, as_token, timeout=DIAGNOSTICS_TIMEOUT
                )
                ping_ok = True
                detail = f"OK — round trip {duration} ms"
            except Exception as e:
                # A timeout's message names its likely cause: the homeserver
                # holds the ping open while it retries Waldur.
                detail = str(e)
        else:
            detail = "Skipped — bot authentication failed"

        checks.append(
            {
                "name": "appservice_ping",
                "label": "Homeserver can reach Waldur (ping)",
                "ok": ping_ok,
                "detail": detail,
            }
        )

        # Check 8b: LiveKit (RTC) configured. The video-call SFU is advertised
        # by the homeserver's .well-known under org.matrix.msc4143.rtc_foci —
        # the exact source the browser reads. Waldur holds no LiveKit config, so
        # we discover it the same way rather than inventing a parallel setting.
        livekit_service_url = ""
        if homeserver_url:
            try:
                resp = httpx.get(
                    f"{homeserver_url}/.well-known/matrix/client",
                    timeout=DIAGNOSTICS_TIMEOUT,
                )
                if resp.status_code == 200:
                    well_known = resp.json()
                    foci = (
                        well_known.get("org.matrix.msc4143.rtc_foci")
                        or well_known.get("org.matrix.msc4143.rtc_transports")
                        or []
                    )
                    lk_focus = next(
                        (f for f in foci if f.get("type") == "livekit"), None
                    )
                    if lk_focus:
                        livekit_service_url = lk_focus.get("livekit_service_url", "")
                    if livekit_service_url:
                        detail = livekit_service_url
                    elif lk_focus:
                        detail = "LiveKit focus present but no livekit_service_url"
                    else:
                        detail = "No LiveKit focus advertised in .well-known"
                else:
                    detail = f"HTTP {resp.status_code} fetching .well-known"
            except httpx.ConnectError:
                detail = "Connection refused fetching .well-known"
            except httpx.TimeoutException:
                detail = "Timed out fetching .well-known"
            except Exception as e:
                detail = str(e)
        else:
            detail = "Skipped — no homeserver URL"

        checks.append(
            {
                "name": "livekit_configured",
                "label": "LiveKit (RTC) configured",
                "ok": bool(livekit_service_url),
                "detail": detail,
            }
        )

        # Check 10: Room stats
        total_rooms = models.MatrixRoom.objects.count()
        active_rooms = models.MatrixRoom.objects.filter(
            state=models.RoomStates.ACTIVE
        ).count()
        error_rooms = models.MatrixRoom.objects.filter(
            state=models.RoomStates.ERROR
        ).count()
        creating_rooms = models.MatrixRoom.objects.filter(
            state=models.RoomStates.CREATING
        ).count()

        checks.append(
            {
                "name": "room_stats",
                "label": "Room statistics",
                "ok": error_rooms == 0,
                "detail": f"{active_rooms} active, {creating_rooms} creating, "
                f"{error_rooms} errored, {total_rooms} total",
            }
        )

        # Check 11: User profiles, and the room members provisioning left
        # without one, e.g. refused because their generated ID already has an
        # account. Those have no chat, so the check fails; users outside every
        # room have only never opened it.
        total_profiles = models.MatrixUserProfile.objects.count()
        provisioned = models.MatrixUserProfile.objects.filter(provisioned=True).count()
        detail = f"{provisioned} provisioned out of {total_profiles} total"
        unlinked = get_unlinked_room_users()
        unlinked_count = unlinked.count()
        if unlinked_count:
            names = list(
                unlinked.values_list("username", flat=True)[:UNLINKED_USERS_SHOWN]
            )
            more = unlinked_count - len(names)
            detail += (
                f"; {unlinked_count} room member(s) without a Matrix account: "
                + ", ".join(names)
                + (f" and {more} more" if more else "")
                + ". The worker log says why; link an existing account with "
                "`waldur link_matrix_account <user> <matrix id>`"
            )

        checks.append(
            {
                "name": "user_stats",
                "label": "User profiles",
                "ok": not unlinked_count,
                "detail": detail,
            }
        )

        # Everything Waldur posts in a room waits for the bot process, the only
        # holder of the keys to post into an encrypted room.
        bot_running = bot_state.is_bot_running(matrix_client.get_bot_user_id())
        pending = models.MatrixOutboxMessage.objects.filter(
            state=models.OutboxStates.PENDING
        ).count()
        checks.append(
            {
                "name": "bot_running",
                "label": "Matrix bot running",
                "ok": bot_running,
                "detail": (
                    f"{'Running' if bot_running else 'Not running: start the matrix_bot process'}"
                    f"; {pending} message(s) waiting to be posted"
                ),
            }
        )

        all_ok = all(c["ok"] for c in checks)
        return Response(
            {"ok": all_ok, "checks": checks},
            status=status.HTTP_200_OK,
        )


class MatrixReprovisionView(views.APIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(
        summary="Reprovision all active Matrix rooms on a new homeserver",
        request=None,
        responses={202: serializers.MatrixReprovisionResponseSerializer},
        description="Resets all active rooms to 'creating' state and re-queues them "
        "for provisioning. Also resets all user profiles. Only for moving to a new "
        "homeserver: on the same homeserver, every old room stays behind with its "
        "history next to a new, empty one. Staff only.",
    )
    def post(self, request):
        # Reprovisioning resets every active room to 'creating' and re-queues
        # provisioning; with no homeserver attached the tasks no-op and strand
        # all rooms in a state that never resolves.
        if not matrix_client.is_enabled():
            raise ValidationError("Matrix chat is disabled.")

        room_count, user_count = tasks.reprovision_rooms()

        return Response(
            {
                "rooms_reprovisioned": room_count,
                "users_reset": user_count,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class MatrixHistoryExportDownloadView(views.APIView):
    """Stream a Matrix history export file or its media zip.

    Direct FileField URLs from the serializer would be served by the storage
    backend without any auth check — anyone with the URL could download. This
    view enforces the same policy as MatrixHistoryExportViewSet
    and 404s on miss/denied so the route does not leak export existence.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Download a Matrix history export file",
        responses={200: bytes},
        parameters=[
            OpenApiParameter(
                "kind",
                str,
                OpenApiParameter.PATH,
                enum=["export", "media"],
                description="Which artifact to stream.",
            ),
        ],
    )
    def get(self, request, uuid, kind):
        if kind not in ("export", "media"):
            raise Http404
        exports = filter_exports_for_request(
            models.MatrixHistoryExport.objects.all(), request
        )
        try:
            export = exports.get(uuid=uuid)
        except (models.MatrixHistoryExport.DoesNotExist, ValueError):
            raise Http404

        file_field = export.export_file if kind == "export" else export.media_file
        if not file_field:
            raise Http404
        # Force octet-stream: the export is a .json file, and Django would
        # otherwise label it application/json. The SPA downloads via a generic
        # get<Blob>() helper that JSON-parses any application/json response
        # instead of returning a Blob, breaking the download. An as_attachment
        # stream is opaque bytes to the client, so octet-stream is correct.
        return FileResponse(
            file_field.open("rb"),
            as_attachment=True,
            content_type="application/octet-stream",
        )


def _livekit_unreachable_response(exc):
    """Map a LiveKitClientError to a 502, distinguishing rejected credentials.

    A bad API key/secret comes back as an HTTP 401/403 from LiveKit's auth
    layer; reporting that as "unreachable" sends operators chasing networking
    when the fix is a Constance field.
    """
    logger.warning("LiveKit request failed: %s", exc)
    if exc.status_code in (401, 403):
        detail = "LiveKit rejected the admin credentials."
    else:
        detail = "LiveKit is unreachable."
    return Response({"detail": detail}, status=status.HTTP_502_BAD_GATEWAY)


class LiveKitOverviewView(views.APIView):
    """GET /api/admin/matrix/livekit/overview/"""

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(
        summary="Get LiveKit calls overview",
        responses={200: serializers.LiveKitOverviewResponseSerializer},
        description="Point-in-time snapshot of active LiveKit rooms with "
        "participant/publisher totals. Staff only. Returns 503 when LiveKit "
        "credentials are not configured and 502 when LiveKit is unreachable "
        "or rejects the configured credentials.",
    )
    def get(self, request):
        if not livekit_client.is_configured():
            return Response(
                {"detail": "LiveKit is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            rooms = livekit_client.list_rooms()
        except livekit_client.LiveKitClientError as exc:
            return _livekit_unreachable_response(exc)

        totals = {
            "room_count": len(rooms),
            "participant_count": sum(room["num_participants"] for room in rooms),
            "publisher_count": sum(room["num_publishers"] for room in rooms),
        }
        response_data = {
            "rooms": rooms,
            "totals": totals,
            "livekit_url": livekit_client.get_internal_url(),
        }
        serializer = serializers.LiveKitOverviewResponseSerializer(response_data)
        return Response(serializer.data, status=status.HTTP_200_OK)


class LiveKitRoomParticipantsView(views.APIView):
    """GET /api/admin/matrix/livekit/participants/?room=<name>"""

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(
        summary="List participants in a LiveKit room",
        responses={200: serializers.LiveKitParticipantSerializer(many=True)},
        parameters=[
            OpenApiParameter(
                "room",
                str,
                OpenApiParameter.QUERY,
                required=True,
                description="LiveKit room name. A query parameter rather than a "
                "path segment because Element Call room names are base64 and "
                "routinely contain '/'.",
            ),
        ],
        description="Participants and their tracks for a single LiveKit room. "
        "Staff only. An unknown or empty room returns 200 with an empty list. "
        "Returns 503 when LiveKit credentials are not configured and 502 when "
        "LiveKit is unreachable or rejects the configured credentials.",
    )
    def get(self, request):
        room_name = request.query_params.get("room")
        if not room_name:
            return Response(
                {"detail": "The 'room' query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not livekit_client.is_configured():
            return Response(
                {"detail": "LiveKit is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            participants = livekit_client.list_participants(room_name)
        except livekit_client.LiveKitClientError as exc:
            return _livekit_unreachable_response(exc)

        serializer = serializers.LiveKitParticipantSerializer(participants, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class _MatrixError(Exception):
    """A refusal in the Matrix error format lk-jwt-service answers with."""

    def __init__(self, status_code, errcode, error):
        super().__init__(error)
        self.status_code = status_code
        self.errcode = errcode
        self.error = error


def _bad_json(error):
    return _MatrixError(status.HTTP_400_BAD_REQUEST, "M_BAD_JSON", error)


# One answer for every reason a caller may not join, so the response does not
# tell whether a room exists, who is in it, or which devices a user has.
def _forbidden():
    return _MatrixError(
        status.HTTP_403_FORBIDDEN, "M_FORBIDDEN", "You may not join this call."
    )


def _unauthorized():
    return _MatrixError(
        status.HTTP_401_UNAUTHORIZED,
        "M_UNAUTHORIZED",
        "The request could not be authorised.",
    )


def _unavailable():
    return _MatrixError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "M_UNKNOWN",
        "Calls are unavailable right now. Please try again later.",
    )


def _string(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _edge_client_address(request):
    """The client address as the edge proxy saw it.

    DRF's default identity is the whole X-Forwarded-For header, which a client
    can vary to get a fresh bucket wherever a proxy appends to the header it
    sent. The proxy in front of Waldur writes the address it saw last, so the
    rightmost entry is the one a client cannot choose. Without a proxy only a
    request with no X-Forwarded-For falls back to the socket's address, and the
    per-user limit is what bounds a client that sends its own header.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    # Azure Application Gateway writes "address:port", the port being the
    # client's ephemeral one, which would make every connection a new bucket.
    return _strip_port(hops[-1]) if hops else request.META.get("REMOTE_ADDR", "")


class LiveKitClientThrottle(ScopedRateThrottle):
    """Per client address, keyed so that a forged X-Forwarded-For does not
    reset it."""

    def get_ident(self, request):
        return _edge_client_address(request)


class LiveKitUserThrottle(SimpleRateThrottle):
    """Per Matrix user, once their OpenID token checks out, so one user cannot
    create call rooms from many addresses."""

    scope = "matrix_livekit_token_user"

    def __init__(self, matrix_user_id):
        self.matrix_user_id = matrix_user_id
        super().__init__()

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.matrix_user_id}


def _keeps_waldur_room_access(matrix_user_id, room_id):
    """Whether the Matrix user may be in the room as far as Waldur decides:
    always for a room Waldur does not manage, and for one it manages only while
    the room is active and its Waldur user keeps access to it."""
    room = models.MatrixRoom.objects.filter(room_id=room_id).first()
    if room is None:
        return True
    if room.state != models.RoomStates.ACTIVE:
        return False
    profile = (
        models.MatrixUserProfile.objects.filter(matrix_user_id=matrix_user_id)
        .select_related("user")
        .first()
    )
    return profile is not None and models.keeps_room_access(profile.user, room)


class LiveKitTokenView(views.APIView):
    """Base of the call token API that Matrix clients use: lk-jwt-service's
    API, served by Waldur so that it can check room membership.

    Matrix clients find it through the homeserver's ``.well-known`` RTC focus
    (``livekit_service_url``). The caller's OpenID token authenticates them;
    Waldur's own authentication is not used, and cross-origin calls are
    allowed from anywhere, without credentials.
    """

    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)
    throttle_classes = [LiveKitClientThrottle]
    throttle_scope = "matrix_livekit_token"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return add_public_cors_headers(_no_store(response))

    def options(self, request, *args, **kwargs):
        return Response(status=status.HTTP_200_OK)

    def post(self, request):
        if not matrix_client.is_enabled():
            raise Http404
        try:
            return Response(self.issue(request))
        except _MatrixError as e:
            return Response(
                {"errcode": e.errcode, "error": e.error}, status=e.status_code
            )

    def parse(self, data):
        """Return (room_id, slot_id, openid_token, claimed_user_id, device_id,
        identity function)."""
        raise NotImplementedError

    def issue(self, request):
        data = request.data
        if not isinstance(data, dict):
            raise _MatrixError(
                status.HTTP_400_BAD_REQUEST, "M_NOT_JSON", "Error reading request"
            )
        if data.get("delay_id") or data.get("delay_timeout"):
            # Element Call retries without them, and keeps its own delayed
            # leave event alive, which it would not if this were accepted.
            raise _bad_json("Delegation of delayed events is not supported")
        room_id, slot_id, openid_token, claimed_user_id, device_id, identity = (
            self.parse(data)
        )
        access_token = _string(openid_token, "access_token")
        server_name = _string(openid_token, "matrix_server_name")
        if not access_token or not server_name:
            raise _bad_json("Missing OpenID token parameters")
        if slot_id not in livekit_client.CALL_SLOT_IDS:
            raise _bad_json("Unsupported slot_id")

        if not (
            livekit_client.is_configured()
            and livekit_client.get_public_url()
            and matrix_client.is_homeserver_configured()
        ):
            raise _unavailable() from None
        domain = config.MATRIX_HOMESERVER_DOMAIN
        # Only this homeserver's users: rooms are not federated, and only
        # their membership and devices can be checked here.
        if server_name != domain:
            raise _forbidden()

        try:
            matrix_user_id = matrix_client.get_openid_user(access_token)
        except matrix_client.MatrixClientError as e:
            logger.warning("Could not verify a call OpenID token: %s", e)
            raise _unavailable() from None
        if not matrix_user_id:
            raise _unauthorized()
        if claimed_user_id and claimed_user_id != matrix_user_id:
            raise _unauthorized()
        user_throttle = LiveKitUserThrottle(matrix_user_id)
        if not user_throttle.allow_request(request, self):
            raise Throttled(user_throttle.wait())
        if (
            not matrix_user_id.endswith(f":{domain}")
            or matrix_user_id == matrix_client.get_bot_user_id()
        ):
            raise _forbidden()
        # A Waldur user deactivated meanwhile is locked out of Matrix; this
        # covers the window before the lock lands.
        if models.MatrixUserProfile.objects.filter(
            matrix_user_id=matrix_user_id, user__is_active=False
        ).exists():
            raise _forbidden()

        try:
            # Every local user is in the appservice's user namespace, so the
            # appservice can ask as any of them; no other path is needed.
            joined = matrix_client.is_joined(matrix_user_id, room_id)
            own_device = joined and any(
                device.get("device_id") == device_id
                for device in matrix_client.list_devices(matrix_user_id)
            )
        except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
            logger.warning("Could not check call access of %s: %s", matrix_user_id, e)
            raise _unavailable() from None
        # A room Waldur manages also needs the user's standing in Waldur: it
        # must be active, and its Matrix member a Waldur user whom a role (or a
        # staff join) still keeps in it. This holds the call shut in the window
        # before a revoked user is removed from the room on the homeserver, or
        # when that removal failed. Rooms Waldur does not manage (direct
        # messages, rooms made in Element) have no Waldur roles to check, so
        # the homeserver's membership decides. Checked after membership, so a
        # non-member is refused the same way whether or not Waldur knows the
        # room.
        if not (
            joined and own_device and _keeps_waldur_room_access(matrix_user_id, room_id)
        ):
            logger.info(
                "Refused a call token to %s for room %s", matrix_user_id, room_id
            )
            raise _forbidden()

        room_name = livekit_client.call_room_name(room_id, slot_id)
        try:
            livekit_client.create_call_room(room_name)
        except livekit_client.LiveKitClientError as e:
            logger.warning("Could not create the LiveKit room for %s: %s", room_id, e)
            raise _unavailable() from None
        token = livekit_client.mint_call_token(
            room_name, identity(matrix_user_id), matrix_user_id
        )
        logger.info("Issued a call token to %s for room %s", matrix_user_id, room_id)
        return {"url": livekit_client.get_public_url(), "jwt": token}


@extend_schema(exclude=True)
class LiveKitGetTokenView(LiveKitTokenView):
    """POST <base>/get_token: the MatrixRTC (MSC4195) request.

    ``{room_id, slot_id, openid_token, member: {id, claimed_user_id,
    claimed_device_id}}`` -> ``{url, jwt}``
    """

    def parse(self, data):
        room_id = _string(data, "room_id")
        slot_id = _string(data, "slot_id")
        if not room_id or not slot_id:
            raise _bad_json("The request body is missing `room_id` or `slot_id`")
        member = data.get("member")
        member_id = _string(member, "id")
        claimed_user_id = _string(member, "claimed_user_id")
        device_id = _string(member, "claimed_device_id")
        if not member_id or not claimed_user_id or not device_id:
            raise _bad_json(
                "The request body `member` is missing a `id`, "
                "`claimed_user_id` or `claimed_device_id`"
            )
        # The member ID is part of the LiveKit identity, so a free one would
        # let a device take any number of identities, none of them in the
        # room's call membership. Element Call's state-event membership uses
        # "<user>:<device>" (matrix-js-sdk, MembershipManager.makeMyMembership),
        # and the web chat its device ID, so those two are the ones allowed.
        # Matrix 2.0's sticky-event membership uses a random member ID; Element
        # Call falls back to /sfu/get when it is refused.
        if member_id not in (device_id, f"{claimed_user_id}:{device_id}"):
            raise _bad_json("The member `id` must name the claimed device")
        return (
            room_id,
            slot_id,
            data.get("openid_token"),
            claimed_user_id,
            device_id,
            lambda user_id: livekit_client.call_identity(user_id, device_id, member_id),
        )


@extend_schema(exclude=True)
class LiveKitLegacySfuView(LiveKitTokenView):
    """POST <base>/sfu/get: the legacy request older Element Call versions,
    and newer ones as a fallback, send.

    ``{room, openid_token, device_id}`` -> ``{url, jwt}``
    """

    def parse(self, data):
        room_id = _string(data, "room")
        if not room_id:
            raise _bad_json("Missing room parameter")
        device_id = _string(data, "device_id")
        if not device_id:
            raise _bad_json("Missing device_id parameter")
        return (
            room_id,
            livekit_client.ELEMENT_CALL_SLOT_ID,
            data.get("openid_token"),
            "",
            device_id,
            lambda user_id: livekit_client.legacy_call_identity(user_id, device_id),
        )


@extend_schema(exclude=True)
class LiveKitDelegateDelayedLeaveView(LiveKitTokenView):
    """POST <base>/delegate_delayed_leave: handing a delayed leave event
    (MSC4140) over to the service, which restarts it while the member is on
    the SFU and sends it once they leave.

    Waldur does not take delayed leave events over, and answers every request
    with 404 M_NOT_FOUND, the answer by which Matrix clients tell that the
    service does not support it. Element Call probes this endpoint before
    joining and takes any other status as support, after which it schedules
    its leave event an hour ahead and stops restarting it every few seconds
    itself. So the answer is the same whatever the request, and is not
    throttled: a refusal of any other kind would leave a member who drops
    off the call in it for an hour.
    """

    throttle_classes = ()

    def post(self, request):
        return Response(
            {
                "errcode": "M_NOT_FOUND",
                "error": "Delegation of delayed events is not supported",
            },
            status=status.HTTP_404_NOT_FOUND,
        )
