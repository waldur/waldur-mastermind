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

    # Each namespace is optional but all-or-nothing (a pool may manage UIDs only,
    # GIDs only, or both); at least one must be managed.
    managed = []
    for ns in NAMESPACES:
        min_v = getattr(pool, f"min_{ns}")
        max_v = getattr(pool, f"max_{ns}")
        next_v = getattr(pool, f"next_{ns}")
        if min_v is None and max_v is None:
            continue
        if min_v is None or max_v is None:
            raise ValidationError(
                _("Set both min and max for the %(ns)s namespace, or neither.")
                % {"ns": ns.upper()}
            )
        managed.append(ns)
        if not (
            models.PosixIdPool.MIN_ID <= min_v <= max_v <= models.PosixIdPool.MAX_ID
        ):
            raise ValidationError(
                _("%(ns)s bounds must satisfy %(min)s <= min <= max <= %(max)s.")
                % {
                    "ns": ns.upper(),
                    "min": models.PosixIdPool.MIN_ID,
                    "max": models.PosixIdPool.MAX_ID,
                }
            )
        if next_v is not None and not (min_v <= next_v <= max_v + 1):
            raise ValidationError(
                _("%(ns)s next pointer %(next)s is outside [%(min)s, %(max)s+1].")
                % {"ns": ns.upper(), "next": next_v, "min": min_v, "max": max_v}
            )

    if not managed:
        raise ValidationError(
            _("At least one of the UID or GID ranges must be defined.")
        )

    # Provider-wide non-overlap, checked per managed namespace: a UID interval may
    # overlap a GID interval (the historical initial_uidnumber == initial_primary
    # default), but two pools of one provider must not overlap within the same
    # namespace — otherwise the allocator could hand the same number to two
    # consumers that share the provider's single LDAP/GLAuth directory.
    siblings = _provider_pools(pool.customer.id).exclude(pk=pool.pk)
    for ns in managed:
        min_v = getattr(pool, f"min_{ns}")
        max_v = getattr(pool, f"max_{ns}")
        conflict = siblings.filter(
            **{
                f"min_{ns}__isnull": False,
                f"min_{ns}__lte": max_v,
                f"max_{ns}__gte": min_v,
            }
        ).first()
        if conflict is not None:
            raise ValidationError(
                _(
                    "%(ns)s range [%(min)s-%(max)s] overlaps the %(ns)s range "
                    "[%(cmin)s-%(cmax)s] of another pool (%(scope)s) of the same "
                    "service provider."
                )
                % {
                    "ns": ns.upper(),
                    "min": min_v,
                    "max": max_v,
                    "cmin": getattr(conflict, f"min_{ns}"),
                    "cmax": getattr(conflict, f"max_{ns}"),
                    "scope": conflict.scope,
                }
            )


def resolve(offering: models.Offering) -> models.PosixIdPool | None:
    """The pool that governs an offering: its own override, else the provider's."""
    return models.PosixIdPool.resolve(offering)


def principal_filter(consumer) -> dict:
    """Lookup keys identifying the principal that owns ``consumer``'s identity.

    An offering user's identity belongs to the Waldur **user**, so every offering
    account of that user which resolves to the same pool shares one UID and one
    primary GID. Robot accounts and groups have no user behind them, so they stay
    keyed on the consumer row itself.
    """
    if isinstance(consumer, models.OfferingUser):
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


def _next_value(pool: models.PosixIdPool, namespace: str) -> int | None:
    """Lowest value to hand out for ``namespace`` in a locked pool, or ``None``.

    Released values in bounds are recycled first (auto-recycle policy), lowest
    first; otherwise the high-water mark ``next_*`` is used, skipping any value
    held by an in-range manual override. ``None`` means the pool is exhausted.

    A released row flagged ``recyclable=False`` is skipped: the retrofit and the
    re-point action free values that are still stamped on files on the provider's
    filesystem, and handing such a number to a different user is a security
    problem. An operator returns them to circulation deliberately.
    """
    min_v = getattr(pool, f"min_{namespace}")
    max_v = getattr(pool, f"max_{namespace}")
    active = models.PosixIdentity.objects.filter(
        pool=pool, released_at__isnull=True, **{f"{namespace}__isnull": False}
    )

    # 1) Recycle: the lowest released value, in bounds, that is not currently
    #    held by an active identity. A released row leaves the partial-unique
    #    active set, so reissuing its value is safe.
    recycled = (
        models.PosixIdentity.objects.filter(
            pool=pool,
            released_at__isnull=False,
            recyclable=True,
            **{f"{namespace}__gte": min_v, f"{namespace}__lte": max_v},
        )
        .filter(~Exists(active.filter(**{namespace: OuterRef(namespace)})))
        .order_by(namespace)
        .values_list(namespace, flat=True)
        .first()
    )
    if recycled is not None:
        return recycled

    # 2) High-water mark, skipping any value already held by an in-range override
    #    (an override does not advance the counter, so it leaves a hole below it).
    #    Fetch the override-held values at or above the mark in a single query
    #    rather than probing the DB per candidate while holding the pool lock.
    start = getattr(pool, f"next_{namespace}")
    taken = set(
        active.filter(
            **{f"{namespace}__gte": start, f"{namespace}__lte": max_v}
        ).values_list(namespace, flat=True)
    )
    value = start
    while value in taken:
        value += 1
    if value > max_v:
        return None
    setattr(pool, f"next_{namespace}", value + 1)
    pool.save(update_fields=[f"next_{namespace}"])
    return value


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
        consumer, models.OfferingUser
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
        value = _next_value(pool, namespace)
        if value is None:
            raise PosixIdPoolExhausted(
                f"The POSIX {namespace.upper()} pool for offering "
                f"{offering.uuid.hex} is exhausted."
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
    if isinstance(consumer, models.OfferingUser):
        # The consumer-scoped pass above is not dead code for offering users:
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
    """Per-namespace capacity, active count and utilization for a pool.

    ``used`` counts principals — a user with accounts on several offerings of the
    provider consumes one UID, not one per offering. A namespace the pool does
    not manage is reported as ``None``.
    """
    stats = {"utilization_threshold": config.POSIX_ID_POOL_UTILIZATION_THRESHOLD}
    for ns in NAMESPACES:
        if not pool.manages(ns):
            stats[ns] = None
            continue
        min_v = getattr(pool, f"min_{ns}")
        max_v = getattr(pool, f"max_{ns}")
        next_v = getattr(pool, f"next_{ns}")
        capacity = max_v - min_v + 1
        used = models.PosixIdentity.objects.filter(
            pool=pool, released_at__isnull=True, **{f"{ns}__isnull": False}
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
