"""The Matrix bot's state in Waldur's database: its identity, lease and outbox.

Plain Django, without matrix-nio, so the API and Celery can queue messages and
ask whether the bot is running without loading the bot's crypto.
"""

import json
import secrets
import time
from datetime import timedelta

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
