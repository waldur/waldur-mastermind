"""POSIX UID/GID pool resolution and allocation.

A :class:`marketplace.models.PosixIdPool` reserves a UID range and a GID range
for a service provider (the default) or, as an override, a single offering. The
allocator hands out values under a row lock on the pool and records each one in
the :class:`marketplace.models.PosixIdentity` table — the source of truth.

An identity belongs to a *principal*, not to a single account: for offering users
the principal is the Waldur user, so all of that user's accounts on offerings
resolving to the same pool share one UID and one primary GID. Robot accounts and
groups have no user behind them and stay per-consumer.

Consumers keep the value projected into their ``backend_metadata`` (``uidnumber``
/ ``primarygroup`` for offering users and robot accounts, ``gid`` for groups) so
the GLAuth rendering and site-agent contracts stay unchanged.

POSIX has exactly two numeric namespaces, UID and GID; they are independent, so a
UID and a GID may legally share a number. When no pool resolves for an offering,
allocation is skipped (the account gets no UID/GID until a pool is configured) —
there is no legacy fallback.
"""

import logging

from constance import config
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from waldur_core.core.exceptions import IncorrectStateException

from . import models

logger = logging.getLogger(__name__)

UID = "uid"
GID = "gid"
NAMESPACES = (UID, GID)
# A pool may reserve a second GID range for provider project groups. Its values
# are GIDs like any other (recorded in ``PosixIdentity.gid``); only the range
# they are drawn from differs.
GROUP_GID = "group_gid"
RANGES = (UID, GID, GROUP_GID)


def column(range_name: str) -> str:
    """The ``PosixIdentity`` column a range's values are recorded in."""
    return GID if range_name == GROUP_GID else range_name


class PosixIdPoolExhausted(IncorrectStateException):
    default_detail = _("POSIX ID pool is exhausted.")


class PosixIdValueConflict(Exception):
    """A specific POSIX id is already actively allocated to another consumer.

    Raised by the manual-override path; the API layer maps it to HTTP 400.
    """


def _provider_pools(customer_id):
    """All pools belonging to the given provider customer (default + overrides)."""
    return models.PosixIdPool.objects.filter(
        Q(service_provider__customer_id=customer_id)
        | Q(offering__customer_id=customer_id)
    )


