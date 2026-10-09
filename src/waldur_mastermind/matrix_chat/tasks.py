import io
import json
import logging
import re
import zipfile
from datetime import timedelta

import httpx
from celery import shared_task
from constance import config
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from waldur_core.permissions.models import UserRole

from . import bot_state, formatting, matrix_client, models

User = get_user_model()

logger = logging.getLogger(__name__)

# Power level granted to staff who self-join via the admin panel. 50 renders as
# the "Moderator" badge in Element — the same tier as customer owners.
STAFF_POWER_LEVEL = 50

# How long a task that must not act before the bot has posted (the notice that
# a room is closing, sent before its members are removed) waits for it.
BOT_POST_WAIT_SECONDS = 30

# Sent outbox messages are kept this long, for debugging delivery.
OUTBOX_RETENTION_DAYS = 7


def post_as_bot(room, body, reply_to="", wait=False):
    """Queue ``body`` for the bot to post, encrypted, in ``room``.

    Rooms are encrypted and only the bot process holds the keys to post into
    them, so nothing else posts as the bot: a message waits in the outbox until
    the bot sends it. ``wait`` holds the caller until it has, or gives up quietly.
    """
    message = bot_state.enqueue(room, body, reply_to=reply_to)
    # Only a running bot is worth waiting for; otherwise the worker would sit
    # out the full wait for every room.
    if (
        wait
        and bot_state.is_bot_running(matrix_client.get_bot_user_id())
        and not bot_state.wait_until_sent(message, BOT_POST_WAIT_SECONDS)
    ):
        logger.warning("The bot has not posted %s in time", message.uuid)


# Deleting an export runs its post_delete handler, so Django loads every row it
# deletes; chunks keep the first run after an upgrade from loading them all.
EXPORT_CLEANUP_CHUNK_SIZE = 100


def _record_left(member):
    member.membership_state = models.MembershipStates.LEFT
    member.manually_joined = False
    member.save(update_fields=["membership_state", "manually_joined"])


def _save_member(room, user, matrix_user_id, membership_state, power_level):
    """Set the member's power level in the room and record the membership."""
    defaults = {"matrix_user_id": matrix_user_id, "membership_state": membership_state}
    # Also when it is 0, so a level the user's roles no longer give is taken back.
    try:
        matrix_client.set_power_level(room.room_id, matrix_user_id, power_level)
        defaults["power_level"] = power_level
    except Exception:
        # The user is in the room by now, and revocation finds the rooms to
        # take them out of by this row, so it is written either way.
        logger.warning(
            "Failed to set the power level of %s in %s", matrix_user_id, room.room_id
        )
    models.MatrixRoomMember.objects.update_or_create(
        room=room, user=user, defaults=defaults
    )


def _power_level_in_room(user, room):
    power_level = matrix_client.get_power_level_for_scope(user, room.scope)
    # Staff and support hold their level by joining with the Join button, not
    # by a role, so a role they also hold must not lower it.
    if models.MatrixRoomMember.objects.filter(
        models.STAFF_JOINED, room=room, user=user
    ).exists():
        return max(power_level, STAFF_POWER_LEVEL)
    return power_level


@shared_task(name="waldur_mastermind.matrix_chat.create_room")
def create_room(room_uuid):
    """Create a Matrix room and sync project members."""
    if not matrix_client.is_enabled():
        logger.info("Matrix integration is disabled, skipping room creation")
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    # Every dispatch moves the row to CREATING first, so any other state means
    # an earlier task already handled it; creating again would leave a second
    # homeserver room behind and flip the working one to ERROR.
    if room.state != models.RoomStates.CREATING:
        logger.info(
            "Skipped creating Matrix room %s: already %s", room.uuid, room.state
        )
        return

    # A scope removed while this task waited has already passed pre_delete, so
    # a room created now would stay active and never be archived. No homeserver
    # room exists yet, so marking the row erred leaves nothing behind.
    scope = room.scope
    if scope is None or getattr(scope, "is_removed", False):
        room.set_erred()
        room.error_message = (
            f"The {room.content_type.name} was removed before its room was "
            "created, so retrying cannot succeed."
        )
        room.save(update_fields=["state", "error_message"])
        logger.info("Skipped creating Matrix room %s: scope removed", room.uuid)
        return

    try:
        alias_localpart = None
        if room.project:
            # The whole UUID: a prefix collides on sequential UUIDs, and the
            # alias must come out the same when a room is reprovisioned.
            alias_localpart = f"{models.ROOM_ALIAS_PREFIX}{room.project.uuid.hex}"

        room_id, alias_was_set = matrix_client.create_room(
            name=room.room_name,
            alias_localpart=alias_localpart,
        )

        room.room_id = room_id
        if alias_was_set:
            room.room_alias = f"#{alias_localpart}:{config.MATRIX_HOMESERVER_DOMAIN}"
        room.set_active()
        room.save(update_fields=["room_id", "room_alias", "state"])

        logger.info("Created Matrix room %s for %s", room_id, room.scope)

        sync_project_members_to_room.delay(str(room.uuid))

    except Exception as e:
        room.set_erred()
        room.error_message = str(e)
        room.save(update_fields=["state", "error_message"])
        logger.exception("Failed to create Matrix room for %s", room.scope)


