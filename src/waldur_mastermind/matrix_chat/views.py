import hashlib
import hmac
import logging
import secrets

import httpx
import yaml
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status, views
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.core import permissions as core_permissions
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.models import Customer, Project

from . import filters, matrix_client, models, serializers, tasks

logger = logging.getLogger(__name__)

# Matrix Application Service spec v1 endpoint the homeserver PUTs transactions to.
# The registration YAML's `url:` is the base URL; Synapse appends this path itself.
MATRIX_APPSERVICE_WEBHOOK_PATH = "/_matrix/app/v1/transactions/{txnId}"

# Per-call budget for diagnostics httpx.get(). Set tighter than the cumulative
# 5s/check so a single hung connection can't monopolize the staff's request.
DIAGNOSTICS_TIMEOUT = httpx.Timeout(connect=3.0, read=2.0, write=2.0, pool=2.0)


def _token_fingerprint(token):
    """Return a short SHA-256 fingerprint of a secret token for display."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def _get_accessible_room_ids(user):
    """Get MatrixRoom IDs accessible to the user based on project/customer roles."""
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)

    connected_projects = get_connected_projects(user)
    connected_customers = get_connected_customers(user)

    # Include projects that belong to user's connected customers
    projects_via_customer = Project.objects.filter(
        customer__in=connected_customers
    ).values_list("id", flat=True)

    return models.MatrixRoom.objects.filter(
        Q(content_type=project_ct, object_id__in=connected_projects)
        | Q(content_type=project_ct, object_id__in=projects_via_customer)
        | Q(content_type=customer_ct, object_id__in=connected_customers)
    ).values_list("id", flat=True)


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
        return queryset.filter(id__in=_get_accessible_room_ids(user))

    @extend_schema(
        request=serializers.MatrixRoomCreateSerializer,
        responses={201: serializers.MatrixRoomSerializer},
        summary="Create a Matrix room for a project",
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project = serializer.validated_data["project"]

        # Demo policy: only staff and support provision rooms; owners manage an
        # existing room (sync, export) but do not create or tear them down.
        structure_permissions.is_staff_or_support(request, self)

        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_name=project.name,
            content_type=ct,
            object_id=project.id,
            created_by=request.user,
        )

        room_uuid = str(room.uuid)
        transaction.on_commit(lambda: tasks.create_room.delay(room_uuid))

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
        description="Returns projects where the caller is customer owner "
        "(staff sees all) and no MatrixRoom row exists yet. Existing archived "
        "rooms still block creation, so projects with any room are excluded.",
    )
    @action(detail=False, methods=["get"])
    def eligible_projects(self, request):
        user = request.user
        projects = Project.available_objects.all()

        if not (user.is_staff or user.is_support):
            owned_customer_ids = get_connected_customers(user, CustomerRole.OWNER)
            projects = projects.filter(customer_id__in=owned_customer_ids)

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
        serializer = serializers.MatrixRoomMemberSerializer(page or queryset, many=True)
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

    sync_members_permissions = [structure_permissions.is_owner]

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

    export_history_permissions = [structure_permissions.is_owner]

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

    retry_permissions = [structure_permissions.is_owner]

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

    disable_permissions = [structure_permissions.is_staff_or_support]

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
            # Posted by the bot without attribution: only staff/owners can reactivate.
            transaction.on_commit(
                lambda: tasks.send_room_notification.delay(
                    room_uuid, "Chat room was reactivated"
                )
            )
        output_serializer = serializers.MatrixRoomSerializer(
            room, context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_202_ACCEPTED)

    reactivate_permissions = [structure_permissions.is_owner]

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

    destroy_permissions = [structure_permissions.is_staff_or_support]


class MatrixHistoryExportViewSet(ActionsViewSet):
    queryset = models.MatrixHistoryExport.objects.all().order_by("-created")
    serializer_class = serializers.MatrixHistoryExportSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.MatrixHistoryExportFilter
    lookup_field = "uuid"
    disabled_actions = ["create", "destroy", "update", "partial_update"]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return queryset.none()
        if user.is_staff or user.is_support:
            return queryset
        return queryset.filter(room__id__in=_get_accessible_room_ids(user))


class MatrixCredentialsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    # Per-user rate limit: a malicious authenticated user can otherwise flood
    # the homeserver with auto-provisioning calls. 30/hour fits any legitimate
    # workflow (drawer open, room switch) and bounds the abuse surface.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "matrix_credentials"

    @extend_schema(
        summary="Get Matrix login credentials",
        responses={200: serializers.MatrixCredentialsSerializer},
        description="Returns Matrix login credentials for the authenticated user based on the configured login method.",
    )
    def get(self, request):
        # Don't auto-provision a Matrix account for callers when the integration
        # is disabled. 404 hides the endpoint's existence on flag-off; staff
        # see "Not Found" too, matching the homeport behavior on the flag.
        if not matrix_client.is_enabled():
            raise Http404
        try:
            credentials = matrix_client.get_user_matrix_credentials(request.user)
        except matrix_client.MatrixClientError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # If room_uuid is provided and the user is an active member of that
        # room, include the internal room_id and an access_token for embedded
        # chat (works regardless of configured login method). Role-based access
        # alone — e.g. staff or customer owners who are not project members —
        # grants room management but not the conversation itself.
        room_uuid = request.query_params.get("room_uuid")
        if room_uuid:
            try:
                room = models.MatrixRoom.objects.get(uuid=room_uuid)
            except models.MatrixRoom.DoesNotExist:
                room = None

            is_member = (
                room is not None
                and models.MatrixRoomMember.objects.filter(
                    room=room,
                    user=request.user,
                    membership_state__in=[
                        models.MembershipStates.INVITED,
                        models.MembershipStates.JOINED,
                    ],
                ).exists()
            )

            if is_member:
                try:
                    credentials["access_token"] = (
                        matrix_client.get_access_token_for_user(request.user)
                    )
                except matrix_client.MatrixClientError as e:
                    logger.warning(
                        "Failed to get access token for embedded chat: %s", e
                    )

                credentials["room_id"] = room.room_id
                # join_room_as_self both accepts a pending invite (INVITED → JOINED)
                # and no-ops when already joined; the membership row is the source
                # of truth, so no extra invite call is needed here. Drift recovery
                # belongs in sync_project_members_to_room, not this endpoint.
                if credentials.get("access_token"):
                    try:
                        matrix_client.join_room_as_self(
                            room.room_id, credentials["access_token"]
                        )
                    except matrix_client.MatrixClientError:
                        pass

        return Response(credentials)


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

        # Validate hs_token from Authorization header using a constant-time
        # comparison: a naive `!=` leaks the token byte-by-byte to a network-
        # adjacent attacker.
        hs_token = config.MATRIX_APPSERVICE_HS_TOKEN
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not hs_token or not hmac.compare_digest(provided, hs_token):
            return Response(status=status.HTTP_403_FORBIDDEN)

        # Idempotency check
        events = request.data.get("events", [])
        _, created = models.MatrixAppserviceTransaction.objects.get_or_create(
            txn_id=txn_id,
            defaults={"event_count": len(events)},
        )
        if not created:
            return Response({}, status=status.HTTP_200_OK)

        # Dispatch Celery task
        if events:
            tasks.process_appservice_events.delay(txn_id, events)

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

        homeserver_domain = effective["MATRIX_HOMESERVER_DOMAIN"]
        registration = {
            "id": "waldur",
            "url": url,
            "as_token": as_token,
            "hs_token": hs_token,
            "sender_localpart": sender_localpart,
            "namespaces": {
                "users": [
                    # Claim the bot identity exclusively so it cannot be
                    # registered through normal client signup on the local
                    # homeserver. The bot always lives on
                    # MATRIX_HOMESERVER_DOMAIN, so the regex is scoped.
                    {
                        "exclusive": True,
                        "regex": f"@{sender_localpart}:{homeserver_domain}",
                    },
                    {
                        "exclusive": False,
                        "regex": f"@.*:{homeserver_domain}",
                    },
                ],
                "rooms": [],
                "aliases": [],
            },
        }
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
        server_name = ""
        if homeserver_url:
            try:
                resp = httpx.get(
                    f"{homeserver_url}/_matrix/client/versions",
                    timeout=DIAGNOSTICS_TIMEOUT,
                )
                server_reachable = resp.status_code == 200
                if server_reachable:
                    versions = resp.json().get("versions", [])
                    server_name = resp.json().get("server", {}).get("name", "")
                    detail = f"OK — versions: {', '.join(versions[-3:])}"
                    if server_name:
                        detail += f" (server: {server_name})"
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
                    detail = f"OK — authenticated as {bot_user_id}"
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

        # Check 8: Bot can operate (list joined rooms)
        bot_functional = False
        if bot_ok and bot_user_id:
            try:
                resp = httpx.get(
                    f"{homeserver_url}/_matrix/client/v3/joined_rooms",
                    headers=auth_headers,
                    timeout=DIAGNOSTICS_TIMEOUT,
                )
                if resp.status_code == 200:
                    bot_functional = True
                    rooms = resp.json().get("joined_rooms", [])
                    detail = f"OK — {bot_user_id} is in {len(rooms)} room(s)"
                else:
                    detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.ConnectError:
                detail = "Connection refused"
            except httpx.TimeoutException:
                detail = "Timed out"
            except Exception as e:
                detail = str(e)
        else:
            detail = "Skipped — bot authentication failed"

        checks.append(
            {
                "name": "bot_functional",
                "label": "Bot can operate",
                "ok": bot_functional,
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

        # Check 9: Room stats
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

        # Check 10: User profiles
        total_profiles = models.MatrixUserProfile.objects.count()
        provisioned = models.MatrixUserProfile.objects.filter(provisioned=True).count()

        checks.append(
            {
                "name": "user_stats",
                "label": "User profiles",
                "ok": True,
                "detail": f"{provisioned} provisioned out of {total_profiles} total",
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
        "for provisioning. Also resets all user profiles. Staff only.",
    )
    def post(self, request):
        # Reprovisioning resets every active room to 'creating' and re-queues
        # provisioning; with no homeserver attached the tasks no-op and strand
        # all rooms in a state that never resolves.
        if not matrix_client.is_enabled():
            raise ValidationError("Matrix chat is disabled.")
        room_count = 0
        with transaction.atomic():
            # Lock the rows up front so concurrent disable/retry calls can't
            # race the reprovisioning write-back. The active state filter is
            # re-checked under the lock; rows that have transitioned out are
            # silently skipped.
            locked_rooms = list(
                models.MatrixRoom.objects.select_for_update().filter(
                    state=models.RoomStates.ACTIVE
                )
            )
            for room in locked_rooms:
                try:
                    room.begin_reprovisioning()
                except TransitionNotAllowed:
                    continue
                room.room_id = None
                room.room_alias = ""
                room.save(
                    update_fields=["state", "error_message", "room_id", "room_alias"]
                )
                room_uuid = str(room.uuid)
                transaction.on_commit(
                    lambda uuid=room_uuid: tasks.create_room.delay(uuid)
                )
                room_count += 1

            user_count = models.MatrixUserProfile.objects.filter(
                provisioned=True
            ).update(
                provisioned=False,
                access_token="",
                provisioned_at=None,
            )

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
    view enforces the same room-access policy as MatrixHistoryExportViewSet
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
        try:
            export = models.MatrixHistoryExport.objects.get(uuid=uuid)
        except (models.MatrixHistoryExport.DoesNotExist, ValueError):
            raise Http404

        user = request.user
        if not (user.is_staff or user.is_support):
            if export.room.id not in _get_accessible_room_ids(user):
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