def validate_pool(pool: models.PosixIdPool) -> None:
    """Bounds, exactly-one-scope and provider-wide per-namespace overlap checks.

    Shared by ``PosixIdPool.clean()`` and the API serializer. Raises
    ``django.core.exceptions.ValidationError``.
    """
    has_provider = bool(pool.service_provider_id or pool.service_provider)
    has_offering = bool(pool.offering_id or pool.offering)
    if has_provider == has_offering:
        raise ValidationError(
            _("Exactly one of service_provider or offering must be set.")
        )

    # Each range is optional but all-or-nothing (a pool may manage UIDs only,
    # GIDs only, or both); at least one of UID and GID must be managed.
    managed = []
    for ns in RANGES:
        min_v = getattr(pool, f"min_{ns}")
        max_v = getattr(pool, f"max_{ns}")
        next_v = getattr(pool, f"next_{ns}")
        if min_v is None and max_v is None:
            continue
        if min_v is None or max_v is None:
            raise ValidationError(
                _("Set both min and max for the %(ns)s namespace, or neither.")
                % {"ns": range_label(ns)}
            )
        managed.append(ns)
        if not (
            models.PosixIdPool.MIN_ID <= min_v <= max_v <= models.PosixIdPool.MAX_ID
        ):
            raise ValidationError(
                _("%(ns)s bounds must satisfy %(min)s <= min <= max <= %(max)s.")
                % {
                    "ns": range_label(ns),
                    "min": models.PosixIdPool.MIN_ID,
                    "max": models.PosixIdPool.MAX_ID,
                }
            )
        if next_v is not None and not (min_v <= next_v <= max_v + 1):
            raise ValidationError(
                _("%(ns)s next pointer %(next)s is outside [%(min)s, %(max)s+1].")
                % {"ns": range_label(ns), "next": next_v, "min": min_v, "max": max_v}
            )

    if not set(managed) & set(NAMESPACES):
        raise ValidationError(
            _("At least one of the UID or GID ranges must be defined.")
        )

    if GROUP_GID in managed and has_offering:
        raise ValidationError(
            _(
                "Only a service provider's pool may have a project group range: "
                "project groups belong to the whole provider."
            )
        )

    # The group range holds GIDs too, so it must not overlap the pool's own GID
    # range: a project group and a user's primary GID would share a number.
    if GID in managed and GROUP_GID in managed:
        if pool.min_group_gid <= pool.max_gid and pool.max_group_gid >= pool.min_gid:
            raise ValidationError(
                _(
                    "Group GID range [%(min)s-%(max)s] overlaps the pool's GID "
                    "range [%(gmin)s-%(gmax)s]."
                )
                % {
                    "min": pool.min_group_gid,
                    "max": pool.max_group_gid,
                    "gmin": pool.min_gid,
                    "gmax": pool.max_gid,
                }
            )

    # Provider-wide non-overlap, checked per managed namespace: a UID interval may
    # overlap a GID interval (the historical initial_uidnumber == initial_primary
    # default), but two pools of one provider must not overlap within the same
    # namespace — otherwise the allocator could hand the same number to two
    # consumers that share the provider's single LDAP/GLAuth directory.
    # A range is compared with every sibling range recorded in the same column,
    # so a group GID range clashes with another pool's GID range as well as with
    # its group GID range.
    siblings = _provider_pools(pool.customer.id).exclude(pk=pool.pk)
    for ns in managed:
        min_v = getattr(pool, f"min_{ns}")
        max_v = getattr(pool, f"max_{ns}")
        for other in RANGES:
            if column(other) != column(ns):
                continue
            conflict = siblings.filter(
                **{
                    f"min_{other}__isnull": False,
                    f"min_{other}__lte": max_v,
                    f"max_{other}__gte": min_v,
                }
            ).first()
            if conflict is not None:
                raise ValidationError(
                    _(
                        "%(ns)s range [%(min)s-%(max)s] overlaps the %(other)s "
                        "range [%(cmin)s-%(cmax)s] of another pool (%(scope)s) of "
                        "the same service provider."
                    )
                    % {
                        "ns": range_label(ns),
                        "other": range_label(other),
                        "min": min_v,
                        "max": max_v,
                        "cmin": getattr(conflict, f"min_{other}"),
                        "cmax": getattr(conflict, f"max_{other}"),
                        "scope": conflict.scope,
                    }
                )
    _check_foreign_identities(pool, managed, siblings)


def _check_foreign_identities(pool, managed, siblings) -> None:
    """Refuse a range holding a value another pool of the provider handed out.

    That pool's ranges no longer contain it (or the overlap check would have
    caught it), but the value is still in use; this pool would hand it out
    again.
    """
    for ns in managed:
        col = column(ns)
        held = (
            models.PosixIdentity.objects.filter(
                pool__in=siblings,
                released_at__isnull=True,
                **{
                    f"{col}__gte": getattr(pool, f"min_{ns}"),
                    f"{col}__lte": getattr(pool, f"max_{ns}"),
                },
            )
            .order_by(col)
            .select_related("user")
        )
        if held.exists():
            raise ValidationError(
                _(
                    "The %(range)s range [%(min)s-%(max)s] contains values another "
                    "pool of the service provider has allocated: %(values)s."
                )
                % {
                    "range": range_label(ns),
                    "min": getattr(pool, f"min_{ns}"),
                    "max": getattr(pool, f"max_{ns}"),
                    "values": describe_values(held, col),
                }
            )


def range_label(range_name: str) -> str:
    """How a range is named in messages: "UID", "GID" or "project group"."""
    return "project group" if range_name == GROUP_GID else range_name.upper()