@shared_task(name="waldur_mastermind.matrix_chat.sync_project_members_to_room")
def sync_project_members_to_room(room_uuid):
    """Ensure all project members are provisioned and invited to the room."""
    if not matrix_client.is_enabled():
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.ACTIVE:
        logger.warning("Room %s is not active, skipping sync", room.room_id)
        return

    project = room.project
    if not project:
        logger.warning("Room %s has no associated project", room.room_id)
        return

    # Get all active users in the project (direct + via customer). Deactivation
    # leaves a user's roles in place, so the user's own flag is checked too.
    user_roles = UserRole.objects.filter(
        scope=project,
        is_active=True,
        user__is_active=True,
    ).select_related("user")

    customer_roles = (
        models.get_customer_roles_in_project_rooms(project.customer)
        .filter(user__is_active=True)
        .select_related("user")
    )

    seen_users = set()
    for role in list(user_roles) + list(customer_roles):
        user = role.user
        if user.id in seen_users:
            continue
        seen_users.add(user.id)

        try:
            matrix_user_id = matrix_client.ensure_user_exists(user)
            power_level = _power_level_in_room(user, room)

            # Ensure display name is up to date
            display_name = user.full_name or user.username
            try:
                matrix_client.set_display_name(matrix_user_id, display_name)
            except Exception:
                logger.warning("Failed to update display name for %s", matrix_user_id)

            # Rooms are invite-only, so even the appservice needs the bot's
            # invite before it can join the user.
            matrix_client.invite_user(room.room_id, matrix_user_id)
            try:
                matrix_client.join_room_as_user(room.room_id, matrix_user_id)
                membership_state = models.MembershipStates.JOINED
            except Exception:
                logger.warning(
                    "Auto-join failed for %s in %s, left as invited",
                    matrix_user_id,
                    room.room_id,
                )
                membership_state = models.MembershipStates.INVITED

            _save_member(room, user, matrix_user_id, membership_state, power_level)

            logger.info("Synced user %s to room %s", matrix_user_id, room.room_id)
        except Exception:
            logger.exception("Failed to sync user %s to room %s", user, room.room_id)

    # Kick members who no longer have any active roles in this project or its customer
    stale_members = (
        models.MatrixRoomMember.objects.filter(room=room)
        .exclude(user_id__in=seen_users)
        .exclude(
            membership_state__in=[
                models.MembershipStates.LEFT,
                models.MembershipStates.BANNED,
            ]
        )
        # Staff and support who joined with the Join button hold no role here.
        .exclude(models.STAFF_JOINED)
    )
    for member in stale_members:
        try:
            matrix_client.kick_user(
                room.room_id, member.matrix_user_id, reason="Role removed in Waldur"
            )
            _record_left(member)
            logger.info(
                "Kicked stale member %s from room %s",
                member.matrix_user_id,
                room.room_id,
            )
        except Exception:
            logger.exception(
                "Failed to kick stale member %s from room %s",
                member.matrix_user_id,
                room.room_id,
            )

    room.modified = timezone.now()
    room.save(update_fields=["modified"])


@shared_task(name="waldur_mastermind.matrix_chat.sync_rooms_of_role")
def sync_rooms_of_role(role_id):
    """Sync every active project room the role has holders in."""
    if not matrix_client.is_enabled():
        return

    rooms = models.get_project_rooms(
        UserRole.objects.filter(role_id=role_id, is_active=True)
    )
    for room_uuid in rooms.values_list("uuid", flat=True):
        sync_project_members_to_room.delay(str(room_uuid))


@shared_task(name="waldur_mastermind.matrix_chat.invite_user_to_room")
def invite_user_to_room(room_uuid, user_uuid):
    """Invite a single user to a Matrix room."""
    if not matrix_client.is_enabled():
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.ACTIVE:
        logger.warning("Room %s is not active, skipping invite", room.room_id)
        return

    try:
        user = User.objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        logger.error("User %s not found", user_uuid)
        return

    try:
        matrix_user_id = matrix_client.ensure_user_exists(user)
        power_level = _power_level_in_room(user, room)

        matrix_client.invite_user(room.room_id, matrix_user_id)
        try:
            matrix_client.join_room_as_user(room.room_id, matrix_user_id)
            membership_state = models.MembershipStates.JOINED
        except Exception:
            logger.warning(
                "Auto-join failed for %s in %s, left as invited",
                matrix_user_id,
                room.room_id,
            )
            membership_state = models.MembershipStates.INVITED

        _save_member(room, user, matrix_user_id, membership_state, power_level)

        logger.info("Invited user %s to room %s", matrix_user_id, room.room_id)
    except Exception:
        logger.exception("Failed to invite user %s to room %s", user, room.room_id)


@shared_task(name="waldur_mastermind.matrix_chat.staff_join_room")
def staff_join_room(room_uuid, user_uuid):
    """Add a staff member to a Matrix room with a Moderator badge and announce it."""
    if not matrix_client.is_enabled():
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.ACTIVE:
        logger.warning("Room %s is not active, skipping staff join", room.room_id)
        return

    try:
        user = User.objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        logger.error("User %s not found", user_uuid)
        return

    try:
        matrix_user_id = matrix_client.ensure_user_exists(user)

        display_name = user.full_name or user.username
        try:
            matrix_client.set_display_name(matrix_user_id, display_name)
        except Exception:
            logger.warning("Failed to update display name for %s", matrix_user_id)

        matrix_client.invite_user(room.room_id, matrix_user_id)
        try:
            matrix_client.join_room_as_user(room.room_id, matrix_user_id)
            membership_state = models.MembershipStates.JOINED
        except Exception:
            logger.warning(
                "Auto-join failed for staff %s in %s, left as invited",
                matrix_user_id,
                room.room_id,
            )
            membership_state = models.MembershipStates.INVITED

        matrix_client.set_power_level(room.room_id, matrix_user_id, STAFF_POWER_LEVEL)

        models.MatrixRoomMember.objects.update_or_create(
            room=room,
            user=user,
            defaults={
                "matrix_user_id": matrix_user_id,
                "power_level": STAFF_POWER_LEVEL,
                "membership_state": membership_state,
                "manually_joined": True,
            },
        )

        full_name = user.full_name or user.username
        try:
            post_as_bot(
                room, f"{formatting.escape_markdown(full_name)} joined the room."
            )
        except Exception:
            logger.warning("Failed to announce staff join in room %s", room.room_id)

        logger.info("Staff %s joined room %s", matrix_user_id, room.room_id)
    except Exception:
        logger.exception("Failed to add staff %s to room %s", user, room.room_id)


