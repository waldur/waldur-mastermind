"""Entities a user can restrict a personal access token to.

The create serializer accepts a binding when the user holds one of the token's
permissions on the entity or on one of its ancestors (``get_scope_ancestors``);
staff and holders of a site-wide grant pass for any entity. This lists the
entities of one type that can pass that check: those with an ancestor the user
holds an active role on. Which of the token's permissions the role grants is
checked once the permissions are chosen.

Some ancestors are reached through properties, which a query cannot follow. So
the entities are first narrowed with a query over the foreign keys that lead to
an ancestor (a superset of what can pass), and each candidate is then checked
against its real ancestor chain, as the create serializer will check it.
"""

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from . import enums, global_scope, models, utils

# Type key -> (field path on the entity, type key of the ancestor it leads to).
# Mirrors get_scope_ancestors downward; a path missing here only hides an
# entity that could be chosen, it never lists one that cannot.
ANCESTOR_PATHS = {
    "customer": [("id", "customer")],
    "service_provider": [("id", "service_provider"), ("customer_id", "customer")],
    "call_organizer": [("id", "call_organizer"), ("customer_id", "customer")],
    "project": [("id", "project"), ("customer_id", "customer")],
    "offering": [
        ("id", "offering"),
        ("customer_id", "customer"),
        ("project_id", "project"),
        ("project__customer_id", "customer"),
    ],
    "resource": [
        ("id", "resource"),
        ("offering_id", "offering"),
        ("project_id", "project"),
        ("project__customer_id", "customer"),
    ],
    "resource_project": [
        ("id", "resource_project"),
        ("resource_id", "resource"),
        ("resource__offering_id", "offering"),
        ("resource__project_id", "project"),
        ("resource__project__customer_id", "customer"),
    ],
    "call": [
        ("id", "call"),
        ("manager_id", "call_organizer"),
        ("manager__customer_id", "customer"),
    ],
    "proposal": [
        ("id", "proposal"),
        ("project_id", "project"),
        ("project__customer_id", "customer"),
    ],
}


def descendant_types(type_key: str) -> set[str]:
    """The types a role on a ``type_key`` entity lets a token be bound to:
    that type and the types below it."""
    return {
        target
        for target, paths in ANCESTOR_PATHS.items()
        if any(ancestor == type_key for _path, ancestor in paths)
    }


# Types with no name field of their own are shown and searched by their
# organization's name.
NAME_FIELD = {"service_provider": "customer__name", "call_organizer": "customer__name"}

LIMIT = 50


def _role_scopes(user) -> dict[str, set[int]]:
    """``{type_key: {object_id}}`` of the user's active roles that grant
    anything."""
    rows = (
        models.UserRole.objects.filter(
            user=user,
            is_active=True,
            role__permissions__permission__isnull=False,
        )
        .values_list("content_type__app_label", "content_type__model", "object_id")
        .distinct()
    )
    scopes: dict[str, set[int]] = {}
    for app_label, model, object_id in rows:
        type_key = enums.TYPE_KEY_BY_CT.get((app_label, model))
        if type_key:
            scopes.setdefault(type_key, set()).add(object_id)
    return scopes


def _context(instance) -> str:
    customer = getattr(instance, "customer", None)
    return getattr(customer, "name", "") or ""


def _name(instance) -> str:
    return getattr(instance, "name", "") or _context(instance)


def bindable_entities(user, type_key: str, query: str = "") -> list[dict]:
    """Up to ``LIMIT`` entities of ``type_key`` the user can bind a token to,
    as ``{"type", "instance", "name", "context"}``."""
    model = apps.get_model(*enums.BINDABLE_TYPE_MAP[type_key])
    name_field = NAME_FIELD.get(type_key, "name")
    queryset = model.objects.all()
    if query:
        queryset = queryset.filter(**{f"{name_field}__icontains": query})
    queryset = queryset.order_by(name_field, "id")

    unrestricted = user.is_staff or bool(global_scope.global_permissions(user))
    held: set[tuple[int, int]] = set()
    if not unrestricted:
        scopes = _role_scopes(user)
        conditions = [
            Q(**{f"{path}__in": scopes[ancestor]})
            for path, ancestor in ANCESTOR_PATHS[type_key]
            if scopes.get(ancestor)
        ]
        if not conditions:
            return []
        superset = Q()
        for condition in conditions:
            superset |= condition
        queryset = queryset.filter(superset).distinct()
        held = {
            (ContentType.objects.get_by_natural_key(*enums.TYPE_MAP[key]).id, pk)
            for key, ids in scopes.items()
            for pk in ids
        }

    result = []
    for instance in queryset.iterator():
        if not unrestricted and not any(
            (ContentType.objects.get_for_model(type(ancestor)).id, ancestor.id) in held
            for ancestor in utils.get_scope_ancestors(instance)
        ):
            continue
        result.append(
            {
                "type": type_key,
                "instance": instance,
                "name": _name(instance),
                "context": _context(instance) if type_key != "customer" else "",
            }
        )
        if len(result) >= LIMIT:
            break
    return result
