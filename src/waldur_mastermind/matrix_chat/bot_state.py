"""The Matrix bot's state in Waldur's database: its identity, lease and outbox.

Plain Django, without matrix-nio, so the API and Celery can queue messages and
ask whether the bot is running without loading the bot's crypto.
"""

import json
import secrets
import time
from datetime import timedelta

from django.db import transaction
from django.db.models import Case, DateTimeField, ExpressionWrapper, F, Q, Value, When
from django.db.models.functions import Now
from django.utils import timezone

from waldur_mastermind.matrix_chat import models

# The holder renews the lease well within its lifetime and stops as soon as a
# renewal fails, so a lease that ran out means its holder is gone or stuck.
LEASE_TTL = timedelta(seconds=60)
LEASE_RENEW_INTERVAL = timedelta(seconds=15)

# Retry delays for a message the bot could not send.
OUTBOX_RETRY_DELAYS = [timedelta(seconds=s) for s in (5, 30, 120, 600, 1800)]
OUTBOX_MAX_ATTEMPTS = len(OUTBOX_RETRY_DELAYS) + 1


class LeaseHeld(RuntimeError):
    """Another bot process holds the lease."""


def new_device_id():
    return "WALDURBOT" + secrets.token_hex(4).upper()


def get_or_create_identity(user_id):
    identity, _ = models.MatrixBotIdentity.objects.get_or_create(
        user_id=user_id,
        defaults={
            "device_id": new_device_id(),
            "pickle_key": secrets.token_urlsafe(32),
        },
    )
    return identity


# Lease times are the database's clock, not each process's: two hosts whose
# clocks disagree must still agree on whether a lease has run out.
def _lease_end():
    return ExpressionWrapper(Now() + Value(LEASE_TTL), output_field=DateTimeField())


_LEASE_LIVE = Q(lease_expires_at__gt=Now())


def acquire_lease(user_id, holder):
    """Take the bot's lease for ``holder``, or raise LeaseHeld."""
    get_or_create_identity(user_id)
    taken = (
        models.MatrixBotIdentity.objects.filter(user_id=user_id)
        .filter(Q(lease_holder="") | Q(lease_holder=holder) | ~_LEASE_LIVE)
        .update(lease_holder=holder, lease_expires_at=_lease_end())
    )
    if not taken:
        expires_at = (
            models.MatrixBotIdentity.objects.filter(user_id=user_id)
            .values_list("lease_expires_at", flat=True)
            .first()
        )
        raise LeaseHeld(
            "Another Matrix bot process holds the lease until "
            f"{expires_at.isoformat() if expires_at else 'it renews it'}."
        )


def renew_lease(user_id, holder):
    """Extend the lease; False if ``holder`` no longer has it."""
    return bool(
        models.MatrixBotIdentity.objects.filter(
            _LEASE_LIVE, user_id=user_id, lease_holder=holder
        ).update(lease_expires_at=_lease_end())
    )


def release_lease(user_id, holder):
    models.MatrixBotIdentity.objects.filter(
        user_id=user_id, lease_holder=holder
    ).update(lease_holder="", lease_expires_at=None)


def is_bot_running(user_id):
    """Whether a bot process holds a live lease, and so drains the outbox."""
    return models.MatrixBotIdentity.objects.filter(
        _LEASE_LIVE, user_id=user_id
    ).exists()


def load_cross_signing_seeds(identity):
    """``{usage: seed bytes}``, empty until the bot has published its keys."""
    if not identity.cross_signing_seeds:
        return {}
    return {
        usage: bytes.fromhex(seed)
        for usage, seed in json.loads(identity.cross_signing_seeds).items()
    }


def save_cross_signing_seeds(identity, seeds):
    identity.cross_signing_seeds = json.dumps(
        {usage: seed.hex() for usage, seed in seeds.items()}
    )
    identity.save(update_fields=["cross_signing_seeds", "modified"])


