"""Backfill logic for the POSIX ID pool migration.

For every offering that carries legacy POSIX configuration — UID/GID values in
the ``backend_metadata`` of its offering users, robot accounts or groups, or the
legacy ``initial_*`` plugin options — synthesize an offering-level PosixIdPool,
record the observed values as PosixIdentity rows, and strip the obsolete
``initial_*`` keys.

One pool per offering reproduces the exact pre-existing allocation behaviour:
the old per-offering "max + 1" allocators handed out values independently per
offering, so an offering's UID/GID values are internally unique and map cleanly
into a single pool without collisions. Two offerings of one provider may end up
with numerically overlapping pools — that grandfathers the reality of the old
per-offering allocators; the provider-wide non-overlap rule applies only to
pools created afterwards through the API.

Logic lives here (underscore prefix keeps it out of the migration loader) so it
can be unit-tested.
"""

MIN_ID = 1000
MAX_ID = 2**32 - 2
HEADROOM = 10_000

UID_FLOOR_KEY = "initial_uidnumber"
GID_FLOOR_KEYS = (
    "initial_primarygroup_number",
    "initial_usergroup_number",
    "initial_rolegroup_number",
)
LEGACY_KEYS = (UID_FLOOR_KEY,) + GID_FLOOR_KEYS
DEFAULT_UID_FLOOR = 5000
DEFAULT_GID_FLOOR = 5000
# Historical role-group band; kept inside the unified GID range so role-group
# GIDs already handed out at 60000+ stay within the pool's bounds.
DEFAULT_GID_CEIL = 60000

# Chunk sizes so a provider with a very large offering does not load every
# consumer row (or issue one unbounded multi-row INSERT) at once.
CONSUMER_CHUNK_SIZE = 2000
IDENTITY_BATCH_SIZE = 1000


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _collect_identities(apps, offering):
    """Map ``(content_type_id, object_id)`` to ``{"uid": int|None, "gid": int|None}``.

    Offering users and robot accounts contribute a UID (``uidnumber``) and a
    primary GID (``primarygroup``); groups contribute a GID (``gid``).
    """
    OfferingUser = apps.get_model("marketplace", "OfferingUser")
    RobotAccount = apps.get_model("marketplace", "RobotAccount")
    OfferingUserGroup = apps.get_model("marketplace", "OfferingUserGroup")
    OfferingRoleGroup = apps.get_model("marketplace", "OfferingRoleGroup")
    ContentType = apps.get_model("contenttypes", "ContentType")

    cts = {
        model._meta.model_name: ContentType.objects.get_for_model(model).id
        for model in (OfferingUser, RobotAccount, OfferingUserGroup, OfferingRoleGroup)
    }

    identities = {}

    user_like = [
        (OfferingUser.objects.filter(offering=offering), "offeringuser"),
        (RobotAccount.objects.filter(resource__offering=offering), "robotaccount"),
    ]
    for queryset, ct_key in user_like:
        for pk, metadata in (
            queryset.exclude(backend_metadata=None)
            .values_list("id", "backend_metadata")
            .iterator(chunk_size=CONSUMER_CHUNK_SIZE)
        ):
            uid = _as_int((metadata or {}).get("uidnumber"))
            gid = _as_int((metadata or {}).get("primarygroup"))
            if uid is None and gid is None:
                continue
            identities[(cts[ct_key], pk)] = {"uid": uid, "gid": gid}

    group_like = [
        (OfferingUserGroup.objects.filter(offering=offering), "offeringusergroup"),
        (OfferingRoleGroup.objects.filter(offering=offering), "offeringrolegroup"),
    ]
    for queryset, ct_key in group_like:
        for pk, metadata in (
            queryset.exclude(backend_metadata=None)
            .values_list("id", "backend_metadata")
            .iterator(chunk_size=CONSUMER_CHUNK_SIZE)
        ):
            gid = _as_int((metadata or {}).get("gid"))
            if gid is None:
                continue
            identities[(cts[ct_key], pk)] = {"uid": None, "gid": gid}

    return identities


def backfill_posix_pools(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    PosixIdPool = apps.get_model("marketplace", "PosixIdPool")
    PosixIdentity = apps.get_model("marketplace", "PosixIdentity")

    for offering in Offering.objects.filter(customer__isnull=False).iterator():
        plugin_options = offering.plugin_options or {}
        identities = _collect_identities(apps, offering)
        has_legacy = any(key in plugin_options for key in LEGACY_KEYS)
        if not identities and not has_legacy:
            continue

        observed_uids = [v["uid"] for v in identities.values() if v["uid"] is not None]
        observed_gids = [v["gid"] for v in identities.values() if v["gid"] is not None]

        def clamp(value):
            # Keep config-derived floors inside [MIN_ID, MAX_ID] so a corrupt
            # legacy value never pushes the bounds outside the pool's check
            # constraints and crashes the migration.
            return max(MIN_ID, min(value, MAX_ID))

        uid_floor = clamp(
            _as_int(plugin_options.get(UID_FLOOR_KEY)) or DEFAULT_UID_FLOOR
        )
        gid_floors = [
            clamp(value)
            for value in (_as_int(plugin_options.get(key)) for key in GID_FLOOR_KEYS)
            if value is not None
        ]
        gid_floor = min(gid_floors) if gid_floors else DEFAULT_GID_FLOOR
        gid_ceil = max(gid_floors + [clamp(DEFAULT_GID_CEIL)])

        min_uid = max(MIN_ID, min([uid_floor] + observed_uids))
        max_uid = min(MAX_ID, max([uid_floor] + observed_uids) + HEADROOM)
        min_gid = max(MIN_ID, min([gid_floor] + observed_gids))
        max_gid = min(MAX_ID, max(observed_gids + [gid_ceil]) + HEADROOM)

        next_uid = max([uid_floor] + [value + 1 for value in observed_uids])
        next_uid = min(max(next_uid, min_uid), max_uid + 1)
        next_gid = max([gid_floor] + [value + 1 for value in observed_gids])
        next_gid = min(max(next_gid, min_gid), max_gid + 1)

        pool = PosixIdPool.objects.create(
            offering=offering,
            min_uid=min_uid,
            max_uid=max_uid,
            next_uid=next_uid,
            min_gid=min_gid,
            max_gid=max_gid,
            next_gid=next_gid,
            description=(
                "Synthesized from legacy POSIX configuration by migration 0247"
            ),
        )

        # Values are unique within one offering (the old per-offering allocator
        # guaranteed it), so no collision handling is needed; guard defensively
        # against corrupt duplicates by dropping the repeated value only.
        seen_uid, seen_gid = set(), set()
        rows = []
        for (ct_id, object_id), values in identities.items():
            uid, gid = values["uid"], values["gid"]
            if uid is not None and uid in seen_uid:
                uid = None
            elif uid is not None:
                seen_uid.add(uid)
            if gid is not None and gid in seen_gid:
                gid = None
            elif gid is not None:
                seen_gid.add(gid)
            if uid is None and gid is None:
                continue
            rows.append(
                PosixIdentity(
                    pool=pool,
                    uid=uid,
                    gid=gid,
                    content_type_id=ct_id,
                    object_id=object_id,
                    offering=offering,
                )
            )
        PosixIdentity.objects.bulk_create(rows, batch_size=IDENTITY_BATCH_SIZE)

        if has_legacy:
            offering.plugin_options = {
                key: value
                for key, value in plugin_options.items()
                if key not in LEGACY_KEYS
            }
            offering.save(update_fields=["plugin_options"])
