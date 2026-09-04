from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
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
    build_org_role_name,
    ensure_unique_role_name,
    get_create_permission,
    get_delete_permission,
    get_update_permission,
    has_permission,
    validate_role_grant,
)
from waldur_core.structure import models as structure_models
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
            "template_uuid",
            "template_name",
            "customer_uuid",
            "customer_name",
        )
        extra_kwargs = {"is_system_role": {"read_only": True}}

    permissions = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    content_type = serializers.SerializerMethodField()
    template_uuid = serializers.SerializerMethodField()
    template_name = serializers.CharField(
        source="template.name", read_only=True, allow_null=True
    )
    customer_uuid = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()

    def get_template_uuid(self, role: models.Role) -> str | None:
        return role.template.uuid.hex if role.template_id else None

    def _owning_customers(self) -> dict:
        # role_id -> owning Customer, for org-scoped clones. Built once per
        # serializer instance (two queries total) so listing roles stays free of
        # an N+1, since the app bootstraps ENV.roles from this endpoint.
        if hasattr(self, "_owning_customers_map"):
            return self._owning_customers_map
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        availabilities = list(
            models.RoleAvailability.objects.filter(
                content_type=customer_ct
            ).values_list("role_id", "object_id")
        )
        customers_by_id = structure_models.Customer.objects.in_bulk(
            {object_id for _, object_id in availabilities}
        )
        self._owning_customers_map = {
            role_id: customers_by_id[object_id]
            for role_id, object_id in availabilities
            if object_id in customers_by_id
        }
        return self._owning_customers_map

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_customer_uuid(self, role: models.Role) -> str | None:
        customer = self._owning_customers().get(role.id)
        return customer.uuid.hex if customer else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_name(self, role: models.Role) -> str | None:
        customer = self._owning_customers().get(role.id)
        return customer.name if customer else None

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
        # An organization-scoped role's name encodes its owning organization
        # (CUSTOMER.<uuid>.SUFFIX) and its scope binds it to that organization, so
        # neither may change — only the description and permissions are editable.
        if self.instance and self._is_org_scoped(self.instance):
            if attrs.get("name") != self.instance.name:
                raise ValidationError(
                    "Changing name of an organization role is not possible."
                )
            if attrs.get("content_type") != self.instance.content_type:
                raise ValidationError(
                    "Changing scope of an organization role is not possible."
                )
        return attrs

    @staticmethod
    def _is_org_scoped(role: models.Role) -> bool:
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        return role.availability.filter(content_type=customer_ct).exists()

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


def clone_role_for_customer(
    template, customer, description=None, conceal_template=True
):
    """Create an organization-private copy of ``template`` bound to ``customer``.

    The clone keeps ``template``'s content type and permission set (including its
    translated descriptions). Its name is ``{SCOPE}.{customer_slug}.{suffix}`` —
    scope prefix first, so the ``name__startswith="PROJECT."`` filters still see
    project clones, and the organization slug keeps the name human-readable
    (it is re-synced when the slug changes, and a collision suffix guards global
    uniqueness). Availability is bound to the customer so the clone is usable
    only within that organization and its projects.

    When ``conceal_template`` is set, the source system role is also concealed for
    the organization so the clone supersedes it (avoiding two identically-labelled
    entries in the pickers). The freshly-created clone counts as the surviving
    grantable role, so the concealment lockout guard passes.
    """
    if template.content_type.model not in ("customer", "project"):
        raise ValidationError(
            "Only customer and project roles can be cloned into an organization."
        )
    # Access checks resolve clones one level deep (role or role.template), so a
    # chain would silently escape them — and double up the slug in the name.
    if template.template_id is not None:
        raise ValidationError(
            "A clone cannot be cloned. Clone the original role instead."
        )
    customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
    already_cloned = models.Role.objects.filter(
        template=template,
        availability__content_type=customer_ct,
        availability__object_id=customer.id,
    ).exists()
    if already_cloned:
        raise ValidationError(
            "This template has already been cloned into this organization."
        )
    new_name = ensure_unique_role_name(build_org_role_name(template, customer.slug))
    with transaction.atomic():
        role = models.Role(
            name=new_name,
            content_type=template.content_type,
            is_system_role=False,
            description=description or template.description,
            template=template,
        )
        # Preserve the template's per-language descriptions (modeltranslation).
        # Skipped when an explicit override is given (it wins for the base field).
        if not description:
            for lang in settings.LANGUAGE_CHOICES:
                field = f"description_{lang}"
                if hasattr(template, field):
                    setattr(role, field, getattr(template, field))
        try:
            role.save()
        except DjangoValidationError:
            raise ValidationError(
                "This template has already been cloned into this organization."
            )
        for permission in template.permissions.values_list("permission", flat=True):
            role.add_permission(permission)
        models.RoleAvailability.objects.create(
            role=role, content_type=customer_ct, object_id=customer.id
        )
        if conceal_template:
            validate_concealment_allowed(customer, template)
            models.CustomerRoleConcealment.objects.get_or_create(
                role=template, content_type=customer_ct, object_id=customer.id
            )
    return role


