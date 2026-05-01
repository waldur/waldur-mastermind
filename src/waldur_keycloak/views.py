import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from keycloak import exceptions as keycloak_exceptions
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_mastermind.marketplace import models as marketplace_models

from . import filters, models, serializers, signals, utils

logger = logging.getLogger(__name__)


def _get_offering_and_check_permission(request, permission, *, source=None):
    """Resolve offering from offering_uuid and check permission.

    Args:
        source: Where to read offering_uuid from.
            "query" — only query params (use for GET endpoints).
            "body"  — only request body (use for POST endpoints).
            None    — auto-detect from request method.
    """
    if source is None:
        source = "query" if request.method == "GET" else "body"

    if source == "query":
        offering_uuid = request.query_params.get("offering_uuid")
    else:
        offering_uuid = request.data.get("offering_uuid")

    if not offering_uuid:
        raise ValidationError({"offering_uuid": "This field is required."})

    try:
        offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
    except marketplace_models.Offering.DoesNotExist:
        raise ValidationError({"offering_uuid": "Offering not found."})

    if not utils.is_keycloak_enabled(offering):
        raise ValidationError("Keycloak integration is not enabled for this offering.")

    if not has_permission(request, permission, offering.customer):
        raise PermissionDenied()

    return offering


class OfferingKeycloakGroupViewSet(core_views.ActionsViewSet):
    queryset = (
        models.OfferingKeycloakGroup.objects.select_related(
            "offering", "role", "resource"
        )
        .all()
        .order_by("-created")
    )
    serializer_class = serializers.OfferingKeycloakGroupSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingKeycloakGroupFilter
    lookup_field = "uuid"
    disabled_actions = ["create", "update", "partial_update"]
    destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_RESOURCE_USERS,
            ["offering.customer"],
        )
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if not user.is_staff:
            visible_offerings = (
                marketplace_models.Offering.objects.all().filter_for_user(user)
            )
            queryset = queryset.filter(offering__in=visible_offerings)

        return queryset

    @extend_schema(
        summary="Test Keycloak connection for an offering",
        request=serializers.OfferingUUIDSerializer,
        responses={200: serializers.TestConnectionResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def test_connection(self, request):
        offering = _get_offering_and_check_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS
        )
        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            groups = keycloak.list_groups()
            group_names = [g.get("name", "") for g in groups]
            return Response(
                {
                    "status": "ok",
                    "groups_count": len(groups),
                    "groups": group_names,
                },
                status=status.HTTP_200_OK,
            )
        except (keycloak_exceptions.KeycloakError, ValueError):
            logger.exception(
                "Keycloak connection test failed for offering %s", offering.uuid
            )
            return Response(
                {"status": "error", "error": "Unable to connect to Keycloak."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    test_connection_serializer_class = serializers.OfferingUUIDSerializer

    @extend_schema(
        summary="List remote Keycloak groups for an offering",
        parameters=[
            OpenApiParameter(
                "offering_uuid",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the offering",
            ),
        ],
        responses={200: serializers.RemoteGroupSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def remote_groups(self, request):
        offering = _get_offering_and_check_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS
        )
        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            groups = utils.get_offering_groups_from_remote(keycloak, offering)
            result = [
                {
                    "id": g["id"],
                    "name": g["name"],
                    "path": g.get("path", ""),
                    "sub_group_count": len(g.get("subGroups", [])),
                }
                for g in groups
            ]
            return Response(result, status=status.HTTP_200_OK)
        except (keycloak_exceptions.KeycloakError, ValueError):
            logger.exception(
                "Failed to list remote groups for offering %s", offering.uuid
            )
            return Response(
                {"error": "Unable to list remote Keycloak groups."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        summary="List members of a remote Keycloak group",
        parameters=[
            OpenApiParameter(
                "offering_uuid",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the offering",
            ),
            OpenApiParameter(
                "group_id",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Keycloak group ID",
            ),
        ],
        responses={200: serializers.RemoteGroupMemberSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def remote_group_members(self, request):
        offering = _get_offering_and_check_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS
        )
        group_id = request.query_params.get("group_id")
        if not group_id:
            raise ValidationError({"group_id": "This field is required."})

        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            members = keycloak.list_group_members(group_id)
            result = [
                {
                    "id": m["id"],
                    "username": m.get("username", ""),
                    "email": m.get("email", ""),
                    "first_name": m.get("firstName", ""),
                    "last_name": m.get("lastName", ""),
                }
                for m in members
            ]
            return Response(result, status=status.HTTP_200_OK)
        except (keycloak_exceptions.KeycloakError, ValueError):
            logger.exception(
                "Failed to list group members for offering %s", offering.uuid
            )
            return Response(
                {"error": "Unable to list remote group members."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        summary="Search for users in remote Keycloak instance",
        parameters=[
            OpenApiParameter(
                "offering_uuid",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the offering",
            ),
            OpenApiParameter(
                "q",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Search query for username, email, or name",
            ),
        ],
        responses={200: serializers.RemoteGroupMemberSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def search_remote_users(self, request):
        offering = _get_offering_and_check_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS
        )
        query = request.query_params.get("q", "").strip()
        if not query:
            raise ValidationError({"q": "This field is required."})

        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            users = keycloak.search_users(query)
            result = [
                {
                    "id": u["id"],
                    "username": u.get("username", ""),
                    "email": u.get("email", ""),
                    "first_name": u.get("firstName", ""),
                    "last_name": u.get("lastName", ""),
                }
                for u in users
            ]
            return Response(result, status=status.HTTP_200_OK)
        except (keycloak_exceptions.KeycloakError, ValueError):
            logger.exception(
                "Failed to search remote users for offering %s", offering.uuid
            )
            return Response(
                {"error": "Unable to search remote users."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        summary="Compare local and remote Keycloak group state",
        parameters=[
            OpenApiParameter(
                "offering_uuid",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the offering",
            ),
        ],
        responses={200: serializers.SyncStatusResponseSerializer},
    )
    @action(detail=False, methods=["get"])
    def sync_status(self, request):
        offering = _get_offering_and_check_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS
        )
        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            remote_groups = utils.get_offering_groups_from_remote(keycloak, offering)

            # Build a map of remote groups belonging to this offering
            remote_map = {g["id"]: g["name"] for g in remote_groups}

            # Build a map of local groups that have a backend_id
            local_groups = models.OfferingKeycloakGroup.objects.filter(
                offering=offering
            )
            local_map = {g.backend_id: g.name for g in local_groups if g.backend_id}
            local_without_backend = [g.name for g in local_groups if not g.backend_id]

            remote_ids = set(remote_map.keys())
            local_ids = set(local_map.keys())

            synced = [
                {
                    "local_name": local_map[bid],
                    "remote_name": remote_map[bid],
                    "backend_id": bid,
                }
                for bid in remote_ids & local_ids
            ]
            remote_only = [remote_map[bid] for bid in remote_ids - local_ids]
            local_only = [local_map[bid] for bid in local_ids - remote_ids]
            local_only.extend(local_without_backend)

            return Response(
                {
                    "local_only": local_only,
                    "remote_only": remote_only,
                    "synced": synced,
                },
                status=status.HTTP_200_OK,
            )
        except (keycloak_exceptions.KeycloakError, ValueError):
            logger.exception("Failed to get sync status for offering %s", offering.uuid)
            return Response(
                {"error": "Unable to retrieve sync status from Keycloak."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        summary="Set or unlink the backend_id (remote Keycloak group ID) for a local group",
        request=serializers.SetBackendIdSerializer,
        responses={200: serializers.OfferingKeycloakGroupSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, uuid=None):
        group = self.get_object()
        if not has_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS, group.offering.customer
        ):
            raise PermissionDenied()

        serializer = serializers.SetBackendIdSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data.get("backend_id") or ""

        if new_backend_id:
            # Validate that the remote group exists
            try:
                keycloak = utils.get_keycloak_client_for_offering(group.offering)
                remote_group = keycloak.get_group(new_backend_id)
                if remote_group is None:
                    raise ValidationError(
                        {"backend_id": "Remote group not found in Keycloak."}
                    )
            except keycloak_exceptions.KeycloakError:
                logger.exception("Failed to verify remote group %s", new_backend_id)
                raise ValidationError(
                    {"backend_id": "Unable to verify remote group in Keycloak."}
                )

        update_fields = ["backend_id"]
        group.backend_id = new_backend_id

        # Optional resource linking
        resource_uuid = serializer.validated_data.get("resource_uuid")
        if resource_uuid is not None:
            try:
                resource = marketplace_models.Resource.objects.get(
                    uuid=resource_uuid, offering=group.offering
                )
            except marketplace_models.Resource.DoesNotExist:
                raise ValidationError(
                    {"resource_uuid": "Resource not found for this offering."}
                )
            group.resource = resource
            update_fields.append("resource")
        elif "resource_uuid" in request.data and request.data["resource_uuid"] is None:
            group.resource = None
            update_fields.append("resource")

        # Optional scope_id
        if "scope_id" in serializer.validated_data:
            group.scope_id = serializer.validated_data["scope_id"]
            update_fields.append("scope_id")

        group.save(update_fields=update_fields)

        return Response(
            serializers.OfferingKeycloakGroupSerializer(
                group, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )

    set_backend_id_serializer_class = serializers.SetBackendIdSerializer

    @extend_schema(
        summary="Import a remote Keycloak group as a local OfferingKeycloakGroup",
        request=serializers.ImportRemoteGroupSerializer,
        responses={201: serializers.OfferingKeycloakGroupSerializer},
    )
    @action(detail=False, methods=["post"])
    def import_remote(self, request):
        serializer = serializers.ImportRemoteGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        offering = _get_offering_and_check_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS
        )

        # Resolve role and validate it's available for this offering
        try:
            role = Role.objects.get(uuid=data["role_uuid"])
        except Role.DoesNotExist:
            raise ValidationError({"role_uuid": "Role not found."})

        if role.availability.exists():
            offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
            if not role.availability.filter(
                content_type=offering_ct, object_id=offering.id
            ).exists():
                raise ValidationError(
                    {"role_uuid": "Role is not available for this offering."}
                )

        # Resolve optional resource
        resource = None
        if data.get("resource_uuid"):
            try:
                resource = marketplace_models.Resource.objects.get(
                    uuid=data["resource_uuid"], offering=offering
                )
            except marketplace_models.Resource.DoesNotExist:
                raise ValidationError(
                    {"resource_uuid": "Resource not found for this offering."}
                )

        # Fetch group info from Keycloak
        remote_group_id = data["remote_group_id"]
        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            remote_group = keycloak.get_group(remote_group_id)
            if remote_group is None:
                raise ValidationError(
                    {"remote_group_id": "Remote group not found in Keycloak."}
                )
            group_name = remote_group.get("name", remote_group_id)
        except keycloak_exceptions.KeycloakError:
            logger.exception(
                "Failed to fetch remote group %s for offering %s",
                remote_group_id,
                offering.uuid,
            )
            raise ValidationError(
                {"remote_group_id": "Unable to fetch remote group from Keycloak."}
            )

        scope_id = data.get("scope_id")

        # Check for duplicate
        if models.OfferingKeycloakGroup.objects.filter(
            offering=offering,
            backend_id=remote_group_id,
        ).exists():
            raise ValidationError(
                "A local group already exists for this remote Keycloak group."
            )

        group = models.OfferingKeycloakGroup.objects.create(
            offering=offering,
            role=role,
            resource=resource,
            scope_id=scope_id,
            name=group_name,
            backend_id=remote_group_id,
        )

        return Response(
            serializers.OfferingKeycloakGroupSerializer(
                group, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    import_remote_serializer_class = serializers.ImportRemoteGroupSerializer

    @extend_schema(
        summary="Pull members from Keycloak for a group",
        request=None,
        responses={200: serializers.PullMembersResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def pull_members(self, request, uuid=None):
        group = self.get_object()
        if not has_permission(
            request, PermissionEnum.MANAGE_RESOURCE_USERS, group.offering.customer
        ):
            raise PermissionDenied()

        if not group.backend_id:
            raise ValidationError(
                "Group is not linked to a remote Keycloak group. "
                "Remap it first to set a backend ID."
            )

        try:
            keycloak = utils.get_keycloak_client_for_offering(group.offering)
            remote_members = keycloak.list_group_members(group.backend_id)
        except keycloak_exceptions.KeycloakError:
            logger.exception("Failed to pull members for group %s", group.uuid)
            raise ValidationError("Unable to fetch members from Keycloak.")

        created_count = 0
        updated_count = 0
        for member in remote_members:
            username = member.get("username", "")
            if not username:
                continue

            membership, created = (
                models.OfferingKeycloakMembership.objects.get_or_create(
                    username=username,
                    group=group,
                    defaults={
                        "email": member.get("email", ""),
                        "first_name": member.get("firstName", ""),
                        "last_name": member.get("lastName", ""),
                    },
                )
            )
            if created:
                membership.activate()
                membership.save(update_fields=["state"])
                created_count += 1
            else:
                changed = False
                for field, keycloak_key in [
                    ("email", "email"),
                    ("first_name", "firstName"),
                    ("last_name", "lastName"),
                ]:
                    new_val = member.get(keycloak_key, "")
                    if new_val and getattr(membership, field) != new_val:
                        setattr(membership, field, new_val)
                        changed = True
                if membership.state == "pending":
                    membership.activate()
                    changed = True
                if changed:
                    membership.save()
                    updated_count += 1

        return Response(
            {
                "created": created_count,
                "updated": updated_count,
                "total_remote": len(remote_members),
            },
            status=status.HTTP_200_OK,
        )

    pull_members_serializer_class = serializers.PullMembersResponseSerializer


class OfferingKeycloakMembershipViewSet(core_views.ActionsViewSet):
    queryset = (
        models.OfferingKeycloakMembership.objects.select_related(
            "group__offering", "group__role", "group__resource", "user"
        )
        .all()
        .order_by("-created")
    )
    serializer_class = serializers.OfferingKeycloakMembershipSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingKeycloakMembershipFilter
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update"]

    destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_RESOURCE_USERS,
            ["group.offering.customer"],
        )
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if not user.is_staff:
            visible_offerings = (
                marketplace_models.Offering.objects.all().filter_for_user(user)
            )
            queryset = queryset.filter(group__offering__in=visible_offerings)

        return queryset

    def perform_create(self, serializer):
        # Check MANAGE_RESOURCE_USERS permission on the offering's customer
        offering = serializer.validated_data.get("offering")
        if offering and not has_permission(
            self.request, PermissionEnum.MANAGE_RESOURCE_USERS, offering.customer
        ):
            raise PermissionDenied()

        with transaction.atomic():
            membership = serializer.save()
            offering = membership.group.offering
            group = membership.group

            # Create group in Keycloak if it has no backend_id yet
            try:
                keycloak = utils.get_keycloak_client_for_offering(offering)

                if not group.backend_id:
                    backend_group = utils.create_keycloak_group_with_hierarchy(
                        keycloak, offering, group.name
                    )
                    group.backend_id = backend_group["id"]
                    group.save(update_fields=["backend_id"])
                    signals.keycloak_group_created.send(
                        sender=models.OfferingKeycloakGroup,
                        group=group,
                        offering=offering,
                        resource=group.resource,
                    )

                # Try to find user in Keycloak and add to group
                backend_user = keycloak.find_user_by_username(membership.username)
                if backend_user is None:
                    logger.info(
                        "The user %s does not exist in Keycloak yet, "
                        "skipping adding user to the group %s (%s)",
                        membership.username,
                        group.name,
                        group.backend_id,
                    )
                else:
                    keycloak.add_user_to_group(backend_user["id"], group.backend_id)
                    membership.first_name = backend_user.get("firstName", "")
                    membership.last_name = backend_user.get("lastName", "")
                    membership.activate()
                    membership.save()

                # Send notification email
                utils.send_membership_notification_email(membership, offering)

            except keycloak_exceptions.KeycloakError:
                logger.exception(
                    "Failed to add user %s to Keycloak group %s",
                    membership.username,
                    group.backend_id,
                )
                raise ValidationError("Unable to add a user to the Keycloak group.")

    def perform_destroy(self, instance):
        instance.delete()
