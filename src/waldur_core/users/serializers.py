import re

from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.configuration import config
from waldur_core.core import features
from waldur_core.core import serializers as core_serializers
from waldur_core.core.models import User, UserDetailsMatchMixin
from waldur_core.core.permissions import has_staff_access
from waldur_core.core.serializers import GenericRelatedField
from waldur_core.core.validators import get_project_name_regex_error
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import (
    RoleChange,
    check_grant_policy,
    get_valid_models,
    validate_only_one_project_manager,
    validate_role_change,
    validate_scope_available,
)
from waldur_core.structure.models import Customer, OrganizationGroup, Project
from waldur_core.structure.permissions import _get_customer
from waldur_core.users import feature_rules, micro_apps_registry, models
from waldur_core.users.enums import InvitationState, MicroAppModes
from waldur_core.users.utils import (
    can_manage_invitation_with,
    get_invitation_duplicates,
)


class BaseInvitationDetailsSerializer(serializers.HyperlinkedModelSerializer):
    created_by_full_name = serializers.CharField(
        read_only=True,
        source="created_by.full_name",
        help_text="Full name of the user who created this invitation",
    )
    created_by_username = serializers.CharField(
        read_only=True,
        source="created_by.username",
        help_text="Username of the user who created this invitation",
    )
    created_by_image = serializers.ImageField(
        read_only=True,
        source="created_by.image",
        help_text="Profile image of the user who created this invitation",
        allow_null=True,
    )
    scope_uuid = serializers.UUIDField(
        read_only=True,
        source="scope.uuid",
        help_text="UUID of the invitation scope (Customer or Project)",
    )
    scope_name = serializers.CharField(
        read_only=True, source="scope.name", help_text="Name of the invitation scope"
    )
    scope_description = serializers.SerializerMethodField(
        help_text="Description of the invitation scope"
    )
    scope_type = serializers.SerializerMethodField(
        help_text="Type of the invitation scope (e.g., 'customer', 'project')"
    )
    customer_uuid = serializers.UUIDField(
        read_only=True,
        source="customer.uuid",
        help_text="UUID of the customer organization",
    )
    customer_name = serializers.CharField(
        read_only=True,
        source="customer.name",
        help_text="Name of the customer organization",
    )
    role_name = serializers.CharField(
        read_only=True,
        source="role.name",
        help_text="Name of the role being granted (e.g., 'PROJECT.ADMIN')",
    )
    role_description = serializers.CharField(
        read_only=True,
        source="role.description",
        help_text="Description of the role being granted",
    )

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


def _enforce_role_available_for_scope(scope, role, project_role=None):
    """Reject invitation roles not usable within the scope's organization.

    Mirrors the grant-time policy (RoleAvailability allow-list + concealment
    deny-list) at invitation create/update time, so an organization's private
    clone or a concealed role cannot be selected for another organization
    (which would otherwise only fail later, at accept time). ``project_role``
    (auto_create_project) is evaluated against the customer, since it will be
    granted on a project created under it.
    """
    if role is not None:
        check_grant_policy(scope, role)
    if project_role is not None:
        check_grant_policy(_get_customer(scope) or scope, project_role)