def describe_holder(identity) -> str:
    """Who holds an identity's value, for a message an operator reads."""
    if identity.user_id:
        return _("the account of user %(user)s") % {"user": identity.user.username}
    consumer = identity.consumer
    if isinstance(consumer, models.ServiceProviderProjectGroup):
        return _("project group %(name)s") % {"name": consumer.name}
    if isinstance(consumer, models.RobotAccount):
        return _("robot account %(name)s") % {"name": consumer.username}
    if consumer is None:
        return _("a deleted consumer")
    return _("another group")


def describe_values(identities, namespace: str, limit: int = 3) -> str:
    """``"20001 (project group alpha), 20002 (...)"`` and how many more."""
    identities = list(identities)
    parts = [
        f"{getattr(identity, namespace)} ({describe_holder(identity)})"
        for identity in identities[:limit]
    ]
    if len(identities) > limit:
        parts.append(_("and %(count)s more") % {"count": len(identities) - limit})
    return ", ".join(parts)


def resolve(offering: models.Offering) -> models.PosixIdPool | None:
    """The pool that governs an offering: its own override, else the provider's."""
    return models.PosixIdPool.resolve(offering)


def principal_filter(consumer) -> dict:
    """Lookup keys identifying the principal that owns ``consumer``'s identity.

    A user account's identity belongs to the Waldur **user**, so every account of
    that user which resolves to the same pool shares one UID and one primary GID.
    That covers both scopes: an OfferingUser and the ServiceProviderAccount backing
    it are the same principal and must not be handed different numbers. Robot
    accounts and groups have no user behind them, so they stay keyed on the
    consumer row itself.
    """
    if isinstance(consumer, models.BaseAccount):
        return {"user_id": consumer.user_id}
    ct = ContentType.objects.get_for_model(consumer.__class__)
    return {"content_type": ct, "object_id": consumer.pk}


def _active_identity(pool, consumer):
    """The consumer's active identity **in this pool**, if any.

    Scoped to the pool because one user may hold accounts with several providers,
    each drawing from its own pool.
    """
    return models.PosixIdentity.objects.filter(
        pool=pool, released_at__isnull=True, **principal_filter(consumer)
    ).first()


def _get_or_create_active_identity(consumer, pool, offering):
    identity, _created = models.PosixIdentity.objects.get_or_create(
        pool=pool,
        released_at__isnull=True,
        defaults={"offering": offering},
        **principal_filter(consumer),
    )
    return identity


