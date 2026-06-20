"""Shared helpers for ACCOUNT-category chat tools.

Every nav tool filters its queryset through the helpers here so that
permission scoping, UUID/name search and serialization stay in one
place. Tools never touch ``filter_queryset_for_user`` directly.
"""

import uuid as uuid_module
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, QuerySet

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Customer, Project


def sum_invoice_item_totals(items) -> Decimal:
    """Sum ``InvoiceItem.total`` over an iterable.

    ``total`` is a Python property (price + tax, with per-day quantization and
    current-month quantity recomputation), so it cannot be aggregated in SQL —
    callers must iterate. Kept here so the credit tools share one definition.
    """
    total = Decimal("0")
    for item in items:
        total += item.total
    return total


def validate_uuid(value: str) -> bool:
    """True when ``value`` parses as a UUID (any hyphenation, any casing)."""
    if not value:
        return False
    try:
        uuid_module.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def name_search_filter(
    search: str,
    name_field: str = "name",
    extra_fields: list[str] | None = None,
) -> Q:
    """Build a ``Q`` for text-only icontains search across name + extras.

    Matches ``name_field`` and every entry in ``extra_fields`` via
    ``icontains``. NEVER touches the ``uuid`` column — UUID lookup is
    handled by explicit ``uuid`` arguments at the tool layer, following
    the marketplace pattern (text search vs. entity resolution as
    separate concerns).

    Empty ``search`` returns ``Q()``; callers should skip the filter.
    """
    if not search:
        return Q()
    q = Q(**{f"{name_field}__icontains": search})
    for field in extra_fields or []:
        q |= Q(**{f"{field}__icontains": search})
    return q


def user_accessible_customers(user) -> QuerySet:
    """Customers the user can see, via the structure app's scoping manager."""
    return filter_queryset_for_user(Customer.objects.all(), user)


def user_accessible_projects(user) -> QuerySet:
    """Projects the user can see."""
    return filter_queryset_for_user(Project.objects.all(), user)


# Role priority for the "which role to surface on a serializer" decision.
# Owner outranks manager outranks others. When a user has several active
# roles on the same scope we return the highest-ranked one.
_CUSTOMER_ROLE_RANK = {
    RoleEnum.CUSTOMER_OWNER: 0,
    RoleEnum.CUSTOMER_MANAGER: 1,
    RoleEnum.CUSTOMER_SUPPORT: 2,
    RoleEnum.CUSTOMER_READER: 3,
}

_PROJECT_ROLE_RANK = {
    RoleEnum.PROJECT_MANAGER: 0,
    RoleEnum.PROJECT_ADMIN: 1,
    RoleEnum.PROJECT_MEMBER: 2,
}


def _first_role(user, scope, rank: dict) -> RoleEnum | None:
    content_type = ContentType.objects.get_for_model(scope)
    names = UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type=content_type,
        object_id=scope.id,
    ).values_list("role__name", flat=True)
    best_rank = None
    best_name = None
    for name in names:
        try:
            role_enum = RoleEnum(name)
        except ValueError:
            continue
        r = rank.get(role_enum)
        if r is None:
            continue
        if best_rank is None or r < best_rank:
            best_rank = r
            best_name = role_enum
    return best_name


def user_role_on_customer(user, customer) -> RoleEnum | None:
    """Highest-ranked active customer role this user has on ``customer``."""
    return _first_role(user, customer, _CUSTOMER_ROLE_RANK)


def user_role_on_project(user, project) -> RoleEnum | None:
    """Highest-ranked active project role this user has on ``project``."""
    return _first_role(user, project, _PROJECT_ROLE_RANK)
