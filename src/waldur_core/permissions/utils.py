import logging
from typing import TYPE_CHECKING

from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.query import QuerySet
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.exceptions import ValidationError

from waldur_core.core.auth_utils import is_pat_auth
from waldur_core.core.models import User, UserDetailsMatchMixin

from . import enums, models, signals

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from django.db.models import Model
    from django.http import HttpRequest


# Predicates that exempt a UserRole from the automatic expiration sweep
# (permissions.tasks.check_expired_permissions). Apps register a predicate in
# their AppConfig.ready() to protect scopes whose membership must not be
# auto-revoked when a grant's expiration_time passes.
_expiration_guards: list = []


def register_expiration_guard(predicate) -> None:
    """Register ``predicate(user_role) -> bool``; returning ``True`` exempts the
    role from automatic expiration revocation."""
    _expiration_guards.append(predicate)


def is_expiration_exempt(user_role) -> bool:
    """True if any registered guard opts this role out of auto-expiry."""
    return any(guard(user_role) for guard in _expiration_guards)


def _pat_allowed_pairs(auth) -> frozenset:
    """Return the frozenset of ``(content_type_id, object_id)`` bindings.

    Cached on the PAT instance after the first call so subsequent
    permission checks on the same request reuse the set. Malformed
    entries are skipped — a bad row must not be able to break auth.
    """
    cached = getattr(auth, "_allowed_pairs_cache", None)
    if cached is not None:
        return cached
    pairs_set = set()
    for b in auth.allowed_scopes or []:
        try:
            pairs_set.add((b["content_type_id"], b["object_id"]))
        except (KeyError, TypeError):
            continue
    pairs = frozenset(pairs_set)
    try:
        auth._allowed_pairs_cache = pairs
    except AttributeError:
        # ``auth`` is not a PAT instance with writable attrs (e.g. a mock).
        pass
    return pairs


def _pat_entity_check(auth, scope) -> bool:
    """Return True if the PAT's entity bindings allow acting on ``scope``.

    - Empty bindings → always allow.
    - Non-empty bindings + ``scope is None`` → deny (a scoped PAT cannot
      perform scope-less actions).
    - Otherwise → allow iff some ancestor of ``scope`` matches a binding.
      The walk is upward-only: a binding to a child never authorises
      actions on its parents.
    """
    allowed_pairs = _pat_allowed_pairs(auth)
    if not allowed_pairs:
        return True
    if scope is None:
        return False
    for ancestor in get_scope_ancestors(scope):
        ct_id = ContentType.objects.get_for_model(type(ancestor)).id
        if (ct_id, ancestor.id) in allowed_pairs:
            return True
    return False


def _pat_scope_check(
    request, permission: enums.PermissionEnum, scope=None
) -> bool | None:
    """Check the PAT permission allowlist and entity binding for one permission.

    Returns False to short-circuit a deny, None to continue the normal flow
    (either non-PAT request, or PAT passed both checks).
    """
    if isinstance(request, User):
        return None
    auth = getattr(request, "auth", None)
    if not is_pat_auth(auth):
        return None
    if permission.value not in auth.scopes:
        return False
    if not _pat_entity_check(auth, scope):
        return False
    return None


def check_pat_staff_scope(request) -> bool:
    """Return True if the request is allowed staff-level access.

    Non-PAT requests always pass.  PAT requests require the
    STAFF_ACCESS scope.
    """
    auth = getattr(request, "auth", None)
    if not is_pat_auth(auth):
        return True
    return enums.PermissionEnum.STAFF_ACCESS.value in auth.scopes


def check_pat_support_scope(request) -> bool:
    """Return True if the request is allowed support-level access.

    Non-PAT requests always pass.  PAT requests require either
    STAFF_ACCESS or SUPPORT_ACCESS scope.
    """
    auth = getattr(request, "auth", None)
    if not is_pat_auth(auth):
        return True
    return (
        enums.PermissionEnum.STAFF_ACCESS.value in auth.scopes
        or enums.PermissionEnum.SUPPORT_ACCESS.value in auth.scopes
    )