def _candidate_value(
    pool: models.PosixIdPool, namespace: str, recycle: bool = True
) -> tuple:
    """``(value, from_counter)`` for the next value of a range, taking nothing.

    ``namespace`` is one of :data:`RANGES`; the group GID range is searched
    like the others, against the GID column.

    Released values in bounds are recycled first (auto-recycle policy), lowest
    first; otherwise the high-water mark ``next_*`` is used, skipping any value
    held by an in-range manual override. ``value`` is ``None`` when the pool is
    exhausted; ``from_counter`` says whether taking it advances the mark.

    A released row flagged ``recyclable=False`` is skipped: the retrofit and the
    re-point action free values that are still stamped on files on the provider's
    filesystem, and handing such a number to a different user is a security
    problem. An operator returns them to circulation deliberately. For the same
    reason the high-water mark steps over such a value too: a pinned value
    above the mark that was later released must not be reached by counting.
    """
    range_name = namespace
    min_v = getattr(pool, f"min_{range_name}")
    max_v = getattr(pool, f"max_{range_name}")
    namespace = column(range_name)
    active = models.PosixIdentity.objects.filter(
        pool=pool, released_at__isnull=True, **{f"{namespace}__isnull": False}
    )

    withheld = models.PosixIdentity.objects.filter(
        pool=pool, released_at__isnull=False, recyclable=False
    )

    # 1) Recycle: the lowest released value, in bounds, that is not currently
    #    held by an active identity. A released row leaves the partial-unique
    #    active set, so reissuing its value is safe -- unless the same value was
    #    also released as withheld (an override or a re-point moved off it
    #    later): one withheld release keeps the value out of circulation.
    recycled = None
    if recycle:
        recycled = (
            models.PosixIdentity.objects.filter(
                pool=pool,
                released_at__isnull=False,
                recyclable=True,
                **{f"{namespace}__gte": min_v, f"{namespace}__lte": max_v},
            )
            .filter(~Exists(active.filter(**{namespace: OuterRef(namespace)})))
            .filter(~Exists(withheld.filter(**{namespace: OuterRef(namespace)})))
            .order_by(namespace)
            .values_list(namespace, flat=True)
            .first()
        )
    if recycled is not None:
        return recycled, False

    # 2) High-water mark, skipping any value already held by an in-range override
    #    (an override does not advance the counter, so it leaves a hole below it).
    #    Fetch the override-held values at or above the mark in a single query
    #    rather than probing the DB per candidate while holding the pool lock.
    start = getattr(pool, f"next_{range_name}")
    in_window = {f"{namespace}__gte": start, f"{namespace}__lte": max_v}
    taken = set(active.filter(**in_window).values_list(namespace, flat=True))
    taken |= set(withheld.filter(**in_window).values_list(namespace, flat=True))
    value = start
    while value in taken:
        value += 1
    if value > max_v:
        return None, False
    return value, True


def _next_value(
    pool: models.PosixIdPool, namespace: str, recycle: bool = True
) -> int | None:
    """Lowest value to hand out for ``namespace`` in a locked pool, or ``None``.

    ``namespace`` is a range name: the counter advanced is that range's. See
    :func:`_candidate_value`; a value from the high-water mark advances it.
    """
    value, from_counter = _candidate_value(pool, namespace, recycle=recycle)
    if from_counter:
        setattr(pool, f"next_{namespace}", value + 1)
        pool.save(update_fields=[f"next_{namespace}"])
    return value


def peek_next_value(pool: models.PosixIdPool | None, namespace: str) -> int | None:
    """The value the pool would hand out next for ``namespace``, taking nothing.

    For previews only: nothing is locked, so a concurrent allocation may take
    the value first. ``None`` when there is no pool, it does not manage the
    namespace, or it is exhausted.
    """
    if pool is None or not pool.manages(namespace):
        return None
    return _candidate_value(pool, namespace)[0]


def allocate(offering: models.Offering, namespace: str, consumer) -> int | None:
    """Allocate a ``namespace`` value for ``consumer`` and record it.

    Returns ``None`` when no pool resolves for the offering, or the resolved pool
    does not manage this namespace (e.g. a GID-only pool for offerings whose UIDs
    come from an external identity source) — the caller skips the assignment.
    Raises :class:`PosixIdPoolExhausted` (HTTP 409) when the pool is exhausted.

    Idempotent: if the principal already holds a value for this namespace in the
    resolved pool, that value is returned, so a retried provisioning does not
    leak identifiers — and a second offering of the same provider hands the user
    the value already allocated for the first one.
    """
    if isinstance(
        consumer, models.BaseAccount
    ) and namespace not in pool_sourced_namespaces(offering):
        # The offering takes this identifier from the user rather than from the
        # allocator (or manages no POSIX account at all). Allocating here would
        # hand out a pool value that then overwrites the external one — the
        # re-point action reaches this path directly, not only via
        # ``setup_linux_related_data``.
        return None

    # The pool is resolved from the offering on every call, so an offering-level
    # override always wins over the provider default. Pre-existing accounts are
    # not moved implicitly when an override appears later — that is what the
    # explicit re-point action is for.
    pool = models.PosixIdPool.resolve(offering)
    if pool is None or not pool.manages(namespace):
        return None

    return _allocate_in_pool(
        pool, namespace, consumer, offering, f"offering {offering.uuid.hex}"
    )