class BaseInvitationSerializer(BaseInvitationDetailsSerializer):
    scope = GenericRelatedField(
        get_valid_models,
        write_only=True,
        help_text="URL of the scope (Customer or Project) for this invitation",
    )
    role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True),
        slug_field="uuid",
        help_text="UUID of the role to grant to the invited user",
    )

    class Meta:
        model = models.BaseInvitation
        fields = BaseInvitationDetailsSerializer.Meta.fields + (
            "url",
            "uuid",
            "role",
            "scope",
            "created",
        )
        read_only_fields = (
            "url",
            "uuid",
            "created",
        )

    def validate(self, attrs):
        role: Role = attrs["role"]
        scope = attrs["scope"]
        model_class = role.content_type.model_class()
        if model_class and not isinstance(scope, model_class):
            raise serializers.ValidationError(
                "Role and scope should belong to the same content type."
            )

        # Fail here rather than at accept time, so the inviter learns the scope
        # rejects grants before the email goes out.
        validate_scope_available(scope)
        _enforce_role_available_for_scope(scope, role)
        validate_only_one_project_manager(scope, role)
        validate_role_change(
            scope,
            role,
            self.context["request"].user,
            RoleChange.GRANT,
            email=attrs.get("email"),
        )
        return attrs

    def create(self, validated_data):
        validated_data["customer"] = _get_customer(validated_data["scope"])
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class GroupInvitationSerializer(
    core_serializers.UserEmailPatternsValidatorMixin, BaseInvitationSerializer
):
    user_affiliations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    project_role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True, name__startswith="PROJECT."),
        slug_field="uuid",
        required=False,
        allow_null=True,
        help_text="UUID of the project role to grant if auto_create_project is enabled",
    )
    scope_image = serializers.SerializerMethodField(
        help_text="Image URL of the invitation scope (Customer or Project)"
    )

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
            "auto_approve",
            "project_name_template",
            "project_role",
            "user_affiliations",
            "user_email_patterns",
            "user_identity_sources",
            "scope_image",
            "custom_text",
            "allow_multiple_requests",
            "allow_custom_project_details",
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
            if not (request and has_staff_access(request)):
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

        # When auto_create_project is enabled without a dedicated project_role,
        # the fallback `role` must itself be project-scoped — otherwise the
        # generated UserRole on the new project would have a mismatched
        # content_type and confer no permission.
        if attrs.get("auto_create_project") and not attrs.get("project_role"):
            fallback = attrs.get("role")
            if fallback and not fallback.name.startswith("PROJECT."):
                raise serializers.ValidationError(
                    {
                        "project_role": (
                            "project_role is required when auto_create_project is "
                            "enabled and the invitation role is not project-scoped."
                        )
                    }
                )

        if isinstance(scope, Project):
            validate_only_one_project_manager(scope, role)

        _enforce_role_available_for_scope(scope, role, attrs.get("project_role"))
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request and instance.is_public:
            user = request.user
            if not user.is_authenticated or (
                not user.is_staff
                and not user.is_support
                and not can_manage_invitation_with(request, instance.scope)
            ):
                data["created_by_full_name"] = None
                data["created_by_username"] = None
                data["created_by_image"] = None
        return data