def has_permission(
    request: "HttpRequest | User",
    permission: enums.PermissionEnum,
    scope: "Model | None",
) -> bool:
    if isinstance(request, User):
        user = request
    else:
        user = request.user

        # PAT ceiling — checked before is_staff bypass so a scoped PAT
        # narrows even staff users.
        pat_result = _pat_scope_check(request, permission, scope)
        if pat_result is False:
            return False

    # Inactive users should not have any permissions
    if not user.is_active:
        return False

    if user.is_staff:
        return True

    # Handle None scope
    if scope is None:
        return False

    # Single query with join instead of two separate queries
    return models.UserRole.objects.filter(
        user=user,
        is_active=True,
        scope=scope,
        role__permissions__permission=permission,
    ).exists()


def has_any_permission(
    request: "HttpRequest | User",
    permissions: list[enums.PermissionEnum],
    scope: "Model | None",
) -> bool:
    """Check if user has any of the given permissions in scope."""
    if isinstance(request, User):
        user = request
    else:
        user = request.user

        # PAT ceiling — narrow the permission set to what the PAT carries
        # and reject if the scope falls outside the PAT's entity bindings.
        auth = getattr(request, "auth", None)
        if is_pat_auth(auth):
            permissions = [p for p in permissions if p.value in auth.scopes]
            if not permissions:
                return False
            if not _pat_entity_check(auth, scope):
                return False

    if not user.is_active:
        return False

    if user.is_staff:
        return True

    if scope is None:
        return False

    return models.UserRole.objects.filter(
        user=user,
        is_active=True,
        scope=scope,
        role__permissions__permission__in=permissions,
    ).exists()


def has_all_permissions(
    request: "HttpRequest | User",
    permissions: list[enums.PermissionEnum],
    scope: "Model | None",
) -> bool:
    """Check if user has all of the given permissions in scope."""
    return all(has_permission(request, p, scope) for p in permissions)


def has_permission_on_any_source(
    request: "HttpRequest | User",
    permission: enums.PermissionEnum,
    scope: "Model | None",
    sources: list[str] | None = None,
) -> bool:
    """Whether the user holds permission on scope or on any of its named sources.

    `sources` are attribute paths relative to scope, with `"*"` standing for
    scope itself — e.g. `["*", "customer"]` accepts the permission held either
    on the object or on its organization. An empty list means scope only.

    This is the traversal `permission_factory` enforces, exposed as a predicate
    so that a read gate and the write gate it has to agree with can be built
    from the same rule instead of restating it.
    """
    if not scope:
        return False

    if not sources:
        return has_permission(request, permission, scope)

    attribute_errors = 0
    for path in sources:
        try:
            source = scope
            if path != "*":
                for part in path.split("."):
                    source = getattr(source, part)
            if has_permission(request, permission, source):
                return True
        except AttributeError:
            # Continue to next path if attribute doesn't exist
            attribute_errors += 1
            continue

    # If all paths failed due to AttributeError, raise AttributeError
    if attribute_errors == len(sources):
        raise AttributeError(
            f"None of the attribute paths {sources} exist on the scope object"
        )

    return False


def permission_factory(permission, sources=None):
    if not isinstance(permission, enums.PermissionEnum):
        raise ValueError(f"permission must be PermissionEnum, got {type(permission)}")
    if sources is not None and not isinstance(sources, list):
        raise ValueError(f"sources must be a list or None, got {type(sources)}")

    def permission_function(request, view, scope=None):
        if not scope:
            return

        if has_permission_on_any_source(request, permission, scope, sources):
            return

        raise exceptions.PermissionDenied()

    # Attach metadata to the function object
    # This makes the raw data available for inspection.
    setattr(permission_function, "permission", permission)
    setattr(permission_function, "sources", sources)

    return permission_function