def _allocate_in_pool(pool, range_name, consumer, offering, label, recycle=True) -> int:
    """Allocate from ``range_name`` of ``pool`` for ``consumer``; idempotent."""
    namespace = column(range_name)
    existing = _active_identity(pool, consumer)
    if existing is not None and getattr(existing, namespace) is not None:
        return getattr(existing, namespace)

    with transaction.atomic():
        pool = models.PosixIdPool.objects.select_for_update().get(pk=pool.pk)
        identity = _get_or_create_active_identity(consumer, pool, offering)
        # Re-check under the lock: a concurrent allocation for the same consumer
        # and namespace may have populated the value while we waited for the lock.
        # Doing this before consuming a value keeps the call idempotent and avoids
        # leaking a counter position.
        current = getattr(identity, namespace)
        if current is not None:
            return current
        value = _next_value(pool, range_name, recycle=recycle)
        if value is None:
            raise PosixIdPoolExhausted(
                f"The POSIX {range_label(range_name)} pool for {label} is exhausted."
            )
        setattr(identity, namespace, value)
        identity.save(update_fields=[namespace])
        return value


def set_value(consumer, namespace: str, value: int, offering: models.Offering) -> None:
    """Pin ``value`` as the principal's ``namespace`` id (manual override).

    For an offering user the principal is the Waldur user, so the pin applies
    across every offering of the provider that resolves to the same pool — not
    to that single offering account.

    Locks the same pool row the allocator locks, so an override and a concurrent
    automatic allocation serialize. The value must fall inside the resolved
    pool's range. Raises ``ValidationError`` when no pool resolves or the value
    is out of range, and :class:`PosixIdValueConflict` when the value is already
    held by another active identity (the DB unique constraint is the backstop).
    """
    pool = models.PosixIdPool.resolve(offering)
    if pool is None:
        raise ValidationError(_("No POSIX ID pool is configured for this offering."))
    if not pool.manages(namespace):
        raise ValidationError(
            _("The offering's POSIX ID pool does not manage %(ns)s values.")
            % {"ns": namespace.upper()}
        )

    min_v = getattr(pool, f"min_{namespace}")
    max_v = getattr(pool, f"max_{namespace}")
    if not (min_v <= value <= max_v):
        raise ValidationError(
            _("%(value)s is outside the pool's %(ns)s range [%(min)s-%(max)s].")
            % {"value": value, "ns": namespace.upper(), "min": min_v, "max": max_v}
        )

    with transaction.atomic():
        pool = models.PosixIdPool.objects.select_for_update().get(pk=pool.pk)
        identity = _get_or_create_active_identity(consumer, pool, offering)
        if getattr(identity, namespace) == value:
            return
        setattr(identity, namespace, value)
        try:
            with transaction.atomic():
                identity.save(update_fields=[namespace])
        except IntegrityError:
            raise PosixIdValueConflict


def provider_pool(service_provider) -> models.PosixIdPool | None:
    """The pool a provider's project groups draw from: the provider's own.

    Offering-level override pools are not consulted: a project group belongs to
    the whole provider, not to one offering.
    """
    return models.PosixIdPool.objects.filter(service_provider=service_provider).first()


def group_range(pool: models.PosixIdPool | None) -> str | None:
    """The range of ``pool`` that supplies project group GIDs, if any.

    The group GID range when the pool reserves one, else the GID range shared
    with users' primary GIDs.
    """
    if pool is None:
        return None
    for range_name in (GROUP_GID, GID):
        if pool.manages(range_name):
            return range_name
    return None


def allocate_project_group_gid(group: models.ServiceProviderProjectGroup):
    """Allocate a GID for a provider project group; ``None`` without a pool.

    Idempotent: a group that already holds a GID in the provider's pool keeps
    it. Raises :class:`PosixIdPoolExhausted` when the range is exhausted.
    """
    pool = provider_pool(group.service_provider)
    range_name = group_range(pool)
    if range_name is None:
        return None
    # Never recycled: a released GID may be a departed user's primary group,
    # still on files, and must not become a project's group.
    return _allocate_in_pool(
        pool,
        range_name,
        group,
        None,
        f"service provider {group.service_provider.uuid.hex}",
        recycle=False,
    )


