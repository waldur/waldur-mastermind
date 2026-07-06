"""POSIX UID/GID pool resolution and allocation.

A :class:`marketplace.models.PosixIdPool` reserves a UID range and a GID range
for a service provider (the default) or, as an override, a single offering. The
allocator hands out values under a row lock on the pool and records each one in
the :class:`marketplace.models.PosixIdentity` table — the source of truth.
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


def _active_identity(consumer):
    ct = ContentType.objects.get_for_model(consumer.__class__)
    return models.PosixIdentity.objects.filter(
        content_type=ct, object_id=consumer.pk, released_at__isnull=True
    ).first()


def _get_or_create_active_identity(consumer, pool, offering):
    ct = ContentType.objects.get_for_model(consumer.__class__)
    identity, _created = models.PosixIdentity.objects.get_or_create(
        content_type=ct,
        object_id=consumer.pk,
        released_at__isnull=True,
        defaults={"pool": pool, "offering": offering},
    )
    return identity


def _next_value(pool: models.PosixIdPool, namespace: str) -> int | None:
    """Lowest value to hand out for ``namespace`` in a locked pool, or ``None``.

    Released values in bounds are recycled first (auto-recycle policy), lowest
    first; otherwise the high-water mark ``next_*`` is used, skipping any value
    held by an in-range manual override. ``None`` means the pool is exhausted.
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

    Idempotent: if the consumer already holds a value for this namespace, that
    value is returned, so a retried provisioning does not leak identifiers.
    """
    existing = _active_identity(consumer)
    if existing is not None and getattr(existing, namespace) is not None:
        return getattr(existing, namespace)

    # A consumer that already has an identity stays bound to that identity's
    # pool; only a brand-new consumer is placed into the offering's currently
    # resolved pool. This keeps both of a consumer's namespaces in one pool, so
    # the per-pool unique constraints guard the right partition even after the
    # offering's pool scoping changes (e.g. an override pool is added later).
    pool = (
        existing.pool if existing is not None else models.PosixIdPool.resolve(offering)
    )
    if pool is None or not pool.manages(namespace):
        return None

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
    """Pin ``value`` as ``consumer``'s ``namespace`` id (manual override).

    Locks the same pool row the allocator locks, so an override and a concurrent
    automatic allocation serialize. The value must fall inside the resolved
    pool's range. Raises ``ValidationError`` when no pool resolves or the value
    is out of range, and :class:`PosixIdValueConflict` when the value is already
    held by another active identity (the DB unique constraint is the backstop).
    """
    # An override pins a value for an existing account, so it targets that
    # account's current pool; only a consumer without an identity yet falls back
    # to the offering's resolved pool. This keeps the value and the per-pool
    # unique constraint in the same partition.
    existing = _active_identity(consumer)
    pool = (
        existing.pool if existing is not None else models.PosixIdPool.resolve(offering)
    )
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


def release_posix_allocations(consumer) -> int:
    """Mark the deleted consumer's active POSIX identity as released.

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
    return count


def get_pool_stats(pool: models.PosixIdPool) -> dict:
    """Per-namespace capacity, active count and utilization for a pool.

    A namespace the pool does not manage is reported as ``None``.
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
