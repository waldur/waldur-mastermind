"""Personal access token templates: the scopes an integration needs.

A template names the permissions a known integration (the site agent, say)
uses, so its token carries those and nothing else. It is a suggestion: a token
is still created through the ordinary create endpoint with explicit scopes, and
the create serializer still decides what it may carry.

A target is an entity a token from the template is bound to, and it is offered
only where the token will work there:

- Without ``binding_on_path``, a permission check reads the token's binding on
  the object the role is found on, so the role has to be on the target itself:
  an organization owner's token bound to one of its offerings cannot use the
  owner's role on the organization.
- What a token reads follows the user's roles, not the token's scopes, so the
  role on the target must also grant what the integration's reads depend on
  (``role_permissions``). These are not put on the token.
- Site-wide grants do not make a target: a token bound to an entity the user
  holds nothing on would rely on them for every check, and its reads would
  still be filtered by the user's roles there.

A staff user's token without STAFF.ACCESS acts as the user without staff, so
staff get no special case.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Model, Q, QuerySet
from django.utils.text import slugify

from . import enums, models


@dataclass(frozen=True)
class PatTemplateFeature:
    key: str
    name: str
    description: str
    scopes: tuple[enums.PermissionEnum, ...]


@dataclass(frozen=True)
class PatTemplate:
    key: str
    name: str
    description: str
    required_scopes: tuple[enums.PermissionEnum, ...]
    # Type key (as in enums.TYPE_MAP) -> the entities of that type a token from
    # this template may be bound to, before the user's roles are considered.
    binding_targets: dict[str, Callable[[], QuerySet]]
    optional_features: tuple[PatTemplateFeature, ...] = ()
    # Held by the user's role on a target, not carried by the token: what the
    # integration's reads are filtered by.
    role_permissions: tuple[enums.PermissionEnum, ...] = ()
    default_lifetime_days: int = 365
    # Shown on a target, for example the organization of an offering.
    target_context: dict[str, Callable[[Model], str]] = field(default_factory=dict)
    # Type key -> (field on the target, type key) of the ancestors whose roles
    # count for a target of that type, besides the target's own. Only for an
    # integration whose checks accept a role above the binding
    # (binding_on_path): an owner's role on the organization, for a token bound
    # to one of its offerings.
    role_ancestors: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    # Type key -> the role a dedicated user is given on a target of that type
    # when staff create one for the integration (the ``agent_user`` action), so
    # its token is restricted to the target. Absent: no dedicated users.
    dedicated_user_role: dict[str, Callable[[], Model]] = field(default_factory=dict)

    @property
    def binding_types(self) -> list[str]:
        return list(self.binding_targets)


_TEMPLATES: dict[str, PatTemplate] = {}


def register(template: PatTemplate) -> None:
    for type_key in template.binding_targets:
        if type_key not in enums.BINDABLE_TYPE_MAP:
            raise ValueError(f"{type_key} is not a type a token can be bound to")
    _TEMPLATES[template.key] = template


def get_templates() -> list[PatTemplate]:
    return list(_TEMPLATES.values())


def get_template(key: str) -> PatTemplate | None:
    return _TEMPLATES.get(key)


@dataclass(frozen=True)
class PatTemplateTarget:
    type: str
    instance: Model
    context: str
    features: tuple[str, ...]
    # Offered to staff where they hold no role that would do: the token works
    # only with STAFF.ACCESS, which cannot be bound, so it is not restricted to
    # the target and reaches everything staff can.
    staff_access: bool = False
    template: "PatTemplate | None" = None

    @property
    def bindings(self) -> tuple[Model, ...]:
        return () if self.staff_access else (self.instance,)


def _held_by_object(user, type_key: str, wanted: set[str]) -> dict:
    """``{object_id: {permission}}`` of ``wanted`` that the user's active roles
    grant on each object of ``type_key`` itself."""
    content_type = ContentType.objects.get_by_natural_key(*enums.TYPE_MAP[type_key])
    rows = models.UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type=content_type,
        role__permissions__permission__in=wanted,
    ).values_list("object_id", "role__permissions__permission")
    held = defaultdict(set)
    for object_id, permission in rows:
        held[object_id].add(permission)
    return held


def _eligible(
    template: PatTemplate, type_key: str, user, name: str = "", uuid: str = ""
) -> tuple[QuerySet, dict]:
    """The ``type_key`` targets, and the template's permissions held on each."""
    needed = {
        scope.value for scope in (*template.required_scopes, *template.role_permissions)
    }
    wanted = needed | {
        scope.value
        for feature in template.optional_features
        for scope in feature.scopes
    }
    held_by_object = _held_by_object(user, type_key, wanted)
    ancestors = template.role_ancestors.get(type_key, ())
    if ancestors:
        # Fold what is held on each target's ancestors into what is held on
        # the target itself.
        held_by_ancestor = {
            ancestor_type: _held_by_object(user, ancestor_type, wanted)
            for _field, ancestor_type in ancestors
        }
        candidates = template.binding_targets[type_key]().filter(
            Q(id__in=list(held_by_object))
            | _any_of(
                Q(**{f"{field}__in": list(held_by_ancestor[ancestor_type])})
                for field, ancestor_type in ancestors
            )
        )
        held_by_object = {
            row["id"]: set().union(
                held_by_object.get(row["id"], set()),
                *(
                    held_by_ancestor[ancestor_type].get(row[field], set())
                    for field, ancestor_type in ancestors
                ),
            )
            for row in candidates.values("id", *(f for f, _ in ancestors))
        }
    eligible_ids = [
        object_id for object_id, held in held_by_object.items() if needed <= held
    ]
    queryset = template.binding_targets[type_key]().filter(id__in=eligible_ids)
    if name:
        queryset = queryset.filter(name__icontains=name)
    if uuid:
        queryset = queryset.filter(uuid=uuid)
    return queryset, held_by_object


