"""Operator-driven maintenance of POSIX identities.

Two operations live here, both of which rewrite identifiers that a provider's
LDAP tree and filesystem already carry. Neither ever runs by itself:

* **collapse** — the one-off retrofit. Before identities became principal-scoped
  a user with accounts on two offerings of one provider held two identities and
  two different UIDs. This picks one canonical identity per ``(pool, user)``,
  rewrites the other accounts onto it, and reports the UID -> UID map the
  operator needs to drive ``chown`` and the SLURM-side updates.

* **re-point** — applied when an offering gains its own pool after it already
  has accounts. Newly created accounts use the override pool immediately, but
  existing ones keep their provider-pool values until this is run.

Values that stop being allocated are marked released **and** withheld from
recycling (``recyclable=False``): the number is still stamped on files on the
provider's filesystem, and handing it to a different user before those files are
reconciled is a security problem. Returning them to circulation is a separate,
deliberate operator step (see the POSIX identity admin action).
"""

import logging
from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

from . import models, posix_ids

logger = logging.getLogger(__name__)

# backend_metadata key <-> namespace projection, in report order.
METADATA_KEYS = (
    ("uidnumber", posix_ids.UID),
    ("primarygroup", posix_ids.GID),
)


class _Rollback(Exception):
    """Internal sentinel: unwind the preview transaction."""


def _looks_pinned(pool, identity) -> bool:
    """Best-effort guess that a value was set by hand rather than allocated.

    The allocator never issues a value at or above the pool's high-water mark,
    so anything there was pinned through ``set_posix_attributes``. A pin placed
    into a hole *below* the mark is indistinguishable from an allocation; the
    collapse report says so, and the operator reviews it.
    """
    for namespace in posix_ids.NAMESPACES:
        value = getattr(identity, namespace)
        next_value = getattr(pool, f"next_{namespace}")
        if value is not None and next_value is not None and value >= next_value:
            return True
    return False


def _offering_users_in_pool(user_id, pool):
    """The user's offering accounts that resolve to ``pool``."""
    result = []
    for offering_user in models.OfferingUser.objects.filter(
        user_id=user_id
    ).select_related("offering", "user"):
        if not posix_ids.pool_sourced_namespaces(offering_user.offering):
            continue
        resolved = models.PosixIdPool.resolve(offering_user.offering)
        if resolved is not None and resolved.pk == pool.pk:
            result.append(offering_user)
    return result


def project_shared_values(offering_user, pool) -> list:
    """Push the shared identity's values onto the user's other accounts in ``pool``.

    The identity belongs to the user within the pool, so a change made through
    one account — a manual pin, most of all — has to reach the projection every
    other account of that user hands to the site agent. Without this the ledger
    and the directory disagree, which is the divergence this whole feature
    exists to remove. Returns the changes made, for event logging; the account
    the caller is already editing is left to the caller.
    """
    identity = models.PosixIdentity.objects.filter(
        pool=pool,
        released_at__isnull=True,
        **posix_ids.principal_filter(offering_user),
    ).first()
    if identity is None:
        return []
    changes = []
    for sibling in _offering_users_in_pool(offering_user.user_id, pool):
        if sibling.pk == offering_user.pk:
            continue
        sibling_changes = _metadata_changes(sibling, identity.uid, identity.gid)
        if sibling_changes:
            _apply_metadata_changes(sibling, sibling_changes)
            changes.extend(sibling_changes)
    return changes


def _metadata_changes(offering_user, uid, gid):
    """Rows describing how the account's projection has to change.

    Only namespaces this offering draws from the pool are touched: an offering
    whose ``uid_source`` is ``user_attribute`` carries a UID Waldur does not
    own, and a pin or a collapse on a sibling account must not overwrite it.
    """
    metadata = offering_user.backend_metadata or {}
    target = {posix_ids.UID: uid, posix_ids.GID: gid}
    from_pool = posix_ids.pool_sourced_namespaces(offering_user.offering)
    changes = []
    for key, namespace in METADATA_KEYS:
        new_value = target[namespace]
        if new_value is None or namespace not in from_pool:
            continue
        old_value = metadata.get(key)
        old_value = int(old_value) if old_value is not None else None
        if old_value == new_value:
            continue
        changes.append(
            {
                "offering_user_uuid": offering_user.uuid.hex,
                "offering_uuid": offering_user.offering.uuid.hex,
                "offering_name": offering_user.offering.name,
                "user_uuid": offering_user.user.uuid.hex,
                "username": offering_user.user.username,
                "namespace": namespace,
                "metadata_key": key,
                "old_value": old_value,
                "new_value": new_value,
            }
        )
    return changes