@shared_task(name="waldur_mastermind.matrix_chat.prune_web_devices")
def prune_web_devices(matrix_user_id, keep_device_id=None):
    """Sign out a user's web chat devices that are stale or over the limit.

    `keep_device_id` is the session that triggered the prune, which may not
    have been seen by the homeserver yet.
    """
    # Open drawers outlive switching chat off, so this does not need it on.
    if not matrix_client.is_homeserver_configured():
        return

    marked_at = matrix_client.get_web_session_mark(matrix_user_id)
    try:
        web_devices = matrix_client.list_web_devices(matrix_user_id)
    except matrix_client.MatrixUserLocked:
        # A locked account refuses the listing, and its devices just the same.
        # The user stays marked, for the prune after an unlock.
        return

    now_ms = int(timezone.now().timestamp() * 1000)
    stale = matrix_client.stale_web_devices(
        web_devices, now_ms, keep_device_id=keep_device_id
    )
    signed_out = _sign_out_devices(matrix_user_id, stale)

    left = {device["device_id"] for device in web_devices} - signed_out
    # A prune started by a new session always leaves that session's device.
    if not left and not keep_device_id:
        matrix_client.clear_web_session_mark(matrix_user_id, marked_at)


def _sign_out_devices(matrix_user_id, device_ids):
    """Sign out each device and return the IDs that were signed out."""
    signed_out = set()
    for device_id in device_ids:
        try:
            matrix_client.logout_device(matrix_user_id, device_id)
            signed_out.add(device_id)
        except Exception:
            logger.exception(
                "Failed to sign out web device %s of %s", device_id, matrix_user_id
            )
    return signed_out


# Revocations have to outlast a homeserver restart. retry_backoff=True would
# start at one second and spend every retry within seconds. From 30, Celery's
# default jitter waits up to 30, 60, ... 600 s per retry, about 25 minutes at
# most in all, and keeps a bulk deactivation's tasks from retrying in lockstep.
# Only transport and homeserver errors are worth retrying.
REVOCATION_RETRY = dict(
    autoretry_for=(matrix_client.MatrixClientError, httpx.HTTPError),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=6,
)


@shared_task(
    name="waldur_mastermind.matrix_chat.scrub_temporary_matrix_password",
    # The password lets anyone who saw it sign in, so keep trying.
    **REVOCATION_RETRY,
)
def scrub_temporary_matrix_password(matrix_user_id, lease=""):
    """Replace the temporary password an encryption reset was given.

    Runs when the new recovery key is escrowed, when the reset's lease runs out
    in case it never is, and from the periodic sweep. Skipped while a newer
    reset of the same user is under way: that one needs its own password, and
    scrubs it itself. Decided under the profile's lock, so a late retry can't
    replace a newer reset's password between the check and the write.
    """
    with transaction.atomic():
        profile = (
            models.MatrixUserProfile.objects.select_for_update()
            .filter(matrix_user_id=matrix_user_id)
            .first()
        )
        if (
            profile
            and profile.crypto_lease_kind == models.CryptoLeaseKinds.RESET
            and profile.crypto_lease
            and profile.crypto_lease != lease
            and profile.crypto_lease_expires_at
            and profile.crypto_lease_expires_at > timezone.now()
        ):
            return
        matrix_client.set_password(matrix_user_id, matrix_client.new_password())
        if profile:
            profile.crypto_temporary_password_until = None
            profile.save(update_fields=["crypto_temporary_password_until"])


@shared_task(name="waldur_mastermind.matrix_chat.scrub_expired_temporary_passwords")
def scrub_expired_temporary_passwords():
    """Replace any reset password whose scrub task was lost or kept failing."""
    for matrix_user_id in models.MatrixUserProfile.objects.filter(
        crypto_temporary_password_until__lt=timezone.now()
    ).values_list("matrix_user_id", flat=True):
        scrub_temporary_matrix_password.delay(matrix_user_id)


@shared_task(name="waldur_mastermind.matrix_chat.prune_all_web_devices")
def prune_all_web_devices():
    """Prune the idle web devices of every user who may still have one, daily.

    Pruning on session start never reaches users who do not come back, and a
    homeserver without access_token_ttl leaves their tokens valid for a week.
    Users with no web device left are not asked about.
    """
    if not matrix_client.is_homeserver_configured():
        return
    # Each user's task would fail on its own and log the same outage; the next
    # daily run catches up.
    if not matrix_client.is_homeserver_reachable():
        logger.warning("Homeserver does not answer, skipping web device pruning")
        return
    for matrix_user_id in models.MatrixUserProfile.objects.filter(
        provisioned=True, last_web_session_at__isnull=False
    ).values_list("matrix_user_id", flat=True):
        prune_web_devices.delay(matrix_user_id)


