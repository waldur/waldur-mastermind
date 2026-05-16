from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from waldur_core.core.models import User
from waldur_core.core.serializers import (
    RestrictedSerializerMixin,
    TranslatedModelSerializerMixin,
)
from waldur_core.core.utils import is_uuid_like
from waldur_core.permissions.enums import TYPE_KEYS, TYPE_MAP, PermissionEnum
from waldur_core.permissions.utils import (
    get_create_permission,
    get_delete_permission,
    get_update_permission,
    has_permission,
    validate_role_grant,
)
from waldur_core.structure.permissions import _get_customer

from . import models


class RoleDetailsSerializer(RestrictedSerializerMixin, TranslatedModelSerializerMixin):
    class Meta:
        model = models.Role
        fields = (
            "uuid",
            "name",
            "description",
            "permissions",
            "is_system_role",
            "is_active",
            "users_count",
            "content_type",
        )
        extra_kwargs = {"is_system_role": {"read_only": True}}

    permissions = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    content_type = serializers.SerializerMethodField()

    def get_permissions(self, role: models.Role) -> list[str]:
        return list(
            models.RolePermission.objects.filter(role=role)
            .order_by("permission")
            .values_list("permission", flat=True)
        )

    def get_users_count(self, role: models.Role) -> int:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not user or user.is_anonymous or not (user.is_staff or user.is_support):
            return None
        return models.UserRole.objects.filter(is_active=True, role=role).count()

    def get_content_type(self, role: models.Role) -> TYPE_KEYS:
        for external_ct_id, (app_label, model) in TYPE_MAP.items():
            if (
                role.content_type.app_label == app_label
                and role.content_type.model == model
            ):
                return external_ct_id


class RoleModifySerializer(RoleDetailsSerializer):
    permissions = serializers.JSONField()
    content_type = serializers.CharField()

    def validate(self, attrs):
        if not self.instance:
            if models.Role.objects.filter(name=attrs["name"]).exists():
                raise ValidationError("Name should be unique.")
        else:
            if (
                models.Role.objects.filter(name=attrs["name"])
                .exclude(id=self.instance.id)
                .exists()
            ):
                raise ValidationError("Name should be unique.")
        if self.instance and self.instance.is_system_role:
            if attrs.get("name") != self.instance.name:
                raise ValidationError("Changing name for system role is not possible.")
        return attrs

    def validate_content_type(self, type_name):
        if type_name not in TYPE_MAP:
            raise ValidationError("Invalid content type.")
        return ContentType.objects.get_by_natural_key(*TYPE_MAP[type_name])

    def validate_permissions(self, permissions):
        invalid = set(permissions) - set(perm.value for perm in PermissionEnum)
        if invalid:
            raise ValidationError(f"Invalid permissions {','.join(invalid)}")
        return permissions

    def create(self, validated_data):
        permissions = validated_data.pop("permissions")
        role = super().create(validated_data)
        for permission in permissions:
            models.RolePermission.objects.create(role=role, permission=permission)
        return role

    def update(self, instance, validated_data):
        current_permissions = set(
            models.RolePermission.objects.filter(role=instance).values_list(
                "permission", flat=True
            )
        )
        new_permissions = set(validated_data.pop("permissions"))
        role = super().update(instance, validated_data)
        models.RolePermission.objects.filter(
            role=role, permission__in=current_permissions - new_permissions
        ).delete()

        for permission in new_permissions - current_permissions:
            models.RolePermission.objects.create(role=role, permission=permission)

        return role


class RoleDescriptionSerializer(TranslatedModelSerializerMixin):
    class Meta:
        model = models.Role
        fields = ("description",)


