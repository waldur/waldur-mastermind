"""Shared helpers for ACCOUNT-category chat tools.

Every nav tool filters its queryset through the helpers here so that
permission scoping, UUID/name search and serialization stay in one
place. Tools never touch ``filter_queryset_for_user`` directly.
"""

import uuid as uuid_module
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, QuerySet

from waldur_core.logging.models import Event
from waldur_core.logging.utils import exclude_hidden_events, filter_events_for_scope
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace.models import Resource


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


def decimal_or_none(value: Decimal | None) -> str | None:
    """Fixed-point string for a Decimal, as REST prints it; None stays None.

    str() switches to exponent form for small values: a zero cost stored with
    10 places prints as "0E-10".
    """
    return None if value is None else f"{value:f}"


def iso_or_none(value) -> str | None:
    """ISO 8601 string for a date or datetime, or None when unset."""
    return value.isoformat() if value else None


def unknown_arguments(arguments: dict, definition) -> str:
    """An error naming arguments the tool's inputSchema lacks, else "".

    Dropping a misnamed filter would present an unfiltered list as filtered.
    """
    allowed = definition.inputSchema.get("properties", {})
    unknown = sorted(set(arguments) - set(allowed))
    if not unknown:
        return ""
    return (
        f"Unknown argument: {', '.join(unknown)}. "
        f"Valid arguments: {', '.join(allowed)}."
    )


def text_arguments(arguments: dict, *names: str) -> tuple[dict[str, str], str]:
    """Stripped string values of ``names``, plus an error when one is not text.

    A missing or null argument reads as "". The executor does not check
    arguments against inputSchema, so a number or list must not reach
    ``.strip()`` or the ORM.
    """
    values = {}
    for name in names:
        value = arguments.get(name)
        if value is None:
            value = ""
        if not isinstance(value, str):
            return {}, f"{name} must be a string."
        # PostgreSQL text cannot hold NUL, so the query would fail.
        if "\x00" in value:
            return {}, f"{name} must not contain a NUL character."
        values[name] = value.strip()
    return values, ""


def string_list(value) -> list[str] | None:
    """``value`` as a list of strings, or None when it is not one.

    A bare string counts as a one-item list; a missing value as empty.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


# How to get a UUID when the model passed a name instead.
_UUID_HINTS = {
    "resource_uuid": "Look the resource up with get_resource(resource_name=...) "
    "to get its UUID.",
    "project_uuid": "Pass the name as project_name instead.",
    "customer_uuid": "Pass the name as customer_name instead.",
    "offering_uuid": "Pass the name as offering_name instead.",
    "order_uuid": "Find the order's UUID with list_orders first.",
}


def invalid_uuid_message(name: str, value: str) -> str:
    """Validation summary for a malformed UUID argument, with a way forward."""
    # Cut the echo: arguments are not length-limited.
    message = f"Invalid UUID for {name}: {value[:64]}."
    hint = _UUID_HINTS.get(name)
    return f"{message} {hint}" if hint else message


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


def user_consumer_resources(user) -> QuerySet:
    """Resources the user can see as a consumer, as in the REST consumer list."""
    # filter_for_service_consumer returns every resource to anonymous users.
    if not user.is_authenticated:
        return Resource.objects.none()
    return Resource.objects.all().filter_for_service_consumer(user)


def scope_events(user, scope) -> QuerySet | None:
    """Events filed on ``scope``, as ``/api/events/?scope=`` lists them.

    None when the user may not read the scope's events at all, so callers can
    tell hidden events from an empty history.
    """
    events = filter_events_for_scope(user, scope, Event.objects.all())
    if events is None:
        return None
    return exclude_hidden_events(user, events.order_by("-created", "id"))


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
