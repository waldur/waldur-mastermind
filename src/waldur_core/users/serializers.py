import re

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core.serializers import GenericRelatedField
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import get_valid_models
from waldur_core.structure.permissions import _get_customer
from waldur_core.users import models
from waldur_core.users.enums import InvitationStateType


class BaseInvitationDetailsSerializer(serializers.HyperlinkedModelSerializer):
    created_by_full_name = serializers.CharField(
        read_only=True, source="created_by.full_name"
    )
    created_by_username = serializers.CharField(
        read_only=True, source="created_by.username"
    )
    created_by_image = serializers.ImageField(read_only=True, source="created_by.image")
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")
    scope_name = serializers.CharField(read_only=True, source="scope.name")
    scope_description = serializers.SerializerMethodField()
    scope_type = serializers.SerializerMethodField()
    customer_uuid = serializers.UUIDField(read_only=True, source="customer.uuid")
    customer_name = serializers.CharField(read_only=True, source="customer.name")
    role_name = serializers.CharField(read_only=True, source="role.name")
    role_description = serializers.CharField(read_only=True, source="role.description")

    class Meta:
        model = models.BaseInvitation
        fields = (
            "scope_uuid",
            "scope_name",
            "scope_description",
            "scope_type",
            "customer_uuid",
            "customer_name",
            "role_name",
            "role_description",
            "created_by_full_name",
            "created_by_username",
            "created_by_image",
        )

    def get_scope_description(self, invitation: models.BaseInvitation) -> str:
        """
        Get the description field from the scope if it exists.
        Returns empty string if scope doesn't have a description field.
        """
        if not invitation.scope:
            return ""
        return getattr(invitation.scope, "description", "")

    def get_scope_type(self, invitation: models.Invitation) -> str | None:
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
        model_class = role.content_type.model_class()
        if model_class and not isinstance(scope, model_class):
            raise serializers.ValidationError(
                "Role and scope should belong to the same content type."
            )
        return attrs

    def create(self, validated_data):
        validated_data["customer"] = _get_customer(validated_data["scope"])
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class GroupInvitationSerializer(BaseInvitationSerializer):
    project_role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True, name__startswith="PROJECT."),
        slug_field="uuid",
        required=False,
        allow_null=True,
    )
    scope_image = serializers.SerializerMethodField()

    def validate_user_email_patterns(self, value):
        models.GroupInvitation.validate_user_email_patterns(value)
        return value

    def validate_project_name_template(self, value):
        """Validate that the template only uses allowed placeholders."""
        if not value:
            return value

        # Extract all placeholders from the template
        placeholders = re.findall(r"\{([^}]+)\}", value)

        # Define allowed placeholders
        allowed_placeholders = {"username", "email", "full_name"}

        # Check for invalid placeholders
        invalid_placeholders = set(placeholders) - allowed_placeholders

        if invalid_placeholders:
            raise serializers.ValidationError(
                f"Invalid placeholders in template: {', '.join(invalid_placeholders)}. "
                f"Allowed placeholders are: {', '.join(sorted(allowed_placeholders))}"
            )

        return value

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_scope_image(self, obj):
        """Return the image URL of the scope (Customer or Project) if available."""
        if hasattr(obj.scope, "image") and obj.scope.image:
            # Return the image URL if it exists
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.scope.image.url)
            return obj.scope.image.url
        return None

    class Meta:
        model = models.GroupInvitation
        fields = BaseInvitationSerializer.Meta.fields + (
            "is_active",
            "is_public",
            "auto_create_project",
            "project_name_template",
            "project_role",
            "user_affiliations",
            "user_email_patterns",
            "scope_image",
        )
        read_only_fields = BaseInvitationSerializer.Meta.read_only_fields + (
            "is_active",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "user-group-invitation-detail",
            },
        }

    def validate(self, attrs):
        # Check public invitation constraints first
        # Only staff can create public invitations
        if attrs.get("is_public", False):
            request = self.context.get("request")
            if not (request and request.user.is_staff):
                raise serializers.ValidationError(
                    {"is_public": "Only staff users can create public invitations."}
                )

        # Public invitations must use auto_create_project logic
        if attrs.get("is_public", False) and not attrs.get(
            "auto_create_project", False
        ):
            raise serializers.ValidationError(
                {
                    "auto_create_project": "Public invitations must have auto_create_project enabled."
                }
            )

        # Public invitations should only use project-level roles
        if attrs.get("is_public", False) and attrs.get("role"):
            role = attrs["role"]
            if not role.name.startswith("PROJECT."):
                raise serializers.ValidationError(
                    {
                        "role": "Public invitations can only use project-level roles, not customer-level roles."
                    }
                )

        # Override base validation to allow PROJECT roles with Customer scopes
        # when auto_create_project is enabled
        role: Role = attrs["role"]
        scope = attrs["scope"]
        model_class = role.content_type.model_class()

        if model_class and not isinstance(scope, model_class):
            # Allow PROJECT roles with Customer scopes when auto_create_project is True
            from waldur_core.structure.models import Customer, Project

            if not (
                attrs.get("auto_create_project", False)
                and model_class == Project
                and isinstance(scope, Customer)
            ):
                raise serializers.ValidationError(
                    "Role and scope should belong to the same content type."
                )

        # Continue with GroupInvitation-specific validation
        attrs = super(BaseInvitationSerializer, self).validate(attrs)

        # Validate project role is actually a project-level role
        if attrs.get("auto_create_project") and attrs.get("project_role"):
            project_role = attrs["project_role"]
            if not project_role.name.startswith("PROJECT."):
                raise serializers.ValidationError(
                    "project_role must be a project-level role"
                )

        return attrs