class GroupInvitationUpdateSerializer(
    core_serializers.UserEmailPatternsValidatorMixin, serializers.ModelSerializer
):
    user_affiliations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True),
        slug_field="uuid",
        required=False,
        help_text="UUID of the role to grant.",
    )
    project_role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True, name__startswith="PROJECT."),
        slug_field="uuid",
        required=False,
        allow_null=True,
        help_text="UUID of the project role to grant if auto_create_project is enabled",
    )
    scope = GenericRelatedField(
        get_valid_models,
        required=False,
        help_text="URL of the scope (Customer or Project) for this invitation",
    )

    class Meta:
        model = models.GroupInvitation
        fields = (
            "is_public",
            "role",
            "scope",
            "auto_create_project",
            "auto_approve",
            "project_name_template",
            "project_role",
            "user_affiliations",
            "user_email_patterns",
            "user_identity_sources",
            "custom_text",
            "allow_multiple_requests",
            "allow_custom_project_details",
        )

    def validate_project_name_template(self, value):
        if not value:
            return value
        placeholders = re.findall(r"\{([^}]+)\}", value)
        allowed_placeholders = {"username", "email", "full_name"}
        invalid_placeholders = set(placeholders) - allowed_placeholders
        if invalid_placeholders:
            raise serializers.ValidationError(
                f"Invalid placeholders in template: {', '.join(invalid_placeholders)}. "
                f"Allowed placeholders are: {', '.join(sorted(allowed_placeholders))}"
            )
        return value

    def validate(self, attrs):
        invitation = self.instance

        if not invitation.is_active:
            raise serializers.ValidationError(
                _("Only active invitations can be edited.")
            )

        # Merge attrs with existing instance values for full-state validation
        is_public = attrs.get("is_public", invitation.is_public)
        auto_create_project = attrs.get(
            "auto_create_project", invitation.auto_create_project
        )
        role = attrs.get("role", invitation.role)
        scope = attrs.get("scope", invitation.scope)

        # Staff-only check for is_public
        if is_public:
            request = self.context.get("request")
            if not (request and has_staff_access(request)):
                raise serializers.ValidationError(
                    {"is_public": "Only staff users can create public invitations."}
                )

        # Public invitations must use auto_create_project
        if is_public and not auto_create_project:
            raise serializers.ValidationError(
                {
                    "auto_create_project": "Public invitations must have auto_create_project enabled."
                }
            )

        # Public invitations should only use project-level roles
        if is_public and role and not role.name.startswith("PROJECT."):
            raise serializers.ValidationError(
                {
                    "role": "Public invitations can only use project-level roles, not customer-level roles."
                }
            )

        # Role/scope compatibility
        if role:
            model_class = role.content_type.model_class()
            if model_class and not isinstance(scope, model_class):
                if not (
                    auto_create_project
                    and model_class == Project
                    and isinstance(scope, Customer)
                ):
                    raise serializers.ValidationError(
                        "Role and scope should belong to the same content type."
                    )

        # Validate project_role
        project_role = attrs.get("project_role", invitation.project_role)
        if auto_create_project and project_role:
            if not project_role.name.startswith("PROJECT."):
                raise serializers.ValidationError(
                    "project_role must be a project-level role"
                )

        if (attrs.get("role") or attrs.get("scope")) and isinstance(scope, Project):
            validate_only_one_project_manager(scope, role)

        _enforce_role_available_for_scope(scope, role, project_role)
        return attrs

    def update(self, instance, validated_data):
        if "scope" in validated_data:
            validated_data["customer"] = _get_customer(validated_data["scope"])
        return super().update(instance, validated_data)


