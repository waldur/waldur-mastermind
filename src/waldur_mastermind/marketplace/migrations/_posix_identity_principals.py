"""Backfill logic for making POSIX identities principal-scoped.

A ``PosixIdentity`` used to be keyed on the consumer row, so a user with offering
accounts on two offerings of one service provider held two identities and two
different UIDs, even though both offerings draw from the same pool. The identity
is now keyed on the *principal* — the Waldur user for offering users — so one
user has one UID and one primary GID per pool.

This helper moves existing rows onto the ``user`` column. It deliberately does
**not** collapse duplicates: rewriting a live account's UID changes what the
provider's LDAP tree and filesystem must agree on, so it is an operator decision
driven by the ``collapse_posix_identities`` management command. Where a
``(pool, user)`` group already has several active identities, the oldest one is
made user-scoped and the rest are left consumer-scoped and untouched — nothing
changes for them until the command is run with ``--apply``.

Logic lives here (underscore prefix keeps it out of the migration loader) so it
can be unit-tested.
"""

CHUNK_SIZE = 2000


def backfill_posix_identity_principals(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    OfferingUser = apps.get_model("marketplace", "OfferingUser")
    PosixIdentity = apps.get_model("marketplace", "PosixIdentity")

    offering_user_ct = ContentType.objects.filter(
        app_label="marketplace", model="offeringuser"
    ).first()
    if offering_user_ct is None:
        return

    identities = list(
        PosixIdentity.objects.filter(
            content_type=offering_user_ct, released_at__isnull=True
        ).order_by("id")
    )
    if not identities:
        return

    user_ids = dict(
        OfferingUser.objects.filter(
            id__in={identity.object_id for identity in identities}
        ).values_list("id", "user_id")
    )

    # Oldest identity of a (pool, user) group wins the user FK; later ones stay
    # consumer-scoped so the new partial-unique on active (pool, user) holds.
    claimed = set()
    updated = []
    for identity in identities:
        user_id = user_ids.get(identity.object_id)
        if user_id is None:
            # The offering user row is gone; keep the audit row as it is.
            continue
        key = (identity.pool_id, user_id)
        if key in claimed:
            continue
        claimed.add(key)
        identity.user_id = user_id
        identity.content_type = None
        identity.object_id = None
        updated.append(identity)

    for start in range(0, len(updated), CHUNK_SIZE):
        PosixIdentity.objects.bulk_update(
            updated[start : start + CHUNK_SIZE],
            ["user_id", "content_type", "object_id"],
        )