class InvitationSerializer(BaseInvitationSerializer):
    class Meta:
        model = models.Invitation
        fields = BaseInvitationSerializer.Meta.fields + (
            "full_name",
            "native_name",
            "phone_number",
            "organization",
            "job_title",
            "email",
            "civil_number",
            "state",
            "error_message",
            "extra_invitation_text",
            "execution_state",
            "error_message",
        )
        read_only_fields = BaseInvitationSerializer.Meta.read_only_fields + (
            "state",
            "error_message",
            "execution_state",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "user-invitation-detail",
            },
        }


class VisibleInvitationDetailsSerializer(BaseInvitationDetailsSerializer):
    state = serializers.SerializerMethodField()

    class Meta:
        model = models.Invitation
        fields = BaseInvitationDetailsSerializer.Meta.fields + (
            "email",
            "error_message",
            "execution_state",
            "state",
        )
        read_only_fields = (
            "error_message",
            "execution_state",
        )

    def get_state(self, obj) -> InvitationStateType:
        return obj.state


class PermissionRequestSerializer(serializers.HyperlinkedModelSerializer):
    created_by_full_name = serializers.CharField(
        read_only=True, source="created_by.full_name"
    )
    created_by_username = serializers.CharField(
        read_only=True, source="created_by.username"
    )
    created_by_email = serializers.EmailField(read_only=True, source="created_by.email")
    reviewed_by_full_name = serializers.CharField(
        read_only=True, source="reviewed_by.full_name"
    )
    reviewed_by_username = serializers.CharField(
        read_only=True, source="reviewed_by.username"
    )
    state = serializers.CharField(read_only=True, source="get_state_display")
    scope_uuid = serializers.UUIDField(read_only=True, source="invitation.scope.uuid")
    scope_name = serializers.CharField(read_only=True, source="invitation.scope.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="invitation.customer.uuid"
    )
    customer_name = serializers.CharField(
        read_only=True, source="invitation.customer.name"
    )
    role_name = serializers.CharField(read_only=True, source="invitation.role.name")
    role_description = serializers.CharField(
        read_only=True, source="invitation.role.description"
    )
    project_name_template = serializers.CharField(
        read_only=True, source="invitation.project_name_template"
    )

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
            "created_by_email",
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
            "project_name_template",
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


class TokenSerializer(serializers.Serializer):
    token = serializers.CharField()


class InvitationCheckSerializer(serializers.Serializer):
    email = serializers.EmailField()
    civil_number_required = serializers.BooleanField(required=False)


class SubmitRequestResponseSerializer(serializers.Serializer):
    uuid = serializers.CharField(help_text="UUID of the created permission request")
    scope_name = serializers.CharField(help_text="Name of the invitation scope")
    scope_uuid = serializers.CharField(help_text="UUID of the invitation scope")


class CancelRequestResponseSerializer(serializers.Serializer):
    uuid = serializers.CharField(help_text="UUID of the canceled permission request")
    scope_name = serializers.CharField(help_text="Name of the invitation scope")
    scope_uuid = serializers.CharField(help_text="UUID of the invitation scope")