class InvitationSerializer(BaseInvitationSerializer):
    expires = serializers.DateTimeField(
        source="get_expiration_time",
        read_only=True,
        help_text="Expiration date and time of the invitation",
    )

    # Fields that can be controlled by INVITATION_ALLOWED_FIELDS setting
    CONFIGURABLE_FIELDS = (
        "full_name",
        "native_name",
        "phone_number",
        "organization",
        "job_title",
        "civil_number",
    )

    class Meta:
        model = models.Invitation
        fields = BaseInvitationSerializer.Meta.fields + (
            "expires",
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
            "expires",
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

    def validate(self, attrs):
        attrs = super().validate(attrs)
        scope = attrs["scope"]
        email = attrs["email"]
        duplicates = get_invitation_duplicates(
            scope,
            [{"email": email, "role": attrs["role"]}],
        )
        if duplicates:
            raise serializers.ValidationError(
                {
                    "email": _(
                        "Pending invitation already exists for this email and role."
                    )
                }
            )

        # Validate email against scope's email patterns
        self._validate_email_against_scope_patterns(email, scope)

        return attrs

    @staticmethod
    def _validate_email_against_scope_patterns(email, scope):
        """Check that the invitation email matches the scope's user_email_patterns.

        For projects, also checks parent customer patterns.
        """
        scopes_to_check = [scope]
        if hasattr(scope, "customer") and isinstance(scope.customer, Customer):
            scopes_to_check.append(scope.customer)

        for s in scopes_to_check:
            patterns = getattr(s, "user_email_patterns", None)
            if not patterns:
                continue
            if not any(
                UserDetailsMatchMixin._is_pattern_match(p, email) for p in patterns
            ):
                scope_name = s._meta.verbose_name
                raise serializers.ValidationError(
                    {
                        "email": _(
                            "Email does not match the membership restrictions of the %s."
                        )
                        % scope_name,
                    }
                )

    def get_fields(self):
        """Filter invitation fields based on INVITATION_ALLOWED_FIELDS setting.

        Fields like full_name, organization, etc. are used for email personalization
        and are NOT copied to user profile. This method controls which fields
        are available when creating invitations.
        """
        fields = super().get_fields()

        # Skip filtering during schema generation to ensure full OpenAPI export
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        allowed_fields = set(config.INVITATION_ALLOWED_FIELDS or [])

        for field_name in self.CONFIGURABLE_FIELDS:
            if field_name not in allowed_fields and field_name in fields:
                del fields[field_name]

        return fields


class InvitationUpdateSerializer(serializers.ModelSerializer):
    role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True),
        slug_field="uuid",
        required=False,
        help_text="UUID of the new role to assign. Must be compatible with the invitation scope.",
    )

    class Meta:
        model = models.Invitation
        fields = ("email", "role")

    def validate_role(self, role):
        """Validate that the new role is compatible with the invitation's scope"""
        if role:
            invitation = self.instance
            model_class = role.content_type.model_class()
            if model_class and not isinstance(invitation.scope, model_class):
                raise serializers.ValidationError(
                    "Role and scope should belong to the same content type."
                )
            # Ensure role is within the same scope (same content type)
            if invitation.role.content_type != role.content_type:
                raise serializers.ValidationError(
                    f"Cannot change role type from {invitation.role.content_type} to {role.content_type}. "
                    "Role must remain within the same scope type."
                )
        return role

    def validate(self, attrs):
        """Ensure invitation is in a state that allows editing"""
        invitation = self.instance
        if invitation.state not in [
            InvitationState.PENDING,
            InvitationState.PENDING_PROJECT,
        ]:
            raise serializers.ValidationError("Only pending invitations can be edited.")

        user = self.context["request"].user
        email = attrs.get("email", invitation.email)
        # Editing an invitation is checked like withdrawing its role and
        # offering the new one.
        validate_role_change(
            invitation.scope, invitation.role, user, RoleChange.GRANT, email=email
        )
        new_role = attrs.get("role")
        if new_role:
            validate_only_one_project_manager(invitation.scope, new_role)
            validate_role_change(
                invitation.scope, new_role, user, RoleChange.GRANT, email=email
            )
            _enforce_role_available_for_scope(invitation.scope, new_role)

        return attrs


class InvitationDuplicateCheckItemSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.SlugRelatedField(
        queryset=Role.objects.filter(is_active=True),
        slug_field="uuid",
        help_text="UUID of the role to grant to the invited user",
    )


class InvitationDuplicateCheckSerializer(serializers.Serializer):
    scope = GenericRelatedField(
        get_valid_models,
        help_text="URL of the scope (Customer or Project) for this invitation list",
    )
    invitations = InvitationDuplicateCheckItemSerializer(many=True, allow_empty=True)


class InvitationDuplicateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.UUIDField(format="hex")
    existing_invitation_uuid = serializers.UUIDField(allow_null=True, required=False)


class InvitationExistingRoleSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.UUIDField(
        format="hex", help_text="UUID of the role requested for this email"
    )
    existing_role = serializers.UUIDField(
        format="hex", help_text="UUID of the role the user already holds in the scope"
    )
    existing_role_name = serializers.CharField(
        help_text="Name of the role the user already holds in the scope"
    )
    existing_role_description = serializers.CharField(
        help_text="Human-readable description of the role the user already holds, "
        "for display. Falls back to the role name when the description is blank."
    )
    is_same_role = serializers.BooleanField(
        help_text="Whether the role already held is the one being requested. This "
        "reports what the scope currently holds, not the outcome of a grant: "
        "acceptance is decided per accepting user and also depends on the "
        "INVITATION_DISABLE_MULTIPLE_ROLES and ONLY_ONE_PROJECT_MANAGER settings."
    )


