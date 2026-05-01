from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.permissions import models as permissions_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import models, utils


class OfferingKeycloakGroupSerializer(serializers.HyperlinkedModelSerializer):
    offering_uuid = serializers.ReadOnlyField(source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    role_name = serializers.ReadOnlyField(source="role.name")
    resource_uuid = serializers.ReadOnlyField(source="resource.uuid")
    resource_name = serializers.SerializerMethodField()

    class Meta:
        model = models.OfferingKeycloakGroup
        fields = (
            "uuid",
            "url",
            "name",
            "backend_id",
            "offering",
            "offering_uuid",
            "offering_name",
            "role",
            "role_name",
            "resource",
            "resource_uuid",
            "resource_name",
            "scope_id",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "url",
            "name",
            "backend_id",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "offering-keycloak-group-detail",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
            "role": {
                "lookup_field": "uuid",
                "view_name": "role-detail",
            },
            "resource": {
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
        }

    def get_resource_name(self, obj) -> str | None:
        if obj.resource:
            return obj.resource.name
        return None


class OfferingUUIDSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField()


class TestConnectionResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    groups_count = serializers.IntegerField()
    groups = serializers.ListField(child=serializers.CharField())


class RemoteGroupSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    path = serializers.CharField()
    sub_group_count = serializers.IntegerField()


class RemoteGroupMembersRequestSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField()
    group_id = serializers.CharField()


class RemoteGroupMemberSerializer(serializers.Serializer):
    id = serializers.CharField()
    username = serializers.CharField()
    email = serializers.CharField(allow_blank=True)
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)


class SearchRemoteUsersRequestSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField()
    q = serializers.CharField()


class SyncedGroupSerializer(serializers.Serializer):
    local_name = serializers.CharField()
    remote_name = serializers.CharField()
    backend_id = serializers.CharField()


class SyncStatusResponseSerializer(serializers.Serializer):
    local_only = serializers.ListField(child=serializers.CharField())
    remote_only = serializers.ListField(child=serializers.CharField())
    synced = SyncedGroupSerializer(many=True)


class SetBackendIdSerializer(serializers.Serializer):
    backend_id = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    resource_uuid = serializers.UUIDField(required=False, allow_null=True)
    scope_id = serializers.CharField(required=False, allow_blank=True, default="")


class ImportRemoteGroupSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField()
    role_uuid = serializers.UUIDField()
    remote_group_id = serializers.CharField()
    resource_uuid = serializers.UUIDField(required=False, allow_null=True)
    scope_id = serializers.CharField(required=False, allow_blank=True, default="")


class PullMembersResponseSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    total_remote = serializers.IntegerField()


class OfferingKeycloakMembershipSerializer(serializers.HyperlinkedModelSerializer):
    # Write-only fields for creating membership
    offering = serializers.HyperlinkedRelatedField(
        view_name="marketplace-provider-offering-detail",
        lookup_field="uuid",
        queryset=marketplace_models.Offering.objects.all(),
        write_only=True,
    )
    role = serializers.HyperlinkedRelatedField(
        view_name="role-detail",
        lookup_field="uuid",
        queryset=permissions_models.Role.objects.all(),
        write_only=True,
    )
    resource = serializers.HyperlinkedRelatedField(
        view_name="marketplace-resource-detail",
        lookup_field="uuid",
        queryset=marketplace_models.Resource.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
    )
    scope_id = serializers.CharField(
        required=False, allow_blank=True, default="", write_only=True
    )

    # Read-only fields
    group = serializers.HyperlinkedRelatedField(
        view_name="offering-keycloak-group-detail",
        lookup_field="uuid",
        read_only=True,
    )
    group_name = serializers.CharField(source="group.name", read_only=True)
    group_role_name = serializers.CharField(source="group.role.name", read_only=True)
    group_offering_uuid = serializers.ReadOnlyField(source="group.offering.uuid")
    group_offering_name = serializers.ReadOnlyField(source="group.offering.name")
    group_resource_name = serializers.ReadOnlyField(
        source="group.resource.name", default=None
    )
    group_resource_uuid = serializers.ReadOnlyField(
        source="group.resource.uuid", default=None
    )
    group_scope_id = serializers.ReadOnlyField(source="group.scope_id", default=None)

    class Meta:
        model = models.OfferingKeycloakMembership
        fields = (
            "uuid",
            "url",
            "username",
            "email",
            "first_name",
            "last_name",
            "group",
            "group_name",
            "group_role_name",
            "group_offering_uuid",
            "group_offering_name",
            "group_resource_name",
            "group_resource_uuid",
            "group_scope_id",
            "offering",
            "role",
            "resource",
            "scope_id",
            "user",
            "state",
            "created",
            "modified",
            "last_checked",
            "error_message",
            "error_traceback",
        )
        read_only_fields = (
            "uuid",
            "first_name",
            "last_name",
            "state",
            "created",
            "modified",
            "last_checked",
            "error_message",
            "error_traceback",
            "group_resource_name",
            "group_resource_uuid",
            "group_scope_id",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "offering-keycloak-membership-detail",
            },
            "user": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
                "required": False,
                "allow_null": True,
            },
        }

    def get_fields(self):
        fields = super().get_fields()
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields
        request = self.context.get("request")
        if request and not request.user.is_staff:
            fields.pop("error_traceback", None)
        return fields

    def validate(self, attrs):
        offering = attrs.get("offering")
        role = attrs.get("role")
        resource = attrs.get("resource")

        if not utils.is_keycloak_enabled(offering):
            raise serializers.ValidationError(
                _("Keycloak integration is not enabled for this offering.")
            )

        if resource and resource.offering_id != offering.id:
            raise serializers.ValidationError(
                _("The resource does not belong to the specified offering.")
            )

        # Validate role is available for this offering (via RoleAvailability)
        if role and role.availability.exists():
            offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
            if not role.availability.filter(
                content_type=offering_ct, object_id=offering.id
            ).exists():
                raise serializers.ValidationError(
                    _("The role is not available for this offering.")
                )

        scope_id = attrs.get("scope_id", "")

        # Check for existing membership with same username and role+offering+resource+scope_id
        existing = models.OfferingKeycloakMembership.objects.filter(
            username=attrs["username"],
            group__offering=offering,
            group__role=role,
            group__resource=resource,
            group__scope_id=scope_id,
        )
        if existing.exists():
            raise serializers.ValidationError(
                _("This keycloak membership already exists.")
            )

        return attrs

    def create(self, validated_data):
        offering = validated_data.pop("offering")
        role = validated_data.pop("role")
        resource = validated_data.pop("resource", None)
        scope_id = validated_data.pop("scope_id", "")

        # Get or create the keycloak group
        group, _ = models.OfferingKeycloakGroup.objects.get_or_create(
            offering=offering,
            role=role,
            resource=resource,
            scope_id=scope_id,
            defaults={
                "name": utils.get_keycloak_group_name(
                    offering, role, resource, scope_id
                ),
            },
        )
        validated_data["group"] = group
        return super().create(validated_data)