def _apply_metadata_changes(offering_user, changes):
    metadata = offering_user.backend_metadata or {}
    for change in changes:
        metadata[change["metadata_key"]] = change["new_value"]
    offering_user.backend_metadata = metadata
    offering_user.save(update_fields=["backend_metadata"])


def emit_change_events(changes):
    """One event per changed offering user, naming every identifier that moved."""
    by_offering_user = defaultdict(list)
    for change in changes:
        by_offering_user[change["offering_user_uuid"]].append(change)
    for offering_user_uuid, rows in by_offering_user.items():
        offering_user = models.OfferingUser.objects.filter(
            uuid=offering_user_uuid
        ).first()
        if offering_user is None:
            continue
        moved = ", ".join(
            f"{row['namespace'].upper()} {row['old_value']} -> {row['new_value']}"
            for row in rows
        )
        event_logger.emit(
            f"POSIX identifiers of user {offering_user.user} in offering "
            f"{offering_user.offering.name} have been changed: {moved}.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
            scopes=[offering_user.offering, offering_user.offering.customer],
        )


def plan_collapse(pools=None) -> dict:
    """Group active user identities by ``(pool, user)`` and pick a canonical one.

    Returns ``{"groups": [...], "withheld": [...]}``. Nothing is written.
    """
    offering_user_ct = ContentType.objects.get_for_model(models.OfferingUser)
    if pools is None:
        pools = models.PosixIdPool.objects.all()
    groups = []
    withheld = []

    for pool in pools.select_related("service_provider__customer", "offering"):
        identities = list(
            models.PosixIdentity.objects.filter(
                pool=pool, released_at__isnull=True
            ).select_related("user")
        )
        consumer_scoped = [
            identity
            for identity in identities
            if identity.content_type_id == offering_user_ct.id
        ]
        owners = dict(
            models.OfferingUser.objects.filter(
                id__in=[identity.object_id for identity in consumer_scoped]
            ).values_list("id", "user_id")
        )

        by_user = defaultdict(list)
        for identity in identities:
            user_id = identity.user_id
            if user_id is None:
                if identity.content_type_id != offering_user_ct.id:
                    # Robot accounts and groups are not user-scoped.
                    continue
                user_id = owners.get(identity.object_id)
            if user_id is not None:
                by_user[user_id].append(identity)

        for user_id, rows in by_user.items():
            rows.sort(key=lambda identity: (identity.created, identity.id))
            pinned = [identity for identity in rows if _looks_pinned(pool, identity)]
            warnings = []
            if len(pinned) > 1:
                canonical = pinned[0]
                warnings.append(
                    _(
                        "%(count)s identities look manually pinned (%(values)s); "
                        "keeping the oldest pinned one. Review before applying."
                    )
                    % {
                        "count": len(pinned),
                        "values": ", ".join(
                            f"uid {identity.uid} / gid {identity.gid}"
                            for identity in pinned
                        ),
                    }
                )
            elif pinned:
                canonical = pinned[0]
            else:
                canonical = rows[0]
            superseded = [identity for identity in rows if identity.pk != canonical.pk]

            changes = []
            for offering_user in _offering_users_in_pool(user_id, pool):
                changes.extend(
                    _metadata_changes(offering_user, canonical.uid, canonical.gid)
                )
            if not superseded and not changes and canonical.user_id:
                continue

            groups.append(
                {
                    "pool": pool,
                    "user_id": user_id,
                    "canonical": canonical,
                    "superseded": superseded,
                    "changes": changes,
                    "warnings": warnings,
                }
            )
            for identity in superseded:
                for namespace in posix_ids.NAMESPACES:
                    value = getattr(identity, namespace)
                    if value is not None:
                        withheld.append({"namespace": namespace, "value": value})

    return {"groups": groups, "withheld": withheld}


def apply_collapse(plan: dict) -> None:
    """Persist a plan produced by :func:`plan_collapse`."""
    for group in plan["groups"]:
        with transaction.atomic():
            # Release the losers first: the canonical row can only take the user
            # FK once it is the single active identity of that (pool, user).
            models.PosixIdentity.objects.filter(
                pk__in=[identity.pk for identity in group["superseded"]]
            ).update(released_at=timezone.now(), recyclable=False)
            canonical = group["canonical"]
            if not canonical.user_id:
                canonical.user_id = group["user_id"]
                canonical.content_type = None
                canonical.object_id = None
                canonical.save(update_fields=["user", "content_type", "object_id"])
            by_offering_user = defaultdict(list)
            for change in group["changes"]:
                by_offering_user[change["offering_user_uuid"]].append(change)
            for offering_user_uuid, changes in by_offering_user.items():
                offering_user = models.OfferingUser.objects.filter(
                    uuid=offering_user_uuid
                ).first()
                if offering_user is not None:
                    _apply_metadata_changes(offering_user, changes)
        emit_change_events(group["changes"])
        logger.info(
            "Collapsed %s POSIX identity row(s) of user %s in pool %s onto uid=%s gid=%s.",
            len(group["superseded"]),
            group["user_id"],
            group["pool"].uuid.hex,
            group["canonical"].uid,
            group["canonical"].gid,
        )