class InvitationDuplicateCheckResponseSerializer(serializers.Serializer):
    duplicates = InvitationDuplicateSerializer(many=True)
    existing_roles = InvitationExistingRoleSerializer(many=True)


class VisibleInvitationDetailsSerializer(BaseInvitationDetailsSerializer):
    state = serializers.SerializerMethodField(
        help_text="Current state of the invitation (e.g., 'pending', 'accepted', 'rejected')"
    )

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

    @extend_schema_field(serializers.ChoiceField(choices=InvitationState.values))
    def get_state(self, obj):
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
        read_only=True, source="reviewed_by.full_name", allow_null=True
    )
    reviewed_by_username = serializers.CharField(
        read_only=True, source="reviewed_by.username", allow_null=True
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
            "project_name",
            "project_description",
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
    token = serializers.CharField(
        help_text="Authentication token for invitation acceptance"
    )


class UserInvitationAcceptSerializer(serializers.Serializer):
    user = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
        help_text="Staff only. Accept the invitation for this user instead of the "
        "caller. The user's email must match the invitation unless "
        "allow_email_mismatch is set. Empty, null or the caller's own uuid "
        "accepts for the caller.",
    )
    allow_email_mismatch = serializers.BooleanField(
        required=False,
        allow_null=True,
        help_text="Only with user. Accept even though the user's email differs "
        "from the invitation's, for a caller that has verified the person's "
        "identity itself. Has no effect while "
        "ENABLE_STRICT_CHECK_ACCEPTING_INVITATION is on. The role-granted event "
        "records the bypass in email_check_bypassed, invitation_uuid and "
        "invitation_email.",
    )


class InvitationCheckSerializer(serializers.Serializer):
    email = serializers.EmailField(
        help_text="Email address to check for existing invitations"
    )
    civil_number_required = serializers.BooleanField(
        required=False, help_text="Whether civil number verification is required"
    )


class SubmitRequestSerializer(serializers.Serializer):
    project_name = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        help_text="Custom project name to use instead of auto-generated one",
    )
    project_description = serializers.CharField(
        max_length=2000,
        required=False,
        allow_blank=True,
        help_text="Custom project description",
    )

    def validate_project_name(self, value):
        # A custom name here becomes the created project's name, so it must
        # honour the same configurable pattern as the main project API. A blank
        # value falls back to the invitation's template and is left untouched.
        error = get_project_name_regex_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value


class SubmitRequestResponseSerializer(serializers.Serializer):
    uuid = serializers.CharField(help_text="UUID of the created permission request")
    scope_name = serializers.CharField(help_text="Name of the invitation scope")
    scope_uuid = serializers.CharField(help_text="UUID of the invitation scope")
    scope_type = serializers.ChoiceField(
        choices=list(TYPE_MAP),
        required=False,
        allow_null=True,
        help_text="Type of the invitation scope (e.g., 'customer', 'project')",
    )
    auto_approved = serializers.BooleanField(
        help_text="Whether the request was automatically approved"
    )
    project_uuid = serializers.CharField(
        required=False,
        allow_null=True,
        help_text=(
            "UUID of the project the user was added to. Present when the "
            "invitation has auto_approve and auto_create_project enabled. "
            "Null otherwise."
        ),
    )
    project_created = serializers.BooleanField(
        required=False,
        allow_null=True,
        help_text=(
            "True if a new project was created for the user; false if an "
            "existing project with the same name was reused. Null when no "
            "project workflow ran."
        ),
    )


class CancelRequestResponseSerializer(serializers.Serializer):
    uuid = serializers.CharField(help_text="UUID of the canceled permission request")
    scope_name = serializers.CharField(help_text="Name of the invitation scope")
    scope_uuid = serializers.CharField(help_text="UUID of the invitation scope")


class _UserSummarySerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()


class _NamedSummarySerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()


