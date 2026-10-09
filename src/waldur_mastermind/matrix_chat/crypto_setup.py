"""Escrowing users' recovery keys, and the lease that guards setting them.

The chat drawer sets up end-to-end encryption once per user: secret storage, then
cross-signing, key backup and a dehydrated device. Tuwunel accepts a user's first
cross-signing keys without interactive auth but refuses to replace them unless the
user can type a password, which Waldur's users don't have. Two things follow:

- Only one browser may set up at a time, and Waldur must hold the recovery key
  before anything is uploaded. A lease admits one browser; the escrow call takes
  the key only from the lease holder.
- An identity whose recovery key is lost can only be reset with a password. The
  reset lease sets a temporary one through the admin API, the drawer uses it for
  interactive auth, and the password is replaced with a discarded random one as
  soon as the new key is escrowed, or when the lease runs out.

A reset destroys the user's key backups, so it is refused unless the identity is
really locked: the homeserver has cross-signing keys for the user, and the key
Waldur holds is missing or does not open the user's secret storage.
"""

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import matrix_client, recovery_keys
from .models import CryptoLeaseKinds, MatrixUserProfile

logger = logging.getLogger(__name__)

# The lease outlives the escrow: the holder escrows before uploading anything, and
# another window must not start a setup of its own while those uploads run. The
# holder releases it when done; otherwise it runs out.
LEASE_TTL = timedelta(minutes=10)


class CryptoConflict(Exception):
    """The request can't proceed in the account's current state."""

    def __init__(self, state, detail, retry_after=None):
        super().__init__(detail)
        self.state = state
        self.detail = detail
        self.retry_after = retry_after


def is_identity_locked(profile):
    """Whether the user's cross-signing identity exists but Waldur can't unlock it.

    Decided from the homeserver's account data, which the user's own Matrix token
    can also write; the reset this allows is therefore logged.
    """
    if not matrix_client.get_cross_signing_master_key(profile.matrix_user_id):
        return False
    if not profile.recovery_key:
        return True
    _, key_info, master_stored = matrix_client.get_secret_storage_state(
        profile.matrix_user_id
    )
    # Storage the key opens but that lacks the master key (a setup that died
    # half-way) unlocks nothing either.
    return not (
        recovery_keys.recovery_key_opens(profile.recovery_key, key_info)
        and master_stored
    )


def _active_lease_conflict(profile, now):
    if profile.crypto_lease and profile.crypto_lease_expires_at > now:
        retry_after = int((profile.crypto_lease_expires_at - now).total_seconds()) + 1
        raise CryptoConflict(
            "in_progress",
            "Encryption is being set up in another window.",
            retry_after=retry_after,
        )


def acquire_lease(profile, kind):
    """Grant the caller the lease, and for a reset a temporary password.

    Returns ``(lease, expires_at, temporary_password)``; the password is None for
    a first setup.
    """
    if kind == CryptoLeaseKinds.BOOTSTRAP:
        # Decided by the homeserver, not by the key Waldur holds: a key left over
        # from another homeserver (or one reset meanwhile) opens nothing there and
        # is replaced by this setup.
        if matrix_client.get_cross_signing_master_key(profile.matrix_user_id):
            if profile.recovery_key:
                raise CryptoConflict("set_up", "Encryption is already set up.")
            # Set up elsewhere (Element), or set up here and the key was lost.
            raise CryptoConflict(
                "locked", "Encryption was set up without a key Waldur holds."
            )
    elif not is_identity_locked(profile):
        raise CryptoConflict("not_locked", "Encryption can be unlocked; no reset.")

    now = timezone.now()
    lease = secrets.token_urlsafe(32)
    with transaction.atomic():
        locked = MatrixUserProfile.objects.select_for_update().get(pk=profile.pk)
        _active_lease_conflict(locked, now)
        locked.crypto_lease = lease
        locked.crypto_lease_kind = kind
        locked.crypto_lease_expires_at = now + LEASE_TTL
        locked.save(
            update_fields=[
                "crypto_lease",
                "crypto_lease_kind",
                "crypto_lease_expires_at",
            ]
        )

    temporary_password = None
    if kind == CryptoLeaseKinds.RESET:
        temporary_password = matrix_client.new_password()
        # Recorded before the password exists, so the sweep catches it even if
        # the request dies right after setting it.
        MatrixUserProfile.objects.filter(pk=profile.pk).update(
            crypto_temporary_password_until=now + LEASE_TTL
        )
        try:
            matrix_client.set_password(profile.matrix_user_id, temporary_password)
        except Exception:
            release_lease(profile, lease)
            raise
    return lease, now + LEASE_TTL, temporary_password


def release_lease(profile, lease):
    """End the lease if it is still held; returns its kind, or None."""
    with transaction.atomic():
        held = (
            MatrixUserProfile.objects.select_for_update()
            .filter(pk=profile.pk, crypto_lease=lease)
            .first()
        )
        if not lease or not held:
            return None
        kind = held.crypto_lease_kind
        held.crypto_lease = ""
        held.crypto_lease_kind = ""
        held.crypto_lease_expires_at = None
        held.save(
            update_fields=[
                "crypto_lease",
                "crypto_lease_kind",
                "crypto_lease_expires_at",
            ]
        )
    return kind


def escrow(profile, lease, recovery_key):
    """Store the recovery key for the lease holder. Returns the lease kind.

    The lease is kept while the holder uploads the keys that go with it.
    """
    recovery_keys.decode_recovery_key(recovery_key)
    now = timezone.now()
    with transaction.atomic():
        locked = MatrixUserProfile.objects.select_for_update().get(pk=profile.pk)
        if (
            not lease
            or not secrets.compare_digest(locked.crypto_lease, lease)
            or locked.crypto_lease_expires_at is None
            or locked.crypto_lease_expires_at <= now
        ):
            raise CryptoConflict(
                "no_lease", "The setup took too long or another window took over."
            )
        kind = locked.crypto_lease_kind
        locked.recovery_key = recovery_key
        locked.save(update_fields=["recovery_key"])
    return kind