@shared_task(
    name="waldur_mastermind.matrix_chat.end_deleted_user_access",
    # A retry lists the devices again, so only the ones still there are retried.
    **REVOCATION_RETRY,
)
def end_deleted_user_access(matrix_user_id, room_ids=()):
    """Remove a deleted user from their rooms, sign out all their devices,
    replace their Matrix password and lock the account for good."""
    # Open drawers outlive switching chat off, so this does not need it on.
    if not matrix_client.is_homeserver_configured():
        return
    # As in kick_user: a stored ID can map onto the bot, and none of this is
    # done to the bot.
    if matrix_user_id == matrix_client.get_bot_user_id():
        logger.warning("Not ending the Matrix access of the bot %s", matrix_user_id)
        return
    reason = "Account deleted in Waldur"
    for room_id in room_ids:
        try:
            matrix_client.kick_user(room_id, matrix_user_id, reason=reason)
        except Exception:
            # The membership rows are gone with the user, so the kick is
            # retried on its own rather than through them.
            logger.warning("Failed to kick %s from room %s", matrix_user_id, room_id)
            kick_from_room.delay(room_id, matrix_user_id, reason)
    locked, logout_error = _sign_out_all(matrix_user_id)
    scramble_error = None
    try:
        # The user may have generated the password and still know it, and an
        # account unlocked later, for a new user who gets this Matrix ID,
        # would accept it. The admin API also signs out the devices that the
        # sign-out above could not.
        matrix_client.set_password(
            matrix_user_id, matrix_client.new_password(), logout_devices=True
        )
        logout_error = None
    except matrix_client.MatrixUserNotFound:
        # A profile kept through a move to another homeserver, for one.
        logger.info("Deleted %s has no account on the homeserver", matrix_user_id)
        return
    except matrix_client.MatrixAccountIsHomeserverAdmin as e:
        # The deleted user was linked to it before that was refused. The
        # account is its admin's, who keeps the password and is not locked out.
        logger.warning(
            "Matrix password of deleted %s not replaced and its account not "
            "locked: it is a homeserver admin's",
            matrix_user_id,
        )
        if logout_error:
            raise logout_error from e
        return
    except matrix_client.MatrixAdminRequired as e:
        # The lock goes through the same API, so it is not tried either.
        # Retrying cannot help until an operator makes the API usable.
        logger.warning(
            "Matrix password of deleted %s not replaced, and its account %s: %s",
            matrix_user_id,
            "locked already" if locked else "left unlocked",
            e,
        )
        if logout_error:
            raise logout_error from e
        return
    except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
        # Retried below, once the lock has been tried.
        scramble_error = e
    if not locked:
        _lock_after_sign_out(matrix_user_id, logout_error)
    if scramble_error:
        raise scramble_error
    logger.info("Ended Matrix access of deleted %s", matrix_user_id)


def _sign_out_all(matrix_user_id):
    """Sign out every device of the user.

    Returns whether the account is locked already, and the error if the
    sign-out failed. A locked account refuses the sign-out, which is no
    failure: it refuses its devices just the same, and no retry could sign
    them out.
    """
    try:
        matrix_client.logout_all_devices(matrix_user_id)
    except matrix_client.MatrixUserLocked:
        return True, None
    except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
        return False, e
    logger.info("Signed out every Matrix device of %s", matrix_user_id)
    return False, None


def _lock_after_sign_out(matrix_user_id, logout_error):
    """Lock an account whose devices were just signed out.

    The lock comes last, as a locked account refuses the appservice too, and
    the sign-out acts through it. A sign-out that failed is therefore retried
    only while the account is not locked. Locked, the devices left behind are
    refused like any other, but work again if the account is ever unlocked.
    """
    try:
        locked = matrix_client.set_locked(matrix_user_id, True)
    except matrix_client.MatrixUserNotFound:
        # Nothing to lock, and no device that a retry could sign out.
        logger.info("%s has no account on the homeserver", matrix_user_id)
        return
    except matrix_client.MatrixAdminRequired as e:
        # Retrying cannot help until an operator makes the admin API usable.
        logger.warning("Matrix account left unlocked: %s", e)
        locked = False
    if logout_error:
        if not locked:
            raise logout_error
        logger.warning(
            "Locked %s after its devices could not be signed out: %s",
            matrix_user_id,
            logout_error,
        )


@shared_task(
    name="waldur_mastermind.matrix_chat.end_matrix_access",
    # A retry lists the devices again, so only the ones still there are retried.
    **REVOCATION_RETRY,
)
def end_matrix_access(user_uuid):
    """Sign out all of a deactivated user's Matrix devices, remove them from
    their rooms and lock their Matrix account, so neither the drawer nor an
    external client keeps access or signs in again."""
    # Open drawers outlive switching chat off, so this does not need it on.
    if not matrix_client.is_homeserver_configured():
        return

    # all_objects: the user has just been deactivated.
    try:
        user = User.all_objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        logger.error("User %s not found", user_uuid)
        return
    # Retries run for minutes; a user reactivated meanwhile keeps access.
    if user.is_active:
        return

    # Rooms are left even if the logout fails: one device the homeserver keeps
    # rejecting must not keep the user in every room through all retries.
    locked, logout_error = False, None
    profile = models.MatrixUserProfile.objects.filter(user=user).first()
    if profile:
        locked, logout_error = _sign_out_all(profile.matrix_user_id)

    memberships = (
        models.MatrixRoomMember.objects.filter(user=user, room__room_id__gt="")
        .exclude(membership_state__in=models.MembershipStates.GONE)
        .select_related("room")
    )
    for member in memberships:
        _kick_or_retry(member, "Account deactivated in Waldur")

    error = None
    if profile and not locked:
        try:
            _lock_after_sign_out(profile.matrix_user_id, logout_error)
        except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
            error = e

    # A reactivation committed meanwhile may have unlocked the account and
    # synced the rooms before this locked it and kicked, and nothing would
    # undo that. Its task does, and takes over from any retry of this one.
    if User.all_objects.filter(pk=user.pk, is_active=True).exists():
        restore_matrix_access.delay(user_uuid)
        return
    if error:
        raise error


@shared_task(
    name="waldur_mastermind.matrix_chat.restore_matrix_access",
    **REVOCATION_RETRY,
)
def restore_matrix_access(user_uuid):
    """Unlock a reactivated user's Matrix account, then bring them back into
    the rooms deactivation removed them from.

    One task, as the room syncs join rooms as the user, which a locked account
    refuses, and separate tasks run in no fixed order.
    """
    # As in end_matrix_access: deactivation locks while chat is switched off.
    if not matrix_client.is_homeserver_configured():
        return
    try:
        user = User.all_objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        logger.error("User %s not found", user_uuid)
        return
    # Retries run for minutes; a user deactivated again meanwhile stays locked.
    if not user.is_active:
        return

    profile = models.MatrixUserProfile.objects.filter(user=user).first()
    if profile:
        matrix_user_id = profile.matrix_user_id
        error = None
        try:
            matrix_client.set_locked(matrix_user_id, False)
            logger.info("Unlocked the Matrix account of %s", matrix_user_id)
        except matrix_client.MatrixAdminRequired as e:
            # Deactivation could not lock the account then, unless the admin
            # API stopped being usable since: such an account stays locked
            # until it is unlocked on the homeserver.
            logger.warning("Matrix account may still be locked: %s", e)
        except (matrix_client.MatrixClientError, httpx.HTTPError) as e:
            error = e

        # A deactivation committed meanwhile may have locked the account
        # before this unlocked it, and nothing would lock it again. Its task
        # does, and takes over from any retry of this one.
        if not User.all_objects.filter(pk=user.pk, is_active=True).exists():
            end_matrix_access.delay(user_uuid)
            return
        if error:
            raise error

    if not matrix_client.is_enabled():
        return
    for room_uuid in models.get_project_rooms(
        UserRole.objects.filter(user=user, is_active=True)
    ).values_list("uuid", flat=True):
        sync_project_members_to_room.delay(str(room_uuid))