class FeatureRuleSerializer(
    core_serializers.UserEmailPatternsValidatorMixin,
    core_serializers.UserClaimsValidatorMixin,
    serializers.HyperlinkedModelSerializer,
):
    users = serializers.SlugRelatedField(
        many=True,
        slug_field="uuid",
        queryset=User.objects.all(),
        required=False,
        help_text="UUIDs of the users the rule lists.",
    )
    customers = serializers.SlugRelatedField(
        many=True,
        slug_field="uuid",
        queryset=Customer.objects.all(),
        required=False,
        help_text="UUIDs of organizations; a member of any of them matches.",
    )
    organization_groups = serializers.SlugRelatedField(
        many=True,
        slug_field="uuid",
        queryset=OrganizationGroup.objects.all(),
        required=False,
        help_text="UUIDs of organization groups; a member of an organization "
        "in any of them matches.",
    )
    roles = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="Role names; the user must hold one of them in any scope.",
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    user_nationalities = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    user_organization_types = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    user_assurance_levels = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    user_claims = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        default=dict,
        help_text='Identity provider claims the user must carry, as {"claim": '
        '["accepted", "values"]}. All claims must match; within one claim any '
        "value matches. A value ending in '*' matches by prefix.",
    )
    is_orphaned = serializers.SerializerMethodField(
        help_text="True when the rule targets a feature that is no longer "
        "user-scoped, or a micro-app without a setting: it then has no effect."
    )
    users_summary = serializers.SerializerMethodField()
    customers_summary = serializers.SerializerMethodField()
    organization_groups_summary = serializers.SerializerMethodField()
    created_by = serializers.SlugRelatedField(
        slug_field="uuid", read_only=True, allow_null=True
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, allow_null=True
    )

    class Meta:
        model = models.FeatureRule
        fields = (
            "url",
            "uuid",
            "feature_key",
            "description",
            "is_active",
            "users",
            "customers",
            "organization_groups",
            "roles",
            "user_affiliations",
            "user_email_patterns",
            "user_identity_sources",
            "user_nationalities",
            "user_organization_types",
            "user_assurance_levels",
            "user_claims",
            "is_orphaned",
            "users_summary",
            "customers_summary",
            "organization_groups_summary",
            "created_by",
            "created_by_username",
            "created",
            "modified",
        )
        read_only_fields = ("uuid", "created", "modified")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "feature-rule-detail"},
        }

    def get_is_orphaned(self, rule) -> bool:
        return feature_rules.is_orphaned_key(rule.feature_key)

    @extend_schema_field(_UserSummarySerializer(many=True))
    def get_users_summary(self, rule):
        return _UserSummarySerializer(rule.users.all(), many=True).data

    @extend_schema_field(_NamedSummarySerializer(many=True))
    def get_customers_summary(self, rule):
        return _NamedSummarySerializer(rule.customers.all(), many=True).data

    @extend_schema_field(_NamedSummarySerializer(many=True))
    def get_organization_groups_summary(self, rule):
        return _NamedSummarySerializer(rule.organization_groups.all(), many=True).data

    def validate_feature_key(self, value):
        if value.startswith(models.MICRO_APP_KEY_PREFIX):
            name = value[len(models.MICRO_APP_KEY_PREFIX) :]
            if not re.match(models.MICRO_APP_NAME_REGEX, name):
                raise serializers.ValidationError(
                    _("Not a valid micro-app name: %s.") % name
                )
            return value
        if not features.is_user_scoped(value):
            raise serializers.ValidationError(
                _(
                    "Only user-scoped feature flags and micro_apps.<name> can be "
                    "targeted; %s is not one."
                )
                % value
            )
        return value

    def validate_roles(self, value):
        value = sorted(set(value or []))
        known = set(Role.objects.filter(name__in=value).values_list("name", flat=True))
        unknown = [name for name in value if name not in known]
        if unknown:
            raise serializers.ValidationError(
                _("Unknown roles: %s.") % ", ".join(unknown)
            )
        return value

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class FeatureRuleTestMatchRequestSerializer(serializers.Serializer):
    user_uuid = serializers.UUIDField(
        help_text="UUID of the user to evaluate this rule against."
    )