class RoleCloneSerializer(serializers.Serializer):
    customer = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Customer.objects.all(),
    )
    description = serializers.CharField(required=False, allow_blank=True)
    conceal_template = serializers.BooleanField(default=True)


def validate_concealment_allowed(customer, role):
    """Lockout guard for concealment.

    Refuse to conceal the last grantable role that can add members at the role's
    own scope (e.g. the last customer OWNER-capable role), which would otherwise
    make the organization ungovernable. A role that does not itself carry the
    scope's create-permission is always safe to conceal.
    """
    create_permission = get_create_permission(role.content_type.model_class())
    if create_permission is None:
        return
    if not role.permissions.filter(permission=create_permission.value).exists():
        return
    customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
    concealed_ids = set(
        models.CustomerRoleConcealment.objects.filter(
            content_type=customer_ct, object_id=customer.id
        ).values_list("role_id", flat=True)
    )
    concealed_ids.add(role.id)
    candidates = (
        models.Role.objects.filter(
            content_type=role.content_type,
            is_active=True,
            permissions__permission=create_permission.value,
        )
        .exclude(id__in=concealed_ids)
        .distinct()
    )
    for candidate in candidates:
        if not candidate.availability.exists():
            return  # a system (globally available) role remains
        if candidate.availability.filter(
            content_type=customer_ct, object_id=customer.id
        ).exists():
            return  # an org-private role remains
    raise ValidationError(
        "Cannot conceal the last role that can grant access for this organization."
    )