def set_project_group_gid(
    group: models.ServiceProviderProjectGroup,
    value: int,
    allow_outside_range: bool = False,
) -> int | None:
    """Pin ``value`` as the group's GID; returns the GID it held before.

    A previous GID is released but withheld from recycling: files on the
    provider's storage may still carry it. Raises ``ValidationError`` when the
    provider has no pool with a GID range, when the value lies outside the
    range project groups draw from (unless ``allow_outside_range``), or when it
    lies inside a GID range of another pool of the provider; raises
    :class:`PosixIdValueConflict` when another consumer holds the value in any
    of the provider's pools.
    """
    pool = provider_pool(group.service_provider)
    range_name = group_range(pool)
    if range_name is None:
        raise ValidationError(
            _("The service provider has no POSIX ID pool with a GID range.")
        )
    min_v = getattr(pool, f"min_{range_name}")
    max_v = getattr(pool, f"max_{range_name}")
    if not allow_outside_range and not (min_v <= value <= max_v):
        raise ValidationError(
            _(
                "%(value)s is outside the pool's %(range)s range "
                "[%(min)s-%(max)s]. Set allow_outside_range to use it anyway."
            )
            % {
                "value": value,
                "range": range_label(range_name),
                "min": min_v,
                "max": max_v,
            }
        )

    customer_id = group.service_provider.customer_id
    # A value inside another pool's GID range would later be handed out by that
    # pool's allocator, which only sees its own identities.
    for other in (GID, GROUP_GID):
        foreign = (
            _provider_pools(customer_id)
            .exclude(pk=pool.pk)
            .filter(**{f"min_{other}__lte": value, f"max_{other}__gte": value})
            .first()
        )
        if foreign is not None:
            raise ValidationError(
                _(
                    "%(value)s is inside the %(range)s range of another pool "
                    "(%(scope)s) of the service provider."
                )
                % {"value": value, "range": range_label(other), "scope": foreign.scope}
            )

    ct = ContentType.objects.get_for_model(group.__class__)
    with transaction.atomic():
        pool = models.PosixIdPool.objects.select_for_update().get(pk=pool.pk)
        holder = (
            models.PosixIdentity.objects.filter(
                pool__in=_provider_pools(customer_id),
                released_at__isnull=True,
                gid=value,
            )
            .exclude(content_type=ct, object_id=group.pk)
            .first()
        )
        if holder is not None:
            raise PosixIdValueConflict(
                _("%(value)s is already used by %(holder)s.")
                % {"value": value, "holder": describe_holder(holder)}
            )
        identity = _active_identity(pool, group)
        if identity is not None and identity.gid is not None:
            previous = identity.gid
        else:
            # A GID without a ledger row (imported, or from a pool that is
            # gone) is still the group's previous GID.
            previous = group.gid
        if previous == value:
            if identity is None:
                identity = models.PosixIdentity(
                    pool=pool, content_type=ct, object_id=group.pk, gid=value
                )
                identity.save()
            return previous
        if identity is not None and identity.gid is not None:
            # Keep the old row as the audit record of the released value.
            identity.released_at = timezone.now()
            identity.recyclable = False
            identity.save(update_fields=["released_at", "recyclable"])
            identity = None
        elif previous is not None:
            # Record the released value so that the allocator steps over it.
            models.PosixIdentity.objects.create(
                pool=pool,
                content_type=ct,
                object_id=group.pk,
                gid=previous,
                released_at=timezone.now(),
                recyclable=False,
            )
        if identity is None:
            identity = models.PosixIdentity(
                pool=pool, content_type=ct, object_id=group.pk
            )
        identity.gid = value
        try:
            with transaction.atomic():
                identity.save()
        except IntegrityError:
            raise PosixIdValueConflict(
                _("%(value)s was taken by another consumer meanwhile.")
                % {"value": value}
            )
        group.gid = value
        group.save(update_fields=["gid", "modified"])
    return previous