def _kick_or_retry(member, reason):
    try:
        matrix_client.kick_user(
            member.room.room_id, member.matrix_user_id, reason=reason
        )
    except Exception:
        # Not recorded as left until the homeserver confirms it.
        logger.warning(
            "Failed to kick %s from room %s, retrying",
            member.matrix_user_id,
            member.room.room_id,
        )
        kick_member.delay(member.uuid.hex, reason)
        return
    _record_left(member)


@shared_task(
    name="waldur_mastermind.matrix_chat.kick_member",
    **REVOCATION_RETRY,
)
def kick_member(member_uuid, reason):
    """Remove one membership from its room, whatever the room's state."""
    if not matrix_client.is_homeserver_configured():
        return
    member = (
        models.MatrixRoomMember.objects.filter(uuid=member_uuid)
        .exclude(membership_state__in=models.MembershipStates.GONE)
        .select_related("room")
        .first()
    )
    if not member or not member.room.room_id:
        return
    # A retry can run long after its cause: a user reactivated, or a room
    # reactivated, meanwhile stays.
    if (
        member.user
        and member.room.state == models.RoomStates.ACTIVE
        and models.keeps_room_access(member.user, member.room)
    ):
        return
    matrix_client.kick_user(member.room.room_id, member.matrix_user_id, reason=reason)
    _record_left(member)


@shared_task(
    name="waldur_mastermind.matrix_chat.kick_from_room",
    **REVOCATION_RETRY,
)
def kick_from_room(room_id, matrix_user_id, reason):
    """Kick a Matrix ID that no membership row tracks any more."""
    if not matrix_client.is_homeserver_configured():
        return
    matrix_client.kick_user(room_id, matrix_user_id, reason=reason)


@shared_task(name="waldur_mastermind.matrix_chat.staff_leave_room")
def staff_leave_room(room_uuid, user_uuid):
    """Remove a staff member from a Matrix room (voluntary leave) and announce it."""
    if not matrix_client.is_enabled():
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.ACTIVE:
        logger.warning("Room %s is not active, skipping staff leave", room.room_id)
        return

    # all_objects covers users deactivated after dispatch — leaving them
    # joined to the room would be a silent revocation gap.
    try:
        user = User.all_objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        logger.error("User %s not found", user_uuid)
        return

    member = models.MatrixRoomMember.objects.filter(room=room, user=user).first()
    matrix_user_id = _resolve_matrix_user_id(user, member)
    if not matrix_user_id:
        logger.info("Staff %s has no Matrix identity, nothing to leave", user)
        return

    full_name = user.full_name or user.username
    # Announce before leaving so the departure is attributed while the member
    # is still present in the room.
    try:
        post_as_bot(room, f"{formatting.escape_markdown(full_name)} left the room.")
    except Exception:
        logger.warning("Failed to announce staff leave in room %s", room.room_id)

    try:
        matrix_client.leave_room_as_user(room.room_id, matrix_user_id)
    except Exception:
        logger.exception(
            "Failed to leave room %s as staff %s", room.room_id, matrix_user_id
        )

    if member:
        _record_left(member)
    logger.info("Staff %s left room %s", matrix_user_id, room.room_id)


def _resolve_matrix_user_id(user, member):
    """Find a user's canonical Matrix ID from their room record or profile.

    The MatrixRoomMember row is per-room bookkeeping that can be missing while
    the user is still joined on the homeserver, so the durable MatrixUserProfile
    is the reliable fallback. None means the user was never provisioned to
    Matrix and therefore cannot be in any room.
    """
    if member and member.matrix_user_id:
        return member.matrix_user_id
    profile = models.MatrixUserProfile.objects.filter(user=user).first()
    return profile.matrix_user_id if profile else None


@shared_task(
    name="waldur_mastermind.matrix_chat.kick_user_from_room",
    # User.DoesNotExist is a permanent miss and is not retried.
    **REVOCATION_RETRY,
)
def kick_user_from_room(room_uuid, user_uuid):
    """Remove a user from a Matrix room.

    Kicks the user's canonical Matrix ID rather than gating on a
    MatrixRoomMember row, which can be absent while the user is still joined.
    Failures propagate so Celery retries them: a swallowed error would leave a
    revoked user with chat access indefinitely.
    """
    if not matrix_client.is_enabled():
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.ACTIVE:
        return

    # Deactivation IS the revocation trigger here — User.objects (the active
    # manager) silently skips the row, leaving the user joined to the room
    # with a live access token. all_objects is the only correct queryset.
    try:
        user = User.all_objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        logger.error("User %s not found", user_uuid)
        return

    # Retries run for minutes; a role granted again meanwhile keeps the user in.
    if models.keeps_room_access(user, room):
        return

    member = models.MatrixRoomMember.objects.filter(room=room, user=user).first()
    matrix_user_id = _resolve_matrix_user_id(user, member)
    if not matrix_user_id:
        logger.info("User %s has no Matrix identity, nothing to kick", user)
        return

    matrix_client.kick_user(
        room.room_id, matrix_user_id, reason="Role revoked in Waldur"
    )

    if member:
        _record_left(member)
    logger.info("Kicked user %s from room %s", matrix_user_id, room.room_id)