class FeatureRuleCheckSerializer(serializers.Serializer):
    """Outcome of one criterion of a rule for one user."""

    name = serializers.CharField()
    configured = serializers.BooleanField()
    matched = serializers.BooleanField()
    user_value = serializers.JSONField(allow_null=True, required=False)
    rule_value = serializers.JSONField(allow_null=True, required=False)
    reason = serializers.CharField(allow_blank=True)


class FeatureRuleTestMatchResponseSerializer(serializers.Serializer):
    matched = serializers.BooleanField(
        help_text="Whether the rule matches the user, active or not."
    )
    is_active = serializers.BooleanField()
    enabled_globally = serializers.BooleanField(
        help_text="Whether the feature is already on for everyone, "
        "so the rule changes nothing."
    )
    user_uuid = serializers.UUIDField()
    user_username = serializers.CharField()
    filters = FeatureRuleCheckSerializer(many=True)


class FeatureRuleMatchedUserSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()
    email = serializers.CharField(allow_blank=True)


class FeatureRuleMatchedUsersResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(
        help_text="Active users the rule matches, among those checked."
    )
    checked = serializers.IntegerField(help_text="Active users checked.")
    complete = serializers.BooleanField(
        help_text="False when the check stopped at its limit, so count is a "
        "lower bound."
    )
    users = FeatureRuleMatchedUserSerializer(
        many=True, help_text="The first matched users."
    )


class MicroAppSettingSerializer(serializers.HyperlinkedModelSerializer):
    feature_key = serializers.CharField(
        read_only=True,
        help_text="The feature_key of the rules that pick this app's audience.",
    )
    rules_count = serializers.SerializerMethodField()

    class Meta:
        model = models.MicroAppSetting
        fields = (
            "url",
            "uuid",
            "name",
            "mode",
            "feature_key",
            "rules_count",
            "created",
            "modified",
        )
        read_only_fields = ("uuid", "created", "modified")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "micro-app-setting-detail"},
        }

    def get_rules_count(self, setting) -> int:
        return models.FeatureRule.objects.filter(
            feature_key=setting.feature_key
        ).count()

    def validate_name(self, value):
        if self.instance:
            if self.instance.name != value:
                raise serializers.ValidationError(
                    _("The name of a micro-app setting cannot be changed.")
                )
            return value
        deployed = micro_apps_registry.deployed_micro_app_names()
        # Unknown when the portal's registry cannot be read: then any name is
        # taken, as the admin page still marks one it lacks as not deployed.
        if deployed is not None and value not in deployed:
            raise serializers.ValidationError(
                _("This portal has no micro-app named %s.") % value
            )
        return value


class UserFeatureStateSerializer(serializers.Serializer):
    key = serializers.CharField()
    scope = serializers.ChoiceField(choices=features.SCOPES)
    enabled = serializers.BooleanField()
    source = serializers.ChoiceField(
        choices=("global", "rule", "off"),
        help_text="'global': on for everyone. 'rule': on for this user through "
        "the listed rules. 'off': off for this user.",
    )
    rules = serializers.ListField(child=serializers.UUIDField())


class UserMicroAppStateSerializer(serializers.Serializer):
    name = serializers.CharField()
    mode = serializers.ChoiceField(choices=MicroAppModes.choices)
    enabled = serializers.BooleanField()
    rules = serializers.ListField(child=serializers.UUIDField())


class UserFeaturesResponseSerializer(serializers.Serializer):
    features = UserFeatureStateSerializer(many=True)
    micro_apps = UserMicroAppStateSerializer(
        many=True,
        help_text="Apps with an operator setting; the rest follow "
        "user.show_micro_apps.",
    )