def release_project_group_gid(group) -> int:
    """Release a deleted project group's GID without making it recyclable."""
    ct = ContentType.objects.get_for_model(group.__class__)
    return models.PosixIdentity.objects.filter(
        content_type=ct, object_id=group.pk, released_at__isnull=True
    ).update(released_at=timezone.now(), recyclable=False)


def pool_sourced_namespaces(offering) -> set:
    """Namespaces an offering's **accounts** actually draw from the pool.

    Two per-offering plugin options narrow this, and both matter because the
    pool is normally per-provider while they are not:

    * ``enable_posix_account=False`` opts the offering out of POSIX accounts
      entirely, so it neither allocates nor keeps a value reserved;
    * ``uid_source`` / ``gid_source`` set to ``user_attribute`` mean the value
      comes from the Waldur user (an OIDC claim, typically), not from Waldur's
      allocator — a legal mix, since offering A of a provider may allocate UIDs
      from the shared pool while offering B takes them from the claim.

    A namespace sourced externally must never be allocated, projected onto by a
    pin or the retrofit, or counted as holding the pool's value. Mirrors the
    gate in ``utils.setup_linux_related_data``, which is where the external
    value is read.

    Group GIDs are not covered: a project or role group has no user behind it,
    so ``gid_source`` says nothing about it and its GID always comes from the
    pool.
    """
    plugin_options = offering.plugin_options or {}
    if not plugin_options.get("enable_posix_account", True):
        return set()
    return {
        namespace
        for namespace in NAMESPACES
        if plugin_options.get(f"{namespace}_source", "pool") == "pool"
    }


def _pool_ids_still_in_use_by(user_id: int) -> set:
    """Pools the user still holds a POSIX-relevant offering account in."""
    pool_ids = set()
    resolved: dict[int, int | None] = {}
    offering_users = models.OfferingUser.objects.filter(user_id=user_id).select_related(
        "offering"
    )
    for offering_user in offering_users:
        offering_id = offering_user.offering_id
        if offering_id not in resolved:
            offering = offering_user.offering
            pool = (
                models.PosixIdPool.resolve(offering)
                if pool_sourced_namespaces(offering)
                else None
            )
            resolved[offering_id] = pool.pk if pool is not None else None
        pool_id = resolved[offering_id]
        if pool_id is not None:
            pool_ids.add(pool_id)

    # A provider account outlives the offering accounts that read through it: it
    # is only deleted once the user has lost access to every offering of that
    # provider. Until then it still holds the reservation, so its pool must not
    # be treated as free just because the last OfferingUser row went away.
    provider_accounts = models.ServiceProviderAccount.objects.filter(
        user_id=user_id
    ).select_related("service_provider")
    for account in provider_accounts:
        pool = models.PosixIdPool.objects.filter(
            service_provider=account.service_provider
        ).first()
        if pool is not None:
            pool_ids.add(pool.pk)
    return pool_ids


def release_posix_allocations(consumer) -> int:
    """Mark the deleted consumer's active POSIX identity as released.

    A user identity is shared, so deleting one offering user releases nothing
    while another offering account of that user still resolves to the same pool;
    only the last one frees the value. Robot accounts and groups are released
    per consumer.

    Released rows are retained for audit and become recycle candidates for the
    next allocation from the same pool and namespace.
    """
    ct = ContentType.objects.get_for_model(consumer.__class__)
    count = models.PosixIdentity.objects.filter(
        content_type=ct,
        object_id=consumer.pk,
        released_at__isnull=True,
    ).update(released_at=timezone.now())
    if count:
        logger.info(
            "Released POSIX identity of deleted %s %s",
            consumer.__class__.__name__,
            consumer.pk,
        )
    if isinstance(consumer, models.BaseAccount):
        # The consumer-scoped pass above is not dead code for user accounts:
        # deployments retrofitted by the migration keep the duplicate rows of a
        # (pool, user) group consumer-scoped until the collapse command is run,
        # and those rows belong to one account each.
        count += release_user_allocations(consumer.user_id)
    return count