@shared_task(name="waldur_mastermind.matrix_chat.export_room_history")
def export_room_history(export_uuid):
    """Export chat history from a Matrix room to a JSON file."""
    if not matrix_client.is_enabled():
        return

    try:
        export = models.MatrixHistoryExport.objects.get(uuid=export_uuid)
    except models.MatrixHistoryExport.DoesNotExist:
        logger.error("MatrixHistoryExport %s not found", export_uuid)
        return

    export.state = models.ExportStates.EXPORTING
    export.started_at = timezone.now()
    export.save(update_fields=["state", "started_at"])

    room = export.room
    all_messages = []
    from_token = None

    try:
        while True:
            result = matrix_client.get_room_messages(
                room.room_id, limit=100, from_token=from_token
            )
            messages = result["messages"]
            if not messages:
                break
            all_messages.extend(messages)
            from_token = result["end_token"]
            if not from_token:
                break

        # Media download phase
        media_count = 0
        if config.MATRIX_EXPORT_MEDIA:
            media_messages = [
                m for m in all_messages if m.get("has_media") and m.get("media_url")
            ]
            if media_messages:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for msg in media_messages:
                        try:
                            content_bytes, content_type, original_filename = (
                                matrix_client.download_media(msg["media_url"])
                            )
                            safe_event_id = re.sub(r"[^\w\-.]", "_", msg["event_id"])
                            safe_filename = re.sub(
                                r"[^\w\-.]", "_", msg.get("body", "file")
                            )
                            archive_name = f"{safe_event_id}_{safe_filename}"
                            zf.writestr(archive_name, content_bytes)
                            msg["media_path"] = archive_name
                            media_count += 1
                        except Exception:
                            logger.warning(
                                "Failed to download media %s for event %s",
                                msg["media_url"],
                                msg["event_id"],
                                exc_info=True,
                            )

                if media_count > 0:
                    zip_buffer.seek(0)
                    zip_filename = f"matrix_media_{room.uuid}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.zip"
                    export.media_file.save(zip_filename, ContentFile(zip_buffer.read()))

        export_data = {
            "room_id": room.room_id,
            "room_name": room.room_name,
            "exported_at": timezone.now().isoformat(),
            "message_count": len(all_messages),
            "media_count": media_count,
            "messages": all_messages,
        }

        json_content = json.dumps(export_data, indent=2, default=str)
        filename = (
            f"matrix_export_{room.uuid}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        export.export_file.save(filename, ContentFile(json_content.encode("utf-8")))
        export.message_count = len(all_messages)
        export.media_count = media_count
        export.state = models.ExportStates.COMPLETED
        export.completed_at = timezone.now()
        export.save(
            update_fields=["message_count", "media_count", "state", "completed_at"]
        )

        logger.info(
            "Exported %d messages and %d media files from room %s",
            len(all_messages),
            media_count,
            room.room_id,
        )
    except Exception as e:
        export.state = models.ExportStates.FAILED
        export.error_message = str(e)
        export.save(update_fields=["state", "error_message"])
        logger.exception("Failed to export history for room %s", room.room_id)


@shared_task(name="waldur_mastermind.matrix_chat.disable_room")
def disable_room(room_uuid, delete_history=False, reason=""):
    """Disable a Matrix room: kick members, export history, and archive."""
    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.DISABLING:
        logger.warning("Room %s is not in DISABLING state, skipping", room_uuid)
        return

    try:
        # 1. Notify room before kicking members. The reason is shown only when
        # set (e.g. project termination); manual disables stay unattributed.
        if matrix_client.is_enabled() and room.room_id:
            notice = (
                f"Chat room was deactivated due to {reason}"
                if reason
                else "Chat room was deactivated"
            )
            try:
                # Members must read it before they are removed below.
                post_as_bot(room, notice, wait=True)
            except Exception:
                logger.warning(
                    "Failed to send disable notification to room %s", room.room_id
                )

        # 2. Kick all joined/invited members
        if matrix_client.is_enabled() and room.room_id:
            for member in room.members.exclude(
                membership_state__in=[
                    models.MembershipStates.LEFT,
                    models.MembershipStates.BANNED,
                ]
            ):
                _kick_or_retry(member, "Chat room was deactivated")

        # 3. Export history if enabled, unless the caller is discarding it anyway
        if config.MATRIX_HISTORY_EXPORT_ENABLED and not delete_history:
            export = models.MatrixHistoryExport.objects.create(
                room=room,
                export_type=models.ExportTypes.ON_DELETION,
            )
            export_room_history(str(export.uuid))

        # 4. Optionally delete all history exports, files included
        if delete_history:
            room.exports.all().delete()

        # 5. Transition to archived
        room.set_archived()
        room.save(update_fields=["state"])
        logger.info("Disabled and archived Matrix room %s", room.room_id)

    except Exception as e:
        room.set_erred()
        room.error_message = str(e)
        room.save(update_fields=["state", "error_message"])
        logger.exception("Failed to disable room %s", room.room_id)


@shared_task(name="waldur_mastermind.matrix_chat.periodic_history_export")
def periodic_history_export():
    """Periodically export history for all active rooms."""
    if not config.MATRIX_HISTORY_EXPORT_ENABLED:
        return
    # The export task does nothing without Matrix, so every row queued here
    # would stay pending.
    if not matrix_client.is_enabled():
        return

    rooms = models.MatrixRoom.objects.filter(state=models.RoomStates.ACTIVE)
    for room in rooms:
        export = models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=models.ExportTypes.PERIODIC,
        )
        export_room_history.delay(str(export.uuid))
        logger.info("Queued periodic export for room %s", room.room_id)


