"""Pre-join room history for new members, as Waldur's database tracks it.

A member added to an encrypted room holds no keys for what was said before. The
bot does: it writes the room's Megolm sessions into the member's current key
backup, and the member's clients restore them from there. This module records
which memberships are due such a fill. It is plain Django, without matrix-nio,
so the API and Celery can ask for a fill; the bot process does the writing.

A fill is asked for when a member is added, when the member's encryption is set
up or reset (the reset deletes every older backup), and when the bot finds a
member's backup version changed. Writing skips sessions the backup already
holds, so asking twice costs a little and changes nothing.
"""

from datetime import timedelta

from django.db.models import Min
from django.db.models.functions import Now
from django.utils import timezone

from waldur_mastermind.matrix_chat import models

# A fill that could not be written yet (the member's join has not reached the
# homeserver, or the homeserver failed) is tried again after these delays, and
# then dropped until something asks again.
RETRY_DELAYS = [timedelta(seconds=s) for s in (30, 120, 600, 3600, 6 * 3600)]

# Memberships whose room's history the member may read: the same rule the room's
# open action applies, on an active room.
CURRENT = {
    "membership_state__in": (
        models.MembershipStates.INVITED,
        models.MembershipStates.JOINED,
    ),
    "room__state": models.RoomStates.ACTIVE,
    "room__room_id__isnull": False,
}


def request(members):
    """Ask for a fill of each current membership in the ``members`` queryset."""
    return members.filter(**CURRENT).update(history_due_at=Now(), history_attempts=0)


def request_member(member):
    return request(models.MatrixRoomMember.objects.filter(pk=member.pk))


def request_for_user(user):
    """Ask for a fill of every room the user is in: their backup is new."""
    return request(models.MatrixRoomMember.objects.filter(user=user))


def due_users(limit):
    """Up to ``limit`` users with a fill due, longest waiting first."""
    return list(
        models.MatrixRoomMember.objects.filter(
            history_due_at__lte=timezone.now(), **CURRENT
        )
        .values("user_id")
        .annotate(due=Min("history_due_at"))
        .order_by("due", "user_id")
        .values_list("user_id", flat=True)[:limit]
    )


def due_fills(user_id):
    """The user's due memberships, as ``(pk, room_id, matrix_user_id, due_at)``."""
    return list(
        models.MatrixRoomMember.objects.filter(
            user_id=user_id, history_due_at__lte=timezone.now(), **CURRENT
        )
        .order_by("pk")
        .values_list("pk", "room__room_id", "matrix_user_id", "history_due_at")
    )


def recovery_key(user_id):
    """The user's escrowed recovery key, or "" if Waldur holds none."""
    profile = models.MatrixUserProfile.objects.filter(user_id=user_id).first()
    return profile.recovery_key if profile else ""


def is_current_member(member_pk):
    """Whether the membership still lets its user read the room, decided now:
    a role, or a staff Join, still puts an active user in an active room."""
    member = (
        models.MatrixRoomMember.objects.filter(pk=member_pk, **CURRENT)
        .select_related("room", "user")
        .first()
    )
    return member is not None and models.keeps_room_access(member.user, member.room)


# Each outcome is recorded only if no new request came in meanwhile: that one
# needs a fill of its own, perhaps into a newer backup.


def mark_filled(member_pk, due_at, version):
    models.MatrixRoomMember.objects.filter(pk=member_pk, history_due_at=due_at).update(
        history_due_at=None, history_attempts=0, history_backup_version=version
    )


def mark_dropped(member_pk, due_at, version=""):
    """Nothing to write: no backup, an untrusted one, or no longer a member.

    ``version`` is the backup refused as untrusted, recorded so the version check
    does not ask again for the same backup; a new backup, or the user's next
    setup, reset or escrowed key in Waldur, asks again.
    """
    update = {"history_due_at": None, "history_attempts": 0}
    if version:
        update["history_backup_version"] = version
    models.MatrixRoomMember.objects.filter(pk=member_pk, history_due_at=due_at).update(
        **update
    )


def mark_retry(member_pk, due_at):
    """Try again later, or drop the fill once the retries are used up."""
    member = models.MatrixRoomMember.objects.filter(
        pk=member_pk, history_due_at=due_at
    ).first()
    if member is None:
        return
    attempts = member.history_attempts + 1
    next_due = (
        timezone.now() + RETRY_DELAYS[attempts - 1]
        if attempts <= len(RETRY_DELAYS)
        else None
    )
    models.MatrixRoomMember.objects.filter(pk=member_pk, history_due_at=due_at).update(
        history_due_at=next_due, history_attempts=attempts
    )


def members_to_check(after_user_id, limit):
    """Users with current memberships, in user order after ``after_user_id``:
    ``[(user_id, matrix_user_id), ...]`` for the bot's backup version check."""
    rows = (
        models.MatrixRoomMember.objects.filter(user_id__gt=after_user_id, **CURRENT)
        .order_by("user_id")
        .values_list("user_id", "matrix_user_id")
        .distinct()[:limit]
    )
    return list(rows)


def request_if_version_changed(user_id, version):
    """Ask for a fill of the user's rooms not yet written into ``version``."""
    return request(
        models.MatrixRoomMember.objects.filter(
            user_id=user_id, history_due_at__isnull=True
        ).exclude(history_backup_version=version)
    )


def room_member_ids(room_id):
    """Matrix ids of everyone Waldur has had in the room, past members too."""
    return set(
        models.MatrixRoomMember.objects.filter(room__room_id=room_id).values_list(
            "matrix_user_id", flat=True
        )
    )


# The master cross-signing key of a user whose recovery key Waldur doesn't hold,
# pinned on first sight; see crypto.key_backup.


def pin_master_key(user_id, master_key):
    """Pin ``master_key`` unless a key is pinned already; returns the pinned key,
    or "" if the user has no profile."""
    models.MatrixUserProfile.objects.filter(
        user_id=user_id, pinned_master_key=""
    ).update(pinned_master_key=master_key)
    return (
        models.MatrixUserProfile.objects.filter(user_id=user_id)
        .values_list("pinned_master_key", flat=True)
        .first()
        or ""
    )


def key_escrowed(profile):
    """Waldur holds the user's recovery key now, so their backup is judged by it
    from here on: the pin is no longer needed, and every room is due a fill.

    Only escrow clears the pin. A lease released without a key escrowed (a setup
    cancelled or failed) clears nothing, or a homeserver that hid the user's
    master key to provoke a setup could have its own key pinned afterwards.
    """
    models.MatrixUserProfile.objects.filter(pk=profile.pk).update(pinned_master_key="")
    request_for_user(profile.user)
