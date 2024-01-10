from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from waldur_core.core.serializers import TranslatedModelSerializerMixin
from waldur_core.core.utils import is_uuid_like
from waldur_core.media.serializers import ProtectedImageField
from waldur_core.permissions.enums import TYPE_MAP, PermissionEnum
from waldur_core.permissions.utils import get_customer, has_permission, has_user

from . import models

User = get_user_model()


class RoleDetailsSerializer(TranslatedModelSerializerMixin):
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
    content_type = serializers.ReadOnlyField(source="content_type.model")

    def get_permissions(self, role):
        return list(
            models.RolePermission.objects.filter(role=role).values_list(
                "permission", flat=True
            )
        )

    def get_users_count(self, role):
        return models.UserRole.objects.filter(is_active=True, role=role).count()


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
            raise ValidationError(f'Invalid permissions {",".join(invalid)}')
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


class UserRoleDetailsSerializer(serializers.ModelSerializer):
    role_name = serializers.ReadOnlyField(source="role.name")
    role_uuid = serializers.ReadOnlyField(source="role.uuid")
    user_uuid = serializers.ReadOnlyField(source="user.uuid")
    user_email = serializers.ReadOnlyField(source="user.email")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_image = ProtectedImageField(source="user.image")
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_uuid = serializers.ReadOnlyField(source="created_by.uuid")

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


class PermissionSerializer(serializers.Serializer):
    created = serializers.ReadOnlyField()
    expiration_time = serializers.ReadOnlyField()
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_username = serializers.ReadOnlyField(source="created_by.username")
    role_name = serializers.ReadOnlyField(source="role.name")
    role_description = serializers.ReadOnlyField(source="role.description")
    role_uuid = serializers.ReadOnlyField(source="role.uuid")
    scope_type = serializers.ReadOnlyField(source="scope._meta.model_name")
    scope_uuid = serializers.ReadOnlyField(source="scope.uuid")
    scope_name = serializers.ReadOnlyField(source="scope.name")
    customer_uuid = serializers.ReadOnlyField(source="scope.customer.uuid")
    customer_name = serializers.ReadOnlyField(source="scope.customer.name")


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
            return User.objects.get(uuid=value)
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

        customer = get_customer(scope)
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
        model_name = scope._meta.model_name
        if model_name == "customer":
            return PermissionEnum.CREATE_CUSTOMER_PERMISSION
        elif model_name == "project":
            return PermissionEnum.CREATE_PROJECT_PERMISSION
        elif model_name == "offering":
            return PermissionEnum.CREATE_OFFERING_PERMISSION

    def validate(self, attrs):
        attrs = super().validate(attrs)
        scope = self.context["scope"]
        target_user = attrs["user"]
        role: models.Role = attrs["role"]
        expiration_time = attrs.get("expiration_time")

        if has_user(scope, target_user, expiration_time=expiration_time):
            raise ValidationError("User already has permission in this scope.")

        if not isinstance(scope, role.content_type.model_class()):
            raise ValidationError("Role is not valid for this scope.")

        if not role.is_active:
            raise ValidationError("Role is not active.")
        return attrs


class UserRoleUpdateSerializer(UserRoleMutateSerializer):
    def get_permission(self, scope):
        model_name = scope._meta.model_name
        if model_name == "customer":
            return PermissionEnum.UPDATE_CUSTOMER_PERMISSION
        elif model_name == "project":
            return PermissionEnum.UPDATE_PROJECT_PERMISSION
        elif model_name == "offering":
            return PermissionEnum.UPDATE_OFFERING_PERMISSION


class UserRoleDeleteSerializer(UserRoleMutateSerializer):
    def get_permission(self, scope):
        model_name = scope._meta.model_name
        if model_name == "customer":
            return PermissionEnum.DELETE_CUSTOMER_PERMISSION
        elif model_name == "project":
            return PermissionEnum.DELETE_PROJECT_PERMISSION
        elif model_name == "offering":
            return PermissionEnum.DELETE_OFFERING_PERMISSION
