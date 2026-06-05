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
from django.utils import timezone

from waldur_core.permissions.models import UserRole

from . import matrix_client, models

User = get_user_model()

logger = logging.getLogger(__name__)

# Power level granted to staff who self-join via the admin panel. 50 renders as
# the "Moderator" badge in Element — the same tier as customer owners.
STAFF_POWER_LEVEL = 50


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

    try:
        alias_localpart = None
        if room.project:
            alias_localpart = f"waldur-{room.project.uuid.hex[:8]}"

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

    # Get all active users in the project (direct + via customer)
    user_roles = UserRole.objects.filter(
        scope=project,
        is_active=True,
    ).select_related("user")

    customer_roles = UserRole.objects.filter(
        scope=project.customer,
        is_active=True,
    ).select_related("user")

    seen_users = set()
    for role in list(user_roles) + list(customer_roles):
        user = role.user
        if user.id in seen_users:
            continue
        seen_users.add(user.id)

        try:
            matrix_user_id = matrix_client.ensure_user_exists(user)
            power_level = matrix_client.get_power_level_for_scope(user, project)

            # Ensure display name is up to date
            display_name = user.full_name or user.username
            try:
                matrix_client.set_display_name(matrix_user_id, display_name)
            except Exception:
                logger.warning("Failed to update display name for %s", matrix_user_id)

            # Invite user and auto-join using the user's own access token
            matrix_client.invite_user(room.room_id, matrix_user_id)
            try:
                access_token = matrix_client.get_access_token_for_user(user)
                matrix_client.join_room_as_self(room.room_id, access_token)
                membership_state = models.MembershipStates.JOINED
            except Exception:
                logger.warning(
                    "Auto-join failed for %s in %s, left as invited",
                    matrix_user_id,
                    room.room_id,
                )
                membership_state = models.MembershipStates.INVITED

            # Set power level if non-default
            if power_level > 0:
                matrix_client.set_power_level(room.room_id, matrix_user_id, power_level)

            # Update or create member record
            models.MatrixRoomMember.objects.update_or_create(
                room=room,
                user=user,
                defaults={
                    "matrix_user_id": matrix_user_id,
                    "power_level": power_level,
                    "membership_state": membership_state,
                },
            )

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
        # TEMPORARY: staff/support self-join via the admin panel, so they
        # aren't project members and would otherwise be swept up here. Excluding
        # all staff/support is a blunt stopgap — a demoted ex-staff/support user
        # lingers until the next sync, and it doesn't address power-level drift
        # for those who are also plain project members. Revisit with an explicit
        # per-membership "manually joined" flag once this graduates past the demo.
        .exclude(user__is_staff=True)
        .exclude(user__is_support=True)
    )
    for member in stale_members:
        try:
            matrix_client.kick_user(
                room.room_id, member.matrix_user_id, reason="Role removed in Waldur"
            )
            member.membership_state = models.MembershipStates.LEFT
            member.save(update_fields=["membership_state"])
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
        power_level = matrix_client.get_power_level_for_scope(user, room.scope)

        matrix_client.invite_user(room.room_id, matrix_user_id)
        try:
            access_token = matrix_client.get_access_token_for_user(user)
            matrix_client.join_room_as_self(room.room_id, access_token)
            membership_state = models.MembershipStates.JOINED
        except Exception:
            logger.warning(
                "Auto-join failed for %s in %s, left as invited",
                matrix_user_id,
                room.room_id,
            )
            membership_state = models.MembershipStates.INVITED

        if power_level > 0:
            matrix_client.set_power_level(room.room_id, matrix_user_id, power_level)

        models.MatrixRoomMember.objects.update_or_create(
            room=room,
            user=user,
            defaults={
                "matrix_user_id": matrix_user_id,
                "power_level": power_level,
                "membership_state": membership_state,
            },
        )

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
            access_token = matrix_client.get_access_token_for_user(user)
            matrix_client.join_room_as_self(room.room_id, access_token)
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
            },
        )

        full_name = user.full_name or user.username
        try:
            matrix_client.send_message(room.room_id, f"{full_name} joined the room.")
        except Exception:
            logger.warning("Failed to announce staff join in room %s", room.room_id)

        logger.info("Staff %s joined room %s", matrix_user_id, room.room_id)
    except Exception:
        logger.exception("Failed to add staff %s to room %s", user, room.room_id)


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
        matrix_client.send_message(room.room_id, f"{full_name} left the room.")
    except Exception:
        logger.warning("Failed to announce staff leave in room %s", room.room_id)

    try:
        access_token = matrix_client.get_access_token_for_user(user)
        matrix_client.leave_room_as_self(room.room_id, access_token)
    except Exception:
        logger.exception(
            "Failed to leave room %s as staff %s", room.room_id, matrix_user_id
        )

    if member:
        member.membership_state = models.MembershipStates.LEFT
        member.save(update_fields=["membership_state"])
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
    # Narrow the retry surface: User.DoesNotExist is a permanent miss, not a
    # transient one — retrying it 3x with exponential backoff hides the error
    # for ~10 minutes. matrix_client transport / homeserver errors are the
    # legitimate retry cases.
    autoretry_for=(matrix_client.MatrixClientError, httpx.HTTPError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
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

    member = models.MatrixRoomMember.objects.filter(room=room, user=user).first()
    matrix_user_id = _resolve_matrix_user_id(user, member)
    if not matrix_user_id:
        logger.info("User %s has no Matrix identity, nothing to kick", user)
        return

    matrix_client.kick_user(
        room.room_id, matrix_user_id, reason="Role revoked in Waldur"
    )

    if member:
        member.membership_state = models.MembershipStates.LEFT
        member.save(update_fields=["membership_state"])
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
                matrix_client.send_message(room.room_id, notice)
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
                try:
                    matrix_client.kick_user(
                        room.room_id,
                        member.matrix_user_id,
                        reason="Chat room was deactivated",
                    )
                except Exception:
                    logger.warning(
                        "Failed to kick %s from room %s",
                        member.matrix_user_id,
                        room.room_id,
                    )
                member.membership_state = models.MembershipStates.LEFT
                member.save(update_fields=["membership_state"])

        # 3. Export history if enabled, unless the caller is discarding it anyway
        if config.MATRIX_HISTORY_EXPORT_ENABLED and not delete_history:
            export = models.MatrixHistoryExport.objects.create(
                room=room,
                export_type=models.ExportTypes.ON_DELETION,
            )
            export_room_history(str(export.uuid))

        # 4. Optionally delete all history exports
        if delete_history:
            for export in room.exports.all():
                if export.export_file:
                    export.export_file.delete(save=False)
                if export.media_file:
                    export.media_file.delete(save=False)
                export.delete()

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

    rooms = models.MatrixRoom.objects.filter(state=models.RoomStates.ACTIVE)
    for room in rooms:
        export = models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=models.ExportTypes.PERIODIC,
        )
        export_room_history.delay(str(export.uuid))
        logger.info("Queued periodic export for room %s", room.room_id)


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
        matrix_client.send_message(room.room_id, body)
        logger.info("Sent notification to room %s", room.room_id)
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
        shown = ", ".join(f"`{name}`" for name in errored_names)
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
        return f"**{project.name}** — status: all clear."
    return f"**{project.name}** — status:\n\n" + "\n".join(lines)