@shared_task(name="waldur_mastermind.matrix_chat.cleanup_old_history_exports")
def cleanup_old_history_exports():
    """Delete history exports, files included, past the retention period.

    Exports are full copies of the history, made daily, and keep messages
    that were redacted in the room afterwards.
    """
    retention_days = config.MATRIX_HISTORY_EXPORT_RETENTION_DAYS
    if retention_days <= 0:
        return
    cutoff = timezone.now() - timedelta(days=retention_days)
    # An archived room gets one export on deletion and none after it, so each
    # room's newest completed export is kept: it may be the only copy left.
    newer_completed = models.MatrixHistoryExport.objects.filter(
        room=OuterRef("room"),
        state=models.ExportStates.COMPLETED,
        created__gt=OuterRef("created"),
    )
    # Unfinished exports go too: after a whole retention period their task is
    # no longer running, so none is left to save the row back.
    expired = (
        models.MatrixHistoryExport.objects.filter(created__lt=cutoff)
        .filter(~Q(state=models.ExportStates.COMPLETED) | Exists(newer_completed))
        # Every chunk runs this query again; the default ordering would sort
        # all of it each time.
        .order_by()
    )
    deleted = 0
    while pks := list(expired.values_list("pk", flat=True)[:EXPORT_CLEANUP_CHUNK_SIZE]):
        count, _ = models.MatrixHistoryExport.objects.filter(pk__in=pks).delete()
        deleted += count
    if deleted:
        logger.info(
            "Deleted %d Matrix history exports older than %d days",
            deleted,
            retention_days,
        )


@shared_task(name="waldur_mastermind.matrix_chat.send_room_notification")
def send_room_notification(room_uuid, body):
    """Send a notification message to a Matrix room as the bot."""
    if not matrix_client.is_enabled():
        return

    try:
        room = models.MatrixRoom.objects.get(uuid=room_uuid)
    except models.MatrixRoom.DoesNotExist:
        logger.error("MatrixRoom %s not found", room_uuid)
        return

    if room.state != models.RoomStates.ACTIVE:
        logger.warning("Room %s is not active, skipping notification", room.room_id)
        return

    try:
        post_as_bot(room, body)
        logger.info("Posted notification to room %s", room.room_id)
    except Exception:
        logger.exception("Failed to send notification to room %s", room.room_id)


def _get_project_for_room(room_id):
    """Look up the Project linked to a Matrix room by its room_id."""
    try:
        matrix_room = models.MatrixRoom.objects.get(
            room_id=room_id, state=models.RoomStates.ACTIVE
        )
    except models.MatrixRoom.DoesNotExist:
        return None
    return matrix_room.project


ORDER_STATE_LABELS = {
    1: "pending-consumer",
    2: "executing",
    3: "done",
    4: "erred",
    5: "canceled",
    6: "rejected",
    7: "pending-provider",
}


def _cmd_help(room_id, sender, event_id):
    """Return help text listing available commands."""
    return (
        "**Available commands:**\n\n"
        "- `!help` — Show this help message\n"
        "- `!status` — Show project resource status summary\n"
        "- `!orders` — Show last 5 orders for this project\n"
        "- `!members` — List room members and their roles"
    )


def _cmd_status(room_id, sender, event_id):
    """Surface project health: errored resources, pending approvals, in-flight ops."""
    from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
    from waldur_mastermind.marketplace.models import Order, Resource

    project = _get_project_for_room(room_id)
    if not project:
        return "This room is not linked to a project."

    errored_qs = Resource.objects.filter(project=project, state=ResourceStates.ERRED)
    errored_total = errored_qs.count()
    errored_names = list(errored_qs.values_list("name", flat=True)[:5])

    pending_approval = Order.objects.filter(
        project=project,
        state__in=[OrderStates.PENDING_CONSUMER, OrderStates.PENDING_PROVIDER],
    ).count()

    in_flight_states = [
        (ResourceStates.CREATING, "creating"),
        (ResourceStates.UPDATING, "updating"),
        (ResourceStates.TERMINATING, "terminating"),
    ]
    in_flight_counts = {
        label: Resource.objects.filter(project=project, state=state).count()
        for state, label in in_flight_states
    }

    lines = []
    if errored_total:
        shown = ", ".join(formatting.code_span(name) for name in errored_names)
        if errored_total > len(errored_names):
            shown += f", +{errored_total - len(errored_names)} more"
        noun = "resource" if errored_total == 1 else "resources"
        lines.append(f"- **{errored_total} {noun} errored:** {shown}")
    if pending_approval:
        noun = "order" if pending_approval == 1 else "orders"
        lines.append(f"- **{pending_approval} {noun}** pending approval")
    for label, count in in_flight_counts.items():
        if count:
            lines.append(f"- {count} {label}")

    if not lines:
        return f"**{formatting.escape_markdown(project.name)}** — status: all clear."
    return f"**{formatting.escape_markdown(project.name)}** — status:\n\n" + "\n".join(
        lines
    )


def _cmd_orders(room_id, sender, event_id):
    """Show last 5 orders for the linked project."""
    from waldur_mastermind.marketplace.models import Order

    project = _get_project_for_room(room_id)
    if not project:
        return "This room is not linked to a project."

    orders = Order.objects.filter(project=project).order_by("-created")[:5]
    if not orders:
        return f"**{formatting.escape_markdown(project.name)}**: no orders."

    lines = [
        f"**{formatting.escape_markdown(project.name)}** — last {len(orders)} orders:\n"
    ]
    for o in orders:
        state_label = ORDER_STATE_LABELS.get(o.state, f"unknown({o.state})")
        resource_name = o.resource.name if o.resource else "N/A"
        lines.append(
            f"- `{state_label}` · {o.type} · {formatting.code_span(resource_name)} · "
            f"{o.created.strftime('%Y-%m-%d')}"
        )
    return "\n".join(lines)