def get_users(scope, role_name=None):
    users = models.UserRole.objects.filter(is_active=True, scope=scope)
    if role_name:
        users = users.filter(role__name=role_name)
    user_ids = users.values_list("user_id", flat=True)
    return User.objects.filter(id__in=user_ids)


def get_users_with_permission(scope, permission):
    user_ids = models.UserRole.objects.filter(
        is_active=True, scope=scope, role__permissions__permission=permission
    ).values_list("user_id", flat=True)
    return User.objects.filter(id__in=user_ids)


def get_scope_ids(user, content_type, role=None, permission=None) -> QuerySet[int]:
    """Ids — not objects — of the scopes the user holds a role on.

    Callers routinely feed this to ``filter(scope__in=...)``, which works with
    ids. A membership test does not: ``obj in <queryset of ints>`` is neither a
    type error nor a runtime error, it is silently False.
    """
    qs = models.UserRole.objects.filter(
        is_active=True, user=user, content_type=content_type
    )
    if role:
        if not isinstance(role, list | tuple):
            role = [role]
        # Convert Role objects to their names, but leave strings unchanged
        role_names = []
        for r in role:
            # Check if this is a Role model instance using isinstance
            if isinstance(r, models.Role):
                role_names.append(r.name)
            else:
                # This is a string (like RoleEnum) - use directly
                role_names.append(r)
        qs = qs.filter(role__name__in=role_names)
    if permission:
        qs = qs.filter(role__permissions__permission=permission)
    return qs.order_by().values_list("object_id", flat=True).distinct()


def get_user_ids(content_type, scope_ids, role=None):
    if not isinstance(scope_ids, list | tuple | QuerySet):
        scope_ids = [scope_ids]
    qs = models.UserRole.objects.filter(
        is_active=True, object_id__in=scope_ids, content_type=content_type
    )
    if role:
        if isinstance(role, models.Role):
            qs = qs.filter(role=role)
        else:
            if not isinstance(role, list | tuple):
                role = [role]
            qs = qs.filter(role__name__in=role)
    return qs.values_list("user_id", flat=True)


def count_users(scope):
    return (
        models.UserRole.objects.filter(is_active=True, scope=scope)
        .values_list("user_id")
        .distinct()
        .count()
    )


def has_user(scope, user, role=None, expiration_time=False):
    """
    Checks whether user has role in entity.
    `expiration_time` can have the following values:
        - False (default) - check whether user has role in entity regardless of expiration.
        - None - check whether user has permanent role in entity.
        - Datetime object - check whether user will have role in entity at specific timestamp.
    """
    qs = models.UserRole.objects.filter(is_active=True, user=user, scope=scope)
    if role:
        qs = qs.filter(role=role)
    if expiration_time is None:
        qs = qs.filter(expiration_time=None)
    elif expiration_time is not False:
        # expiration_time is a datetime - check if role will be active at that time
        qs = qs.filter(
            Q(expiration_time=None) | Q(expiration_time__gte=expiration_time)
        )
    # When expiration_time is False, don't filter by expiration at all
    return qs.exists()


def get_permissions(scope, user=None):
    qs = models.UserRole.objects.filter(
        scope=scope, is_active=True, user__is_active=True
    ).select_related("role", "user", "created_by")
    if user:
        qs = qs.filter(user=user)
    return qs


def exclude_removed_project_roles(queryset):
    """Drop roles whose scope is a soft-deleted (terminated) project.

    A ``UserRole`` keeps ``is_active=True`` even after its scope project is
    terminated when the role belonged to an already-inactive user at
    termination time (project termination only revokes the roles of active
    users) or when the row predates the termination handler. Such roles grant
    no real access, so the auto-deactivation policy must not count them — the
    same way course accounts are guarded by ``project__is_removed=False``.

    The scope is a generic relation, so the Project model is resolved via its
    ContentType to avoid importing ``structure`` into this core app.
    """
    project_ct = ContentType.objects.filter(
        app_label="structure", model="project"
    ).first()
    if project_ct is None:
        return queryset
    removed_project_ids = project_ct.model_class().objects.filter(is_removed=True)
    return queryset.exclude(
        content_type=project_ct, object_id__in=removed_project_ids.values("pk")
    )