def _cmd_orders(room_id, sender, event_id):
    """Show last 5 orders for the linked project."""
    from waldur_mastermind.marketplace.models import Order

    project = _get_project_for_room(room_id)
    if not project:
        return "This room is not linked to a project."

    orders = Order.objects.filter(project=project).order_by("-created")[:5]
    if not orders:
        return f"**{project.name}**: no orders."

    lines = [f"**{project.name}** — last {len(orders)} orders:\n"]
    for o in orders:
        state_label = ORDER_STATE_LABELS.get(o.state, f"unknown({o.state})")
        resource_name = o.resource.name if o.resource else "N/A"
        lines.append(
            f"- `{state_label}` · {o.type} · `{resource_name}` · {o.created.strftime('%Y-%m-%d')}"
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

    has_project_role = UserRole.objects.filter(
        user=user, scope=project, is_active=True
    ).exists()
    has_customer_role = UserRole.objects.filter(
        user=user, scope=project.customer, is_active=True
    ).exists()
    if not (has_project_role or has_customer_role):
        return None
    return user


@shared_task(name="waldur_mastermind.matrix_chat.process_appservice_events")
def process_appservice_events(txn_id, events):
    """Process events received from the Matrix homeserver via appservice webhook."""
    if not matrix_client.is_enabled():
        return
    bot_user_id = matrix_client.get_bot_user_id()

    for event in events:
        event_type = event.get("type")
        if event_type != "m.room.message":
            continue

        content = event.get("content", {})
        if content.get("msgtype") != "m.text":
            continue

        sender = event.get("sender", "")
        if sender == bot_user_id:
            continue

        body = content.get("body", "").strip()
        if not body.startswith("!"):
            continue

        room_id = event.get("room_id", "")
        event_id = event.get("event_id", "")

        parts = body[1:].split(None, 1)
        command = parts[0].lower() if parts else ""

        # Bot commands return project-scoped data; gate dispatch on the
        # sender having an active Waldur role on the room's project. A
        # federated user or a room guest can still send the message, but
        # they get a friendly denial instead of project details.
        if _sender_has_project_access(sender, room_id) is None:
            try:
                matrix_client.send_reply(
                    room_id,
                    event_id,
                    "You don't have access to this room's project in Waldur.",
                )
            except Exception:
                logger.warning(
                    "Failed to send access-denied reply to %s in %s",
                    sender,
                    room_id,
                )
            continue

        handle_bot_command.delay(room_id, sender, event_id, command)


@shared_task(name="waldur_mastermind.matrix_chat.handle_bot_command")
def handle_bot_command(room_id, sender, event_id, command):
    """Execute a bot command and send the response as a reply."""
    if not matrix_client.is_enabled():
        return

    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        reply = f"**Unknown command:** `!{command}`\n\n" + _cmd_help(
            room_id, sender, event_id
        )
    else:
        try:
            reply = handler(room_id, sender, event_id)
        except Exception:
            logger.exception("Error handling command !%s in room %s", command, room_id)
            reply = (
                f"**Error** processing command `!{command}`. Please try again later."
            )

    try:
        matrix_client.send_reply(room_id, event_id, reply)
    except Exception:
        logger.exception("Failed to send reply for !%s in room %s", command, room_id)


# Retention for the idempotency-key table. Old transactions never need to be
# replayed (the homeserver retries via the same txn_id, which we've already
# acked), so 30 days is conservative breathing room for debugging.
APPSERVICE_TRANSACTION_RETENTION_DAYS = 30


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