def _cmd_members(room_id, sender, event_id):
    """List room members with their roles."""
    try:
        matrix_room = models.MatrixRoom.objects.get(
            room_id=room_id, state=models.RoomStates.ACTIVE
        )
    except models.MatrixRoom.DoesNotExist:
        return "This room is not linked to a project."

    members = models.MatrixRoomMember.objects.filter(
        room=matrix_room, membership_state=models.MembershipStates.JOINED
    ).select_related("user")

    # The bot is always joined to an active room but isn't tracked as a
    # MatrixRoomMember (it has no Waldur User), so prepend it explicitly.
    lines = [f"- `{matrix_client.get_bot_user_id()}` — bot"]
    for m in members:
        role_label = "**admin**" if m.power_level >= 50 else "member"
        lines.append(f"- `{m.matrix_user_id}` — {role_label}")

    return f"**Room members ({len(lines)}):**\n\n" + "\n".join(lines)


COMMAND_HANDLERS = {
    "help": _cmd_help,
    "status": _cmd_status,
    "orders": _cmd_orders,
    "members": _cmd_members,
}


def _sender_has_project_access(sender, room_id):
    """Confirm a Matrix sender maps to a Waldur user with an active project role.

    Bot commands (`!status`/`!orders`/`!members`) expose project-scoped data,
    so federated rooms — or any room with non-Waldur participants — must not
    leak it. Returns the Waldur User on success, None otherwise.
    """
    try:
        profile = models.MatrixUserProfile.objects.select_related("user").get(
            matrix_user_id=sender
        )
    except models.MatrixUserProfile.DoesNotExist:
        return None
    user = profile.user
    if not user.is_active:
        return None

    try:
        room = models.MatrixRoom.objects.get(
            room_id=room_id, state=models.RoomStates.ACTIVE
        )
    except models.MatrixRoom.DoesNotExist:
        return None
    project = room.project
    if not project:
        return None

    if not models.has_room_role(user, room):
        return None
    return user


ACCESS_DENIED_REPLY = "You don't have access to this room's project in Waldur."


def _parse_command(body):
    parts = body.strip()[1:].split(None, 1)
    return parts[0].lower() if parts else ""


def _command_reply(room_id, sender, event_id, command):
    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        return (
            f"**Unknown command:** {formatting.code_span('!' + command)}\n\n"
            + _cmd_help(room_id, sender, event_id)
        )
    try:
        return handler(room_id, sender, event_id)
    except Exception:
        logger.exception("Error handling command !%s in room %s", command, room_id)
        return (
            f"**Error** processing command {formatting.code_span('!' + command)}. "
            "Please try again later."
        )


def answer_command(room_id, sender, event_id, body):
    """Answer a ``!command`` the bot read in a room, through the outbox.

    Commands return project data, so only a sender with a role on the room's
    project gets an answer; anyone else is told they have no access.
    """
    if not matrix_client.is_enabled():
        return
    room = models.MatrixRoom.objects.filter(
        room_id=room_id, state=models.RoomStates.ACTIVE
    ).first()
    if room is None:
        return
    if not bot_state.may_reply(room):
        logger.warning(
            "Too many commands in %s; leaving %s unanswered", room_id, event_id
        )
        return
    if _sender_has_project_access(sender, room_id) is None:
        reply = ACCESS_DENIED_REPLY
    else:
        reply = _command_reply(room_id, sender, event_id, _parse_command(body))
    bot_state.enqueue(room, reply, reply_to=event_id)


# Retention for the idempotency-key table. Old transactions never need to be
# replayed (the homeserver retries via the same txn_id, which we've already
# acked), so 30 days is conservative breathing room for debugging.
APPSERVICE_TRANSACTION_RETENTION_DAYS = 30


@shared_task(name="waldur_mastermind.matrix_chat.cleanup_old_outbox_messages")
def cleanup_old_outbox_messages():
    """Prune sent and failed outbox messages older than the retention window."""
    cutoff = timezone.now() - timedelta(days=OUTBOX_RETENTION_DAYS)
    deleted, _ = models.MatrixOutboxMessage.objects.filter(
        state__in=[models.OutboxStates.SENT, models.OutboxStates.FAILED],
        modified__lt=cutoff,
    ).delete()
    return {"status": "success", "deleted_count": deleted}


@shared_task(name="waldur_mastermind.matrix_chat.cleanup_old_appservice_transactions")
def cleanup_old_appservice_transactions():
    """Prune MatrixAppserviceTransaction rows older than the retention window."""
    cutoff = timezone.now() - timedelta(days=APPSERVICE_TRANSACTION_RETENTION_DAYS)
    old = models.MatrixAppserviceTransaction.objects.filter(processed_at__lt=cutoff)
    count = old.count()
    if count == 0:
        return {"status": "success", "deleted_count": 0}
    old.delete()
    logger.info("Pruned %d MatrixAppserviceTransaction rows", count)
    return {"status": "success", "deleted_count": count}


def reprovision_rooms():
    """Reset every active room and provisioned profile so the homeserver rebuilds them.

    Shared by the admin endpoint and the reprovision_matrix_rooms command rather
    than written twice: the ordering here is easy to get subtly wrong. Rows are
    locked before the state check so a concurrent disable or retry cannot race
    the write-back, and creation is queued on commit so a worker cannot pick up
    a room whose reset has not landed yet.

    Returns (rooms_reprovisioned, users_reset).
    """
    room_count = 0
    with transaction.atomic():
        locked_rooms = list(
            models.MatrixRoom.objects.select_for_update().filter(
                state=models.RoomStates.ACTIVE
            )
        )
        for room in locked_rooms:
            try:
                room.begin_reprovisioning()
            except TransitionNotAllowed:
                continue
            room.room_id = None
            room.room_alias = ""
            room.save(update_fields=["state", "error_message", "room_id", "room_alias"])
            room_uuid = str(room.uuid)
            transaction.on_commit(lambda uuid=room_uuid: create_room.delay(uuid))
            room_count += 1

        user_count = models.MatrixUserProfile.objects.filter(provisioned=True).update(
            provisioned=False,
            provisioned_at=None,
        )

    return room_count, user_count