def get_active_roles(user):
    """Active roles that grant real access to a user.

    Excludes roles attached to terminated projects (see
    ``exclude_removed_project_roles``). Used by the auto-deactivation policy.
    """
    return exclude_removed_project_roles(
        models.UserRole.objects.filter(user=user, is_active=True)
    )


def get_scope_ancestors(scope):
    """Walk parent links from scope upward for RoleAvailability checks.

    Resource → Offering / Project → Customer.
    ResourceProject → Resource → … (chain above).
    """
    if isinstance(scope, User):
        # A user has no scope ancestors: identity is the whole chain. The
        # hasattr probing below must never wander into reverse accessors of
        # the user model.
        return [scope]
    ancestors = [scope]
    if hasattr(scope, "resource"):  # ResourceProject -> Resource
        ancestors.append(scope.resource)
        if hasattr(scope.resource, "offering"):
            ancestors.append(scope.resource.offering)
        if hasattr(scope.resource, "project"):
            ancestors.append(scope.resource.project)
            if hasattr(scope.resource.project, "customer"):
                ancestors.append(scope.resource.project.customer)
    if hasattr(scope, "offering"):  # Resource -> Offering
        ancestors.append(scope.offering)
    if hasattr(scope, "project"):  # Resource -> Project
        ancestors.append(scope.project)
        if hasattr(scope.project, "customer"):
            ancestors.append(scope.project.customer)
    if hasattr(scope, "customer"):  # Project -> Customer
        ancestors.append(scope.customer)
    # Nullable parent FKs (e.g. an offering with no customer) append None;
    # drop them so callers can safely resolve each ancestor's content type.
    return [ancestor for ancestor in ancestors if ancestor is not None]


def count_active_project_managers(project):
    now = timezone.now()
    return (
        models.UserRole.objects.filter(
            scope=project,
            role__name=enums.RoleEnum.PROJECT_MANAGER,
            is_active=True,
            user__is_active=True,
        )
        .filter(Q(expiration_time=None) | Q(expiration_time__gte=now))
        .count()
    )


def validate_only_one_project_manager(scope, role):
    if not config.ONLY_ONE_PROJECT_MANAGER:
        return

    if scope._meta.model_name != "project":
        return

    if role.name != enums.RoleEnum.PROJECT_MANAGER:
        return

    if count_active_project_managers(scope) >= 1:
        raise ValidationError("Project already has an active project manager.")


def check_grant_policy(scope, role):
    """Enforce the org-scoping policy for granting ``role`` on ``scope``.

    Two rules, both resolved by walking ``get_scope_ancestors(scope)`` so a
    record bound to a Customer also governs its Projects:

    - **RoleAvailability** (allow-list): if the role has any availability
      records, the scope or an ancestor must match one, else the role is not
      usable here.
    - **CustomerRoleConcealment** (deny-list): if an ancestor conceals the role,
      the grant is refused.

    Raises ``ValidationError`` on violation. Shared by ``validate_role_grant``
    (rich, early errors on the DRF/invitation paths) and ``add_user`` (the
    backstop that also covers direct callers).
    """
    ancestors = get_scope_ancestors(scope)

    if role.availability.exists():
        has_valid = any(
            models.RoleAvailability.objects.filter(
                role=role,
                content_type=ContentType.objects.get_for_model(ancestor),
                object_id=ancestor.id,
            ).exists()
            for ancestor in ancestors
        )
        if not has_valid:
            raise ValidationError("Role is not available for this scope.")

    is_concealed = any(
        models.CustomerRoleConcealment.objects.filter(
            role=role,
            content_type=ContentType.objects.get_for_model(ancestor),
            object_id=ancestor.id,
        ).exists()
        for ancestor in ancestors
    )
    if is_concealed:
        raise ValidationError("Role is concealed for this organization.")