def release_user_allocations(user_id: int | None, recyclable: bool = True) -> int:
    """Release the user's identities in pools they no longer have an account in.

    Called after an offering user row is deleted, and by the re-point action when
    accounts move to an override pool. Identities in pools that are still
    reachable through another offering account of the same user stay active, so
    the shared UID/GID keeps its reservation. Takes the id rather than the
    instance: during a cascading user deletion the related row may already be
    gone.

    ``recyclable=False`` withholds the freed values from the recycle pool — the
    caller knows they are still stamped on files on the provider's filesystem.
    """
    if user_id is None:
        return 0
    active = models.PosixIdentity.objects.filter(
        user_id=user_id, released_at__isnull=True
    )
    if not active.exists():
        # Runs from post_delete for every deleted offering user, so keep the
        # common case to a single indexed lookup instead of walking the user's
        # accounts and resolving a pool per offering.
        return 0
    in_use = _pool_ids_still_in_use_by(user_id)
    count = active.exclude(pool_id__in=in_use).update(
        released_at=timezone.now(), recyclable=recyclable
    )
    if count:
        logger.info(
            "Released %s POSIX identity row(s) of user %s: no offering account "
            "of theirs resolves to those pools any more.",
            count,
            user_id,
        )
    return count


def get_pool_stats(pool: models.PosixIdPool) -> dict:
    """Per-range capacity, active count and utilization for a pool.

    ``used`` counts principals — a user with accounts on several offerings of the
    provider consumes one UID, not one per offering — whose value lies in the
    range, so project groups count against the group GID range when the pool
    has one. A range the pool does not manage is reported as ``None``.
    """
    stats = {"utilization_threshold": config.POSIX_ID_POOL_UTILIZATION_THRESHOLD}
    for ns in RANGES:
        if not pool.manages(ns):
            stats[ns] = None
            continue
        min_v = getattr(pool, f"min_{ns}")
        max_v = getattr(pool, f"max_{ns}")
        next_v = getattr(pool, f"next_{ns}")
        capacity = max_v - min_v + 1
        col = column(ns)
        used = models.PosixIdentity.objects.filter(
            pool=pool,
            released_at__isnull=True,
            **{f"{col}__gte": min_v, f"{col}__lte": max_v},
        ).count()
        stats[ns] = {
            "min": min_v,
            "max": max_v,
            "next": next_v,
            "capacity": capacity,
            "used": used,
            "utilization": round(used / capacity * 100, 2) if capacity else 0,
        }
    return stats


# Special POSIX id values worth flagging on a manual override (see systemd's
# UIDS-GIDS guidance): 65534 is the conventional "nobody", 65535 is the 16-bit
# (id_t) -1, and values above 2**31 break software using signed 32-bit ids.
NOBODY_ID = 65534
INT16_MINUS_ONE = 65535
SIGNED_INT32_MAX = 2**31 - 1


def posix_value_advisories(label: str, value: int) -> list[str]:
    """Non-fatal advisories for unusual but in-bounds POSIX id values."""
    if value in (NOBODY_ID, INT16_MINUS_ONE):
        return [
            _(
                "%(label)s %(value)s is a reserved POSIX id (the 'nobody' / -1 "
                "overflow value) and should normally be avoided."
            )
            % {"label": label, "value": value}
        ]
    if value > SIGNED_INT32_MAX:
        return [
            _(
                "%(label)s %(value)s is above 2^31 and may not work with "
                "software that uses signed 32-bit ids."
            )
            % {"label": label, "value": value}
        ]
    return []
