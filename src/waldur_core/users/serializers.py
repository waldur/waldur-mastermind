from django.contrib.auth import get_user_model
from rest_framework import serializers

from waldur_core.core.serializers import GenericRelatedField
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import get_valid_models
from waldur_core.structure.permissions import _get_customer
from waldur_core.users import models

User = get_user_model()


class BaseInvitationDetailsSerializer(serializers.HyperlinkedModelSerializer):
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_username = serializers.ReadOnlyField(source="created_by.username")
    scope_uuid = serializers.ReadOnlyField(source="scope.uuid")
    scope_name = serializers.ReadOnlyField(source="scope.name")
    scope_type = serializers.SerializerMethodField()
    customer_uuid = serializers.ReadOnlyField(source="customer.uuid")
    customer_name = serializers.ReadOnlyField(source="customer.name")
    role_description = serializers.ReadOnlyField(source="role.description")

    class Meta:
        model = models.BaseInvitation
        fields = (
            "scope_uuid",
            "scope_name",
            "scope_type",
            "customer_uuid",
            "customer_name",
            "role_description",
            "created_by_full_name",
            "created_by_username",
        )

    def get_scope_type(self, invitation: models.Invitation):
        if not invitation.content_type:
            return
        for name, (app_label, model_name) in TYPE_MAP.items():
            ctype = invitation.content_type
            if ctype.model != model_name:
                continue
            if ctype.app_label != app_label:
                continue
            return name


class BaseInvitationSerializer(BaseInvitationDetailsSerializer):
    scope = GenericRelatedField(get_valid_models, write_only=True)
    role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True), slug_field="uuid"
    )
    expires = serializers.DateTimeField(source="get_expiration_time", read_only=True)

    class Meta:
        model = models.BaseInvitation
        fields = BaseInvitationDetailsSerializer.Meta.fields + (
            "url",
            "uuid",
            "role",
            "scope",
            "created",
            "expires",
        )
        read_only_fields = (
            "url",
            "uuid",
            "created",
            "expires",
        )

    def validate(self, attrs):
        role: Role = attrs["role"]
        scope = attrs["scope"]
        if not isinstance(scope, role.content_type.model_class()):
            raise serializers.ValidationError(
                "Role and scope should belong to the same content type."
            )
        return attrs

    def create(self, validated_data):
        validated_data["customer"] = _get_customer(validated_data["scope"])
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class GroupInvitationSerializer(BaseInvitationSerializer):
    class Meta:
        model = models.GroupInvitation
        fields = BaseInvitationSerializer.Meta.fields + ("is_active",)
        read_only_fields = BaseInvitationSerializer.Meta.read_only_fields + (
            "is_active",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "user-group-invitation-detail",
            },
        }


class InvitationSerializer(BaseInvitationSerializer):
    class Meta:
        model = models.Invitation
        fields = BaseInvitationSerializer.Meta.fields + (
            "full_name",
            "native_name",
            "tax_number",
            "phone_number",
            "organization",
            "job_title",
            "email",
            "civil_number",
            "state",
            "error_message",
            "extra_invitation_text",
        )
        read_only_fields = BaseInvitationSerializer.Meta.read_only_fields + (
            "state",
            "error_message",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "user-invitation-detail",
            },
        }


class PendingInvitationDetailsSerializer(BaseInvitationDetailsSerializer):
    class Meta:
        model = models.Invitation
        fields = BaseInvitationDetailsSerializer.Meta.fields + ("email",)


class PermissionRequestSerializer(serializers.HyperlinkedModelSerializer):
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_username = serializers.ReadOnlyField(source="created_by.username")
    reviewed_by_full_name = serializers.ReadOnlyField(source="reviewed_by.full_name")
    reviewed_by_username = serializers.ReadOnlyField(source="reviewed_by.username")
    state = serializers.ReadOnlyField(source="get_state_display")
    scope_uuid = serializers.ReadOnlyField(source="invitation.scope.uuid")
    scope_name = serializers.ReadOnlyField(source="invitation.scope.name")
    customer_uuid = serializers.ReadOnlyField(source="invitation.customer.uuid")
    customer_name = serializers.ReadOnlyField(source="invitation.customer.name")
    role_name = serializers.ReadOnlyField(source="invitation.role.name")
    role_description = serializers.ReadOnlyField(source="invitation.role.description")

    class Meta:
        model = models.PermissionRequest
        fields = (
            "url",
            "uuid",
            "invitation",
            "state",
            "created",
            "created_by_full_name",
            "created_by_username",
            "reviewed_by_full_name",
            "reviewed_by_username",
            "reviewed_at",
            "review_comment",
            "scope_uuid",
            "scope_name",
            "customer_uuid",
            "customer_name",
            "role_name",
            "role_description",
        )

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "user-permission-request-detail",
            },
            "invitation": {
                "lookup_field": "uuid",
                "view_name": "user-group-invitation-detail",
            },
        }