def validate_scope_available(scope):
    """Reject a grant on a scope that does not accept role assignments.

    Currently only marketplace offerings: a private (non-shared) offering is
    owned by a single organization and its roles are not handed out, which
    ``UserRoleCreateSerializer.validate`` already enforces on the direct
    add_user path. Checked here as well so the invitation path
    (``Invitation.accept``) and ``PermissionRequest.approve`` cannot be used to
    reach the same grant through the back door.

    Guarded on the attribute rather than the model because this helper is
    generic across every scope type in TYPE_MAP; only Offering defines
    ``shared``.
    """
    if getattr(scope, "shared", None) is False:
        raise ValidationError("Offering is not available.")


def validate_role_grant(scope, user, role, expiration_time=None):
    """Validate a role can be granted to a user on scope.

    Mirrors the role/scope checks in UserRoleCreateSerializer.validate so
    non-DRF callers (Invitation.accept, PermissionRequest.approve) enforce the
    same invariants. Permission/auth checks stay with the caller — this helper
    only validates the (scope, user, role) triple.
    """
    if has_user(scope, user, role, expiration_time=expiration_time):
        raise ValidationError("User has already the same role in this scope.")

    if not isinstance(scope, role.content_type.model_class()):
        raise ValidationError("Role is not valid for this scope.")

    if not role.is_active:
        raise ValidationError("Role is not active.")

    validate_scope_available(scope)

    check_grant_policy(scope, role)

    validate_only_one_project_manager(scope, role)
    validate_user_restrictions(scope, user)


def build_org_role_name(template, slug):
    """Build the ``PREFIX.<slug>.SUFFIX`` name for a customer-scoped clone.

    The owning organization's slug is embedded (instead of an opaque UUID) so the
    name is human-readable; it is kept in sync when the slug changes (see the
    Customer slug-change handler). The scope prefix stays first so
    ``name__startswith="PROJECT."`` filters keep working.
    """
    base = template.name
    if "." in base:
        prefix, suffix = base.split(".", 1)
    else:
        prefix, suffix = template.content_type.model.upper(), base
    return f"{prefix}.{slug}.{suffix}"


def ensure_unique_role_name(name, exclude_id=None):
    """Return ``name``, or ``name-2``/``name-3``/... if it collides.

    Role names must be globally unique. Since a slug is not database-unique and
    can be freed up and reused by another organization, this guards the embedded
    slug against name clashes.
    """
    qs = models.Role.objects.all()
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    if not qs.filter(name=name).exists():
        return name
    index = 2
    while qs.filter(name=f"{name}-{index}").exists():
        index += 1
    return f"{name}-{index}"


def add_user(scope, user, role, created_by=None, expiration_time=None, force=False):
    """Grant ``role`` to ``user`` on ``scope`` (low-level write primitive).

    Enforces the org-scoping policy (:func:`check_grant_policy`) so direct
    callers that bypass ``validate_role_grant`` still respect availability and
    concealment. Pass ``force=True`` for the few internal grants that must bypass
    the policy (e.g. onboarding's initial owner grant).
    """
    if not force:
        check_grant_policy(scope, role)
    content_type = ContentType.objects.get_for_model(scope)
    permission = models.UserRole.objects.create(
        user=user,
        role=role,
        content_type=content_type,
        object_id=scope.id,
        expiration_time=expiration_time,
        created_by=created_by,
    )
    signals.role_granted.send(
        sender=models.UserRole,
        instance=permission,
        current_user=created_by,
    )
    return permission


def add_user_or_skip(scope, user, role, created_by=None, expiration_time=None):
    """Grant ``role`` respecting the org-scoping policy, skipping on rejection.

    For non-interactive callers — signal handlers, auto-provisioning, group sync,
    team-restore — where one policy rejection (concealed or org-unavailable role)
    must not abort the whole operation. The grant is skipped and logged instead of
    raising. Returns the created ``UserRole`` or ``None`` if it was skipped.
    """
    try:
        return add_user(
            scope, user, role, created_by=created_by, expiration_time=expiration_time
        )
    except ValidationError as exc:
        logger.warning(
            "Skipped granting role %s to user %s on %s: %s",
            role,
            getattr(user, "uuid", user),
            scope,
            exc,
        )
        return None


