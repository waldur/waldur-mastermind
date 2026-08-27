import logging

from celery import shared_task

from waldur_core.passkeys import services

logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.passkeys.cleanup_expired_ceremonies")
def cleanup_expired_ceremonies():
    """Delete ceremony rows that can no longer be used.

    Ceremonies are created by unauthenticated callers, so without a reaper the
    table grows with every abandoned login attempt. They are useless the moment
    they expire — nothing reads a stale row — so this is pure garbage
    collection rather than a retention policy.
    """
    deleted, _ = services.purge_expired_ceremonies()
    if deleted:
        logger.info("Deleted %d expired passkey ceremonies.", deleted)