class CustomerRoleConcealmentSerializer(serializers.ModelSerializer):
    role = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Role.objects.all()
    )
    customer = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Customer.objects.all(),
        write_only=True,
    )
    role_name = serializers.CharField(read_only=True, source="role.name")
    customer_uuid = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = models.CustomerRoleConcealment
        fields = (
            "uuid",
            "role",
            "role_name",
            "customer",
            "customer_uuid",
            "customer_name",
        )

    def get_customer_uuid(self, obj) -> str | None:
        scope = obj.scope
        return scope.uuid.hex if scope and getattr(scope, "uuid", None) else None

    def get_customer_name(self, obj) -> str | None:
        scope = obj.scope
        return getattr(scope, "name", None) if scope else None

    def validate(self, attrs):
        role = attrs["role"]
        customer = attrs["customer"]
        if role.content_type.model not in ("customer", "project"):
            raise ValidationError(
                "Only customer and project roles can be concealed for an organization."
            )
        # The role must actually be grantable in this organization: a system
        # role (no availability records) or this organization's own clone.
        # Another organization's private clone is not available here and must not
        # be concealable — nor is there anything to conceal.
        if role.availability.exists():
            customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
            available_here = role.availability.filter(
                content_type=customer_ct, object_id=customer.id
            ).exists()
            if not available_here:
                raise ValidationError("Role is not available in this organization.")
        validate_concealment_allowed(customer, role)
        return attrs

    def create(self, validated_data):
        role = validated_data["role"]
        customer = validated_data["customer"]
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        concealment, _ = models.CustomerRoleConcealment.objects.get_or_create(
            role=role, content_type=customer_ct, object_id=customer.id
        )
        return concealment


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
    uuid = serializers.UUIDField(read_only=True)
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    user_name = serializers.CharField(read_only=True, source="user.full_name")
    user_slug = serializers.CharField(read_only=True, source="user.slug")
    user_username = serializers.CharField(read_only=True, source="user.username")
    user_email = serializers.CharField(read_only=True, source="user.email")
    created_by_full_name = serializers.CharField(
        read_only=True, source="created_by.full_name"
    )
    created_by_username = serializers.CharField(
        read_only=True, source="created_by.username"
    )
    revoked_by_full_name = serializers.CharField(
        read_only=True, source="revoked_by.full_name", allow_null=True
    )
    revoked_by_username = serializers.CharField(
        read_only=True, source="revoked_by.username", allow_null=True
    )
    role_name = serializers.CharField(read_only=True, source="role.name")
    role_description = serializers.CharField(read_only=True, source="role.description")
    role_uuid = serializers.UUIDField(read_only=True, source="role.uuid")
    scope_type = serializers.SerializerMethodField()
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")
    scope_name = serializers.CharField(read_only=True, source="scope.name")
    customer_uuid = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    resource_uuid = serializers.SerializerMethodField()
    project_uuid = serializers.SerializerMethodField()
    scope_is_removed = serializers.SerializerMethodField()

    class Meta:
        model = models.UserRole
        fields = (
            "uuid",
            "user_uuid",
            "user_name",
            "user_slug",
            "user_username",
            "user_email",
            "created",
            "expiration_time",
            "is_active",
            "created_by_full_name",
            "created_by_username",
            "revoked_by_full_name",
            "revoked_by_username",
            "revoke_reason",
            "role_name",
            "role_description",
            "role_uuid",
            "scope_type",
            "scope_uuid",
            "scope_name",
            "scope_is_removed",
            "customer_uuid",
            "customer_name",
            "resource_uuid",
            "project_uuid",
        )

    @extend_schema_field(serializers.BooleanField())
    def get_scope_is_removed(self, obj) -> bool:
        """Whether the role's scope has been soft-deleted (e.g. a removed
        project). Used by the frontend to indicate historical memberships on
        deleted scopes and to hide the restore action for them."""
        scope = obj.scope
        if scope is None:
            return False
        return bool(getattr(scope, "is_removed", False))

    def get_scope_type(self, obj) -> str | None:
        if obj.scope is None:
            return None
        model_name = obj.scope._meta.model_name
        for key, (app, model) in TYPE_MAP.items():
            if model == model_name:
                return key
        return model_name

    def _resolve_customer(self, obj):
        """The organisation a role's scope belongs to.

        Most scopes carry ``customer`` themselves, directly or as a property.
        When the scope *is* a customer (e.g. CUSTOMER.OWNER), that organisation
        is the scope itself — there is no nested ``.customer``.

        Proposals deliberately do not expose ``customer``: ``get_scope_ancestors``
        appends ``scope.customer`` when it exists, and ``pat_filtering`` mirrors
        that walk exactly, so giving ``Proposal`` the attribute would hand it an
        ancestor it is documented not to have — a permission-surface change,
        made silently, for the sake of a display column. Resolve it here
        instead, where it only ever reaches the response.

        The chain is the one ``Proposal.Permissions.customer_path`` already
        names: the organisation running the call the proposal was submitted to.
        """
        scope = obj.scope
        if scope is None:
            return None
        model_name = scope._meta.model_name
        if model_name == "customer":
            return scope
        if model_name == "proposal":
            round_ = getattr(scope, "round", None)
            call = getattr(round_, "call", None)
            manager = getattr(call, "manager", None)
            return getattr(manager, "customer", None)
        return getattr(scope, "customer", None)

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_customer_uuid(self, obj) -> str | None:
        customer = self._resolve_customer(obj)
        return customer.uuid.hex if customer is not None else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_name(self, obj) -> str | None:
        customer = self._resolve_customer(obj)
        return customer.name if customer is not None else None

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


class MePermissionSerializer(PermissionSerializer):
    """Trimmed permission projection for the ``/api/users/me`` endpoint.

    Keeps only the fields the frontend reads for the current user, dropping
    the ones that are redundant (per-row user identity) or moot for the
    active-only roles returned here. Roughly halves the ``permissions`` array,
    which dominates the ``me`` response.
    """

    class Meta(PermissionSerializer.Meta):
        fields = (
            "role_name",
            "role_uuid",
            "scope_type",
            "scope_uuid",
            "scope_name",
            "customer_uuid",
            "customer_name",
            "project_uuid",
            "resource_uuid",
            "expiration_time",
        )


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


class UserRolePermissionActionSerializer(serializers.Serializer):
    """Input for revoke/restore actions on a specific user role grant."""

    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)