class RoleAvailabilityDetailsSerializer(serializers.ModelSerializer):
    """Read-only flat view of a RoleAvailability row for the staff admin
    panel — joins role, scope and (where applicable) the OfferingProfile
    that injected the row so staff can audit role-to-scope bindings."""

    role_uuid = serializers.UUIDField(read_only=True, source="role.uuid")
    role_name = serializers.CharField(read_only=True, source="role.name")
    role_content_type = serializers.SerializerMethodField()
    scope_type = serializers.SerializerMethodField()
    scope_uuid = serializers.SerializerMethodField()
    scope_name = serializers.SerializerMethodField()
    is_profile_managed = serializers.SerializerMethodField()
    profile_uuid = serializers.SerializerMethodField()
    profile_name = serializers.SerializerMethodField()

    class Meta:
        model = models.RoleAvailability
        fields = (
            "uuid",
            "role_uuid",
            "role_name",
            "role_content_type",
            "scope_type",
            "scope_uuid",
            "scope_name",
            "is_profile_managed",
            "profile_uuid",
            "profile_name",
        )

    def get_role_content_type(self, obj) -> str | None:
        for external, (app_label, model) in TYPE_MAP.items():
            ct = obj.role.content_type
            if ct and ct.app_label == app_label and ct.model == model:
                return external
        return obj.role.content_type.model if obj.role.content_type else None

    def get_scope_type(self, obj) -> str | None:
        for external, (app_label, model) in TYPE_MAP.items():
            ct = obj.content_type
            if ct.app_label == app_label and ct.model == model:
                return external
        return obj.content_type.model

    def get_scope_uuid(self, obj) -> str | None:
        scope = obj.scope
        return scope.uuid.hex if scope and getattr(scope, "uuid", None) else None

    def get_scope_name(self, obj) -> str | None:
        scope = obj.scope
        return getattr(scope, "name", None) if scope else None

    def _bound_profile(self, obj):
        """Return the OfferingProfile that contributed this availability,
        or None for direct (per-offering API) bindings."""
        offering = obj.scope
        if offering is None:
            return None
        profile = getattr(offering, "profile", None)
        if profile is None:
            return None
        if profile.roles.filter(id=obj.role_id).exists():
            return profile
        return None

    def get_is_profile_managed(self, obj) -> bool:
        return self._bound_profile(obj) is not None

    def get_profile_uuid(self, obj) -> str | None:
        profile = self._bound_profile(obj)
        return profile.uuid.hex if profile else None

    def get_profile_name(self, obj) -> str | None:
        profile = self._bound_profile(obj)
        return profile.name if profile else None


class UserRoleDetailsSerializer(serializers.ModelSerializer):
    role_name = serializers.ReadOnlyField(source="role.name")
    role_uuid = serializers.UUIDField(read_only=True, source="role.uuid")
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    user_email = serializers.ReadOnlyField(source="user.email")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_image = serializers.ImageField(source="user.image", read_only=True)
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_uuid = serializers.UUIDField(read_only=True, source="created_by.uuid")

    class Meta:
        model = models.UserRole
        lookup_field = "uuid"
        fields = (
            "uuid",
            "created",
            "expiration_time",
            "role_name",
            "role_uuid",
            "user_email",
            "user_full_name",
            "user_username",
            "user_uuid",
            "user_image",
            "created_by_full_name",
            "created_by_uuid",
        )