def save_access_token(identity, token, homeserver_url):
    identity.access_token = token
    identity.access_token_homeserver = homeserver_url
    identity.save(update_fields=["access_token", "access_token_homeserver", "modified"])


def enqueue(room, body, reply_to=""):
    """Queue ``body`` for the bot to post in ``room``.

    A reply to an event is queued once: a command the bot sees again after a
    restart is not answered twice.
    """
    if reply_to:
        message, _ = models.MatrixOutboxMessage.objects.get_or_create(
            room=room, reply_to=reply_to, defaults={"body": body}
        )
        return message
    return models.MatrixOutboxMessage.objects.create(room=room, body=body)


def wait_until_sent(message, timeout_seconds):
    """Wait for the bot to post ``message``; False if it has not in time."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = (
            models.MatrixOutboxMessage.objects.filter(pk=message.pk)
            .values_list("state", flat=True)
            .first()
        )
        if state != models.OutboxStates.PENDING:
            return state == models.OutboxStates.SENT
        time.sleep(1)
    return False


def due_messages(limit=20):
    """Pending messages whose time has come: Waldur's notices, then command
    replies, oldest first, so a burst of commands cannot hold up a notice."""
    return list(
        models.MatrixOutboxMessage.objects.filter(
            state=models.OutboxStates.PENDING, next_attempt_at__lte=timezone.now()
        )
        .select_related("room")
        .order_by(
            Case(When(reply_to="", then=Value(0)), default=Value(1)), "created", "id"
        )[:limit]
    )


# At most this many command replies per room and minute; commands beyond it go
# unanswered, so no room member can flood the bot's outbox.
REPLY_LIMIT_PER_MINUTE = 10


def may_reply(room):
    since = timezone.now() - timedelta(minutes=1)
    return (
        models.MatrixOutboxMessage.objects.filter(room=room, created__gte=since)
        .exclude(reply_to="")
        .count()
        < REPLY_LIMIT_PER_MINUTE
    )


def mark_sent(message, event_id):
    models.MatrixOutboxMessage.objects.filter(pk=message.pk).update(
        state=models.OutboxStates.SENT,
        event_id=event_id or "",
        sent_at=timezone.now(),
        attempts=F("attempts") + 1,
        error_message="",
    )


def mark_failed_attempt(message, error):
    """Schedule a retry, or give up once the attempts are used up."""
    attempts = message.attempts + 1
    update = {"attempts": attempts, "error_message": str(error)[:2000]}
    if attempts >= OUTBOX_MAX_ATTEMPTS:
        update["state"] = models.OutboxStates.FAILED
    else:
        update["next_attempt_at"] = timezone.now() + OUTBOX_RETRY_DELAYS[attempts - 1]
    models.MatrixOutboxMessage.objects.filter(pk=message.pk).update(**update)


def drop_undeliverable(message, reason):
    """Give up on a message whose room can no longer take it."""
    models.MatrixOutboxMessage.objects.filter(pk=message.pk).update(
        state=models.OutboxStates.FAILED, error_message=reason[:2000]
    )


# History exports. Only the bot holds the room keys, so it reads and writes
# every export; an export waiting in PENDING is a request for it.

_UNFINISHED_EXPORT = Q(
    state__in=[models.ExportStates.PENDING, models.ExportStates.EXPORTING]
)

# Manual exports a room may have requested in a day: the bot makes exports one
# at a time, so the room's managers must not be able to fill its queue.
MANUAL_EXPORTS_PER_DAY = 10


class ExportLimitReached(RuntimeError):
    """The room has had as many manual exports today as it may."""


def request_export(room, export_type):
    """``(export, created)``: the room's unfinished export of this type if it
    has one, else a new request. A room never waits in the queue twice for the
    same kind of export, however often one is asked for.

    Raises ExportLimitReached for a manual export past MANUAL_EXPORTS_PER_DAY.
    """
    with transaction.atomic():
        # Serialises requests for one room, so two cannot both find none.
        models.MatrixRoom.objects.select_for_update().filter(pk=room.pk).first()
        exports = models.MatrixHistoryExport.objects.filter(
            room=room, export_type=export_type
        )
        unfinished = exports.filter(_UNFINISHED_EXPORT).order_by("-created").first()
        if unfinished is not None:
            return unfinished, False
        if (
            export_type == models.ExportTypes.MANUAL
            and exports.filter(created__gte=timezone.now() - timedelta(days=1)).count()
            >= MANUAL_EXPORTS_PER_DAY
        ):
            raise ExportLimitReached(
                f"This room has had {MANUAL_EXPORTS_PER_DAY} history exports in "
                "the last 24 hours. Try again later."
            )
        return models.MatrixHistoryExport.objects.create(
            room=room, export_type=export_type
        ), True


# The order the bot takes exports in: a task that closes a room waits for its
# export, people wait for theirs, and the daily run waits for nobody.
_EXPORT_PRIORITY = Case(
    When(export_type=models.ExportTypes.ON_DELETION, then=Value(0)),
    When(export_type=models.ExportTypes.MANUAL, then=Value(1)),
    default=Value(2),
)


def deletion_export_waiting():
    """Whether the export of a room being closed waits for the bot."""
    return models.MatrixHistoryExport.objects.filter(
        state=models.ExportStates.PENDING,
        export_type=models.ExportTypes.ON_DELETION,
    ).exists()


def claim_next_export(deletions_only=False):
    """Take the next pending export for the bot: exports of rooms being closed
    first, which a task is waiting for, then manual ones, then the daily ones;
    the oldest first within each. None if there is none.

    ``deletions_only`` takes only exports of rooms being closed.
    """
    pending = models.MatrixHistoryExport.objects.filter(
        state=models.ExportStates.PENDING
    )
    if deletions_only:
        pending = pending.filter(export_type=models.ExportTypes.ON_DELETION)
    while True:
        export = (
            pending.select_related("room")
            .order_by(_EXPORT_PRIORITY, "created", "id")
            .first()
        )
        if export is None:
            return None
        # The start time also tells this claim from a later one of the same
        # export, so a process that lost it cannot write over the next.
        started_at = timezone.now()
        claimed = models.MatrixHistoryExport.objects.filter(
            pk=export.pk, state=models.ExportStates.PENDING
        ).update(state=models.ExportStates.EXPORTING, started_at=started_at)
        if claimed:
            export.state = models.ExportStates.EXPORTING
            export.started_at = started_at
            return export


def claimed_export(export):
    """The export, as long as it is still this claim of it."""
    return models.MatrixHistoryExport.objects.filter(
        pk=export.pk,
        state=models.ExportStates.EXPORTING,
        started_at=export.started_at,
    )


def fail_interrupted_exports():
    """Fail the exports a bot process stopped in the middle of.

    Called by the process that holds the lease, before it exports anything, so
    every export still marked as running was left behind. Failed, not retried:
    an export that brings the bot down would otherwise do so on every start.
    """
    return models.MatrixHistoryExport.objects.filter(
        state=models.ExportStates.EXPORTING
    ).update(
        state=models.ExportStates.FAILED,
        error_message="The Matrix bot stopped during the export.",
    )


def requeue_export(export):
    """Put back an export the bot was stopped in the middle of, on purpose, or
    that gave way to one that must not wait."""
    claimed_export(export).update(state=models.ExportStates.PENDING, started_at=None)


def fail_export(export, reason):
    claimed_export(export).update(
        state=models.ExportStates.FAILED, error_message=reason[:2000]
    )


def wait_until_exported(export, timeout_seconds):
    """Wait for the bot to finish ``export``; its state then, or None in time."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        export.refresh_from_db(fields=["state", "error_message"])
        if export.state in (models.ExportStates.COMPLETED, models.ExportStates.FAILED):
            return export.state
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)