def update_user(
    scope, user, role, expiration_time=None, current_user=None, reason=None
):
    try:
        permission = models.UserRole.objects.get(
            user=user,
            role=role,
            scope=scope,
            is_active=True,
        )
    except models.UserRole.DoesNotExist:
        return False
    permission.set_expiration_time(expiration_time, current_user, reason=reason)
    return permission


def delete_user(scope, user, role, current_user=None, reason=None):
    try:
        permission = models.UserRole.objects.get(
            user=user,
            role=role,
            scope=scope,
            is_active=True,
        )
    except models.UserRole.DoesNotExist:
        return False

    if not reason:
        if current_user:
            reason = "Manual user removal via API"
        else:
            reason = "System-initiated user removal"

    permission.revoke(current_user, reason=reason)
    return True


def get_customer(scope):
    model_name = scope._meta.model_name
    if model_name == "customer":
        return scope
    else:
        return scope.customer


def get_valid_content_types():
    return [
        ContentType.objects.get_by_natural_key(*pair)
        for pair in enums.TYPE_MAP.values()
    ]


def get_valid_models():
    return [ct.model_class() for ct in get_valid_content_types()]


def get_create_permission(model_class):
    return enums.CREATE_PERMISSIONS.get(model_class._meta.model_name)


def get_delete_permission(model_class):
    return enums.DELETE_PERMISSIONS.get(model_class._meta.model_name)


def get_update_permission(model_class):
    return enums.UPDATE_PERMISSIONS.get(model_class._meta.model_name)


def validate_user_restrictions(scope, user):
    """
    Validate user matches scope's email/affiliation/identity_source/AAI restrictions.

    For Projects, also validates against parent Customer restrictions.
    User must match restrictions at each level (AND logic across levels).
    Within a level, user must match any email pattern OR any affiliation
    OR any identity source (OR logic for basic filters).
    AAI filters (nationality, organization_type, assurance) are additional requirements.

    Raises ValidationError if user doesn't match restrictions.
    """
    # Check if scope supports restrictions
    if not hasattr(scope, "user_email_patterns"):
        return

    # For projects, first check customer restrictions
    if hasattr(scope, "customer"):
        validate_user_restrictions(scope.customer, user)

    # Check basic restrictions (email/affiliation/identity_source)
    has_basic_restrictions = (
        scope.user_email_patterns
        or scope.user_affiliations
        or getattr(scope, "user_identity_sources", None)
    )

    basic_match = False
    if not has_basic_restrictions:
        basic_match = True
    else:
        # Check affiliation match (OR logic within affiliations)
        if scope.user_affiliations:
            if set(user.affiliations or []) & set(scope.user_affiliations):
                basic_match = True

        # Check email pattern match (OR logic within patterns)
        if not basic_match and scope.user_email_patterns:
            for pattern in scope.user_email_patterns:
                if UserDetailsMatchMixin._is_pattern_match(pattern, user.email):
                    basic_match = True
                    break

        # Check identity source match (OR logic within identity sources)
        if not basic_match:
            identity_sources = getattr(scope, "user_identity_sources", None)
            if identity_sources:
                if user.identity_source and user.identity_source in identity_sources:
                    basic_match = True

    if not basic_match:
        scope_name = scope._meta.model_name
        raise ValidationError(
            f"User email, affiliation, or identity source does not match the {scope_name} restrictions."
        )

    # Check AAI restrictions (additional requirements on top of basic match)
    # These are AND logic - each configured restriction must be satisfied

    # Check nationality restriction (OR logic - user must have one of the allowed)
    user_nationalities = getattr(scope, "user_nationalities", None)
    if user_nationalities:
        user_nat = getattr(user, "nationality", "") or ""
        user_nats = getattr(user, "nationalities", []) or []
        all_user_nats = {user_nat} | set(user_nats)
        all_user_nats.discard("")  # Remove empty string if present
        if not (all_user_nats & set(user_nationalities)):
            scope_name = scope._meta.model_name
            raise ValidationError(
                f"User nationality does not match the {scope_name} restrictions."
            )

    # Check organization type restriction (OR logic)
    user_organization_types = getattr(scope, "user_organization_types", None)
    if user_organization_types:
        user_org_type = getattr(user, "organization_type", "") or ""
        if user_org_type not in user_organization_types:
            scope_name = scope._meta.model_name
            raise ValidationError(
                f"User organization type does not match the {scope_name} restrictions."
            )

    # Check assurance level restriction (AND logic - user must have ALL required)
    user_assurance_levels = getattr(scope, "user_assurance_levels", None)
    if user_assurance_levels:
        user_assurance = set(getattr(user, "eduperson_assurance", []) or [])
        required_assurance = set(user_assurance_levels)
        if not required_assurance.issubset(user_assurance):
            scope_name = scope._meta.model_name
            raise ValidationError(
                f"User assurance level does not match the {scope_name} restrictions."
            )