def _any_of(conditions) -> Q:
    combined = Q(pk__in=[])
    for condition in conditions:
        combined |= condition
    return combined


def _usable(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active)


def get_targets(
    template: PatTemplate, user, name: str = "", uuid: str = ""
) -> list[PatTemplateTarget]:
    """The entities a token from ``template`` can be bound to and still work,
    with the optional features the user can add on each.

    Staff also get every other target, marked ``staff_access``: there the
    token works only by carrying STAFF.ACCESS, unrestricted.
    """
    if not _usable(user):
        return []
    targets = []
    for type_key in template.binding_targets:
        queryset, held_by_object = _eligible(template, type_key, user, name, uuid)
        context = template.target_context.get(type_key)
        if user.is_staff:
            others = template.binding_targets[type_key]().exclude(
                id__in=queryset.values("id")
            )
            if name:
                others = others.filter(name__icontains=name)
            if uuid:
                others = others.filter(uuid=uuid)
            every_feature = tuple(f.key for f in template.optional_features)
            targets.extend(
                PatTemplateTarget(
                    type=type_key,
                    instance=instance,
                    context=context(instance) if context else "",
                    features=every_feature,
                    staff_access=True,
                    template=template,
                )
                for instance in others.order_by("name", "id")
            )
        for instance in queryset.order_by("name", "id"):
            held = held_by_object.get(instance.id, set())
            features = tuple(
                feature.key
                for feature in template.optional_features
                if {scope.value for scope in feature.scopes} <= held
            )
            targets.append(
                PatTemplateTarget(
                    type=type_key,
                    instance=instance,
                    context=context(instance) if context else "",
                    features=features,
                    template=template,
                )
            )
    # A target the user's roles qualify for first, then the staff ones.
    return sorted(targets, key=lambda target: target.staff_access)


def has_targets(template: PatTemplate, user) -> bool:
    """Whether the user can bind a token from ``template`` anywhere it works,
    or, for staff, create one with STAFF.ACCESS."""
    if not _usable(user):
        return False
    if user.is_staff:
        return any(
            template.binding_targets[type_key]().exists()
            for type_key in template.binding_targets
        )
    return any(
        _eligible(template, type_key, user)[0].exists()
        for type_key in template.binding_targets
    )


def dedicated_username(template: PatTemplate, target: Model) -> str:
    """The username a dedicated user for ``target`` gets when none is given:
    the template, the target's name and the start of its UUID, such as
    ``site-agent-slurm-hpc-cluster-f30000``; ``-2``, ``-3``... while taken."""
    user_model = apps.get_model("core", "User")
    name = slugify(f"{template.key}-{getattr(target, 'name', '')}")[:100]
    target_uuid = getattr(target, "uuid")  # noqa: B009 - every target has one
    base = f"{name}-{target_uuid.hex[:6]}".replace("_", "-")
    username, suffix = base, 1
    while user_model.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base}-{suffix}"
    return username