def _repoint(pool) -> dict:
    """Move the pool's offering accounts onto this pool. Writes.

    Only meaningful for an offering-level override pool: ``resolve()`` already
    returns it for the offering, so allocating for each account either reuses the
    identity it already has in this pool or creates one. Identities the accounts
    leave behind are released — and withheld from recycling — once no other
    account of that user resolves to the old pool.
    """
    if pool.offering_id is None:
        raise ValueError("Re-pointing applies to an offering-level pool only.")
    offering = pool.offering
    changes = []
    user_ids = set()
    offering_users = models.OfferingUser.objects.filter(
        offering=offering
    ).select_related("user", "offering")
    for offering_user in offering_users:
        values = {}
        for _key, namespace in METADATA_KEYS:
            values[namespace] = posix_ids.allocate(offering, namespace, offering_user)
        offering_user_changes = _metadata_changes(
            offering_user, values[posix_ids.UID], values[posix_ids.GID]
        )
        if offering_user_changes:
            _apply_metadata_changes(offering_user, offering_user_changes)
            changes.extend(offering_user_changes)
        user_ids.add(offering_user.user_id)

    released = 0
    retained = []
    for user_id in user_ids:
        if _holds_a_namespace_outside(user_id, pool):
            # The new pool does not manage every namespace the old identity
            # holds (a GID-only override, say), so the account still sources one
            # of its values from the old pool. Releasing that identity would
            # unreserve a value the account is actively using.
            retained.append(user_id)
            continue
        released += posix_ids.release_user_allocations(user_id, recyclable=False)
    return {
        "changes": changes,
        "released": released,
        "retained": len(retained),
        "other_consumers": _consumers_left_behind(offering, pool),
    }


def _holds_a_namespace_outside(user_id, pool) -> bool:
    """Does the user hold a value the target pool cannot take over?"""
    unmanaged = [
        namespace for namespace in posix_ids.NAMESPACES if not pool.manages(namespace)
    ]
    if not unmanaged:
        return False
    others = models.PosixIdentity.objects.filter(
        user_id=user_id, released_at__isnull=True
    ).exclude(pool=pool)
    for identity in others:
        if any(getattr(identity, namespace) is not None for namespace in unmanaged):
            return True
    return False


def _consumers_left_behind(offering, pool) -> list:
    """Robot accounts and groups of the offering that keep their old values.

    Re-pointing moves offering accounts only — those are the rows whose UID and
    home directory the provider's directory keys on. A robot account or group
    that already holds a value from the previously resolved pool keeps it, so
    the report names them and the operator decides what to do.
    """
    left_behind = []
    for model, label in (
        (models.RobotAccount, "robot account"),
        (models.OfferingUserGroup, "offering user group"),
        (models.OfferingRoleGroup, "offering role group"),
    ):
        content_type = ContentType.objects.get_for_model(model)
        identities = models.PosixIdentity.objects.filter(
            content_type=content_type, released_at__isnull=True, offering=offering
        ).exclude(pool=pool)
        for identity in identities:
            left_behind.append(
                {
                    "kind": label,
                    "uid": identity.uid,
                    "gid": identity.gid,
                    "identity_uuid": identity.uuid.hex,
                }
            )
    return left_behind


def plan_repoint(pool) -> dict:
    """Preview :func:`apply_repoint` without keeping any of its writes.

    The move is executed inside a transaction that is then rolled back, so the
    report carries the exact values the accounts would get. A concurrent
    allocation between the preview and the apply can still shift them.

    Django signal handlers do fire during the preview, so anything hooked onto
    ``OfferingUser`` post-save runs for changes that are then discarded. Today
    that is inert because ``backend_metadata`` is not in the model's field
    tracker; adding it there would start publishing messages for a preview, so
    the hook would need an explicit guard.
    """
    result = {"changes": [], "released": 0, "retained": 0, "other_consumers": []}
    try:
        with transaction.atomic():
            result = _repoint(pool)
            raise _Rollback
    except _Rollback:
        pass
    pool.refresh_from_db()
    return result


def apply_repoint(pool) -> dict:
    """Move the pool's offering accounts onto this pool and log the moves."""
    with transaction.atomic():
        result = _repoint(pool)
    emit_change_events(result["changes"])
    return result