# --- Pub/sub scope bindings (scope bindings / dispatch) ---------------------------------------
# One rule for BOTH "what you may bind a consumer to" (registration) and "who
# may still receive an event" (delivery): the user must hold an active role on
# the entity or one of its ancestors. get_scope_ancestors(offering) yields
# [offering, offering.project, project.customer, offering.customer], which is
# exactly what OfferingQuerySet.filter_for_user ORs over — so the unified path
# is never narrower than the legacy path it replaces.


def scope_keys_for(scope) -> list[tuple[int, int]]:
    """(content_type_id, object_id) for `scope` and each of its ancestors."""
    keys = []
    for ancestor in get_scope_ancestors(scope):
        ct = ContentType.objects.get_for_model(ancestor.__class__)
        keys.append((ct.id, ancestor.id))
    return keys


def scope_keys_q(
    scope_keys, content_type_field="content_type_id", object_id_field="object_id"
):
    """OR of one Q per content_type, each with an ``object_id IN (...)``.

    Index-friendly on a (content_type, object_id) index and plain public API —
    deliberately not Django's private tuple-lookup machinery.
    """
    by_ct: dict[int, list[int]] = {}
    for ct_id, object_id in scope_keys:
        by_ct.setdefault(ct_id, []).append(object_id)
    query = Q()
    for ct_id, object_ids in by_ct.items():
        query |= Q(**{content_type_field: ct_id, f"{object_id_field}__in": object_ids})
    return query


def holds_any_role_on_scope_or_ancestor(user, scope) -> bool:
    """Registration guard: may this user bind a consumer to `scope`?"""
    if not user.is_active:
        return False
    if user.is_staff or user.is_support:
        return True
    if isinstance(scope, User):
        # Self-referential user scope: identity, not a UserRole. A user may
        # always bind to themselves; binding to anyone else is staff/support
        # only (handled above).
        return scope.id == user.id
    keys = scope_keys_for(scope)
    if not keys:
        return False
    return (
        models.UserRole.objects.filter(user=user, is_active=True)
        .filter(scope_keys_q(keys))
        .exists()
    )


def users_with_role_on_any_scope_key(user_ids, scope_keys) -> set[int]:
    """Delivery re-auth, batched: which of `user_ids` still hold an active role
    somewhere in this event's scope chain. ONE query for the whole fan-out.

    Because `scope_keys` are the keys of a single event (entity + ancestors), a
    role on an unrelated binding cannot grant access to this event.
    """
    if not user_ids or not scope_keys:
        return set()
    return set(
        models.UserRole.objects.filter(is_active=True, user_id__in=list(user_ids))
        .filter(scope_keys_q(scope_keys))
        .values_list("user_id", flat=True)
    )