class PermissionSerializer(serializers.ModelSerializer):
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    user_name = serializers.CharField(read_only=True, source="user.full_name")
    user_slug = serializers.CharField(read_only=True, source="user.slug")
    created_by_full_name = serializers.CharField(
        read_only=True, source="created_by.full_name"
    )
    created_by_username = serializers.CharField(
        read_only=True, source="created_by.username"
    )
    role_name = serializers.CharField(read_only=True, source="role.name")
    role_description = serializers.CharField(read_only=True, source="role.description")
    role_uuid = serializers.UUIDField(read_only=True, source="role.uuid")
    scope_type = serializers.SerializerMethodField()
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")
    scope_name = serializers.CharField(read_only=True, source="scope.name")
    customer_uuid = serializers.UUIDField(read_only=True, source="scope.customer.uuid")
    customer_name = serializers.CharField(read_only=True, source="scope.customer.name")
    resource_uuid = serializers.SerializerMethodField()
    project_uuid = serializers.SerializerMethodField()

    class Meta:
        model = models.UserRole
        fields = (
            "user_uuid",
            "user_name",
            "user_slug",
            "created",
            "expiration_time",
            "created_by_full_name",
            "created_by_username",
            "role_name",
            "role_description",
            "role_uuid",
            "scope_type",
            "scope_uuid",
            "scope_name",
            "customer_uuid",
            "customer_name",
            "resource_uuid",
            "project_uuid",
        )

    def get_scope_type(self, obj) -> str | None:
        if obj.scope is None:
            return None
        model_name = obj.scope._meta.model_name
        for key, (app, model) in TYPE_MAP.items():
            if model == model_name:
                return key
        return model_name

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_resource_uuid(self, obj) -> str | None:
        """Parent Resource uuid when the scope is a ResourceProject. Used
        by the frontend to deep-link the user's project-scoped roles back
        to the resource detail page (which hosts the Resource projects
        tab). Null for any other scope type."""
        scope = obj.scope
        if scope is None:
            return None
        if scope._meta.model_name == "resourceproject":
            resource = getattr(scope, "resource", None)
            return resource.uuid.hex if resource is not None else None
        return None

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_project_uuid(self, obj) -> str | None:
        """Project uuid for resource and resource_project scopes.
        Lets the frontend check project-scoped permissions (e.g. PROJECT.MANAGER)
        when deciding whether to enable delete/update actions on resource roles."""
        scope = obj.scope
        if scope is None:
            return None
        model_name = scope._meta.model_name
        if model_name == "resource":
            project = getattr(scope, "project", None)
            return project.uuid.hex if project is not None else None
        if model_name == "resourceproject":
            resource = getattr(scope, "resource", None)
            if resource is not None:
                project = getattr(resource, "project", None)
                return project.uuid.hex if project is not None else None
        return None


class UserRoleMutateSerializer(serializers.Serializer):
    role = serializers.CharField()
    user = serializers.UUIDField()
    expiration_time = serializers.DateTimeField(
        required=False, allow_null=True, input_formats=["%Y-%m-%d", "iso-8601"]
    )

    def validate_role(self, value):
        if is_uuid_like(value):
            field = "uuid"
        else:
            field = "name"
        try:
            return models.Role.objects.get(**{field: value})
        except models.Role.DoesNotExist:
            raise ValidationError("Role is not found.")

    def validate_user(self, value):
        try:
            return User.all_objects.get(uuid=value)
        except User.DoesNotExist:
            raise ValidationError("User is not found.")

    def validate_expiration_time(self, value):
        if value is not None and value < timezone.now():
            raise ValidationError(
                "Expiration time should be greater than current time."
            )
        return value

    def validate(self, data):
        scope = self.context["scope"]
        request = self.context["request"]
        target_user = data["user"]

        customer = _get_customer(scope)
        permission = self.get_permission(scope)

        if getattr(scope, "shared", None) is False:
            raise ValidationError("Offering is not available.")

        if customer.blocked or customer.archived:
            raise ValidationError("Customer is not available.")

        if has_permission(
            request,
            permission,
            customer,
        ):
            return data

        if not has_permission(
            request,
            permission,
            scope,
        ):
            raise PermissionDenied()

        if target_user == request.user and scope != customer:
            raise ValidationError("User can not manage own role.")
        return data


class UserRoleCreateSerializer(UserRoleMutateSerializer):
    def get_permission(self, scope):
        return get_create_permission(scope)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        scope = self.context["scope"]
        request = self.context["request"]
        target_user = attrs["user"]
        role: models.Role = attrs["role"]
        expiration_time = attrs.get("expiration_time")

        if not target_user.is_active and not request.user.is_staff:
            raise ValidationError(
                "Only staff users can assign roles to deactivated users."
            )

        validate_role_grant(scope, target_user, role, expiration_time=expiration_time)

        return attrs


class UserRoleUpdateSerializer(UserRoleMutateSerializer):
    def get_permission(self, scope):
        return get_update_permission(scope)


class UserRoleDeleteSerializer(UserRoleMutateSerializer):
    def get_permission(self, scope):
        return get_delete_permission(scope)


class UserRoleExpirationTimeSerializer(serializers.Serializer):
    expiration_time = serializers.DateTimeField(allow_null=True)
