"""Server-side key backup (``m.megolm_backup.v1.curve25519-aes-sha2``) for the bot.

The bot writes Megolm sessions into a member's backup, encrypted to the backup's
public key, which the member's clients decrypt with the private key they keep in
secret storage. Whoever controls that public key reads what the bot writes, so
the bot writes only into a backup it can tie to the member:

1. When Waldur holds the member's recovery key, the backup must match it, with
   no fallback: the recovery key opens the member's secret storage, which holds
   the backup's private key, and that key's public half must be the backup's
   public key. A homeserver can't forge that match, because secret storage is
   encrypted and authenticated under the recovery key. It can only withhold the
   secret, and then the bot writes nothing. What remains trusted is Waldur's own
   database, which holds the recovery key anyway.
2. When Waldur holds no recovery key for the member (set up in another client),
   the backup's ``auth_data`` must be signed by the member's master
   cross-signing key, or by a device that key vouches for, and that master key
   must be the one pinned for the member when the bot first saw it. A changed
   master key is refused until a recovery key is escrowed with Waldur, which
   clears the pin (and from then on case 1 applies). What remains trusted is
   the homeserver at the moment of first sight: one already compromised then
   can pin its own key.
"""

import base64
import binascii
import hashlib
import hmac
import json

import vodozemac
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from waldur_mastermind.matrix_chat import recovery_keys
from waldur_mastermind.matrix_chat.crypto import cross_signing

BACKUP_ALGORITHM = "m.megolm_backup.v1.curve25519-aes-sha2"
MEGOLM_ALGORITHM = "m.megolm.v1.aes-sha2"
# The secret-storage secret holding the backup's private key.
BACKUP_SECRET = "m.megolm_backup.v1"
SECRET_STORAGE_KEY_PREFIX = "m.secret_storage.key."


def _decode(text):
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def backup_public_key(version_info):
    """The public key of a ``room_keys/version`` answer, or None if it is not a
    backup the bot can write to."""
    if not isinstance(version_info, dict):
        return None
    if version_info.get("algorithm") != BACKUP_ALGORITHM:
        return None
    auth_data = version_info.get("auth_data")
    key = auth_data.get("public_key") if isinstance(auth_data, dict) else None
    if not isinstance(key, str):
        return None
    try:
        if len(_decode(key)) != 32:
            return None
    except (binascii.Error, ValueError):
        return None
    return key


def secret_key_ids(secret):
    """The secret-storage key ids a secret's account data is encrypted under."""
    encrypted = secret.get("encrypted") if isinstance(secret, dict) else None
    return list(encrypted) if isinstance(encrypted, dict) else []


def decrypt_secret(secret_key, name, encrypted):
    """A secret-storage secret, decrypted with the 32-byte ``secret_key``.

    ``encrypted`` is the ``{iv, ciphertext, mac}`` stored for one key id. None if
    the MAC does not match: the secret was not written under this key.
    """
    if not isinstance(encrypted, dict):
        return None
    iv, ciphertext, mac = (encrypted.get(k) for k in ("iv", "ciphertext", "mac"))
    if not all(isinstance(v, str) for v in (iv, ciphertext, mac)):
        return None
    try:
        iv, ciphertext, mac = _decode(iv), _decode(ciphertext), _decode(mac)
    except (binascii.Error, ValueError):
        return None
    if len(iv) != 16:
        return None
    derived = HKDF(
        algorithm=hashes.SHA256(), length=64, salt=b"\x00" * 32, info=name.encode()
    ).derive(secret_key)
    expected = hmac.new(derived[32:], ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        return None
    decryptor = Cipher(algorithms.AES(derived[:32]), modes.CTR(iv)).decryptor()
    return (decryptor.update(ciphertext) + decryptor.finalize()).decode()


def public_key_of(private_key):
    """The unpadded base64 Curve25519 public key of a base64 private key."""
    raw = _decode(private_key)
    public = (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return cross_signing.encode(public)


def key_in_secret_storage(recovery_key, secret, key_descriptions):
    """The backup public key the user's secret storage holds, as far as the
    recovery key opens it; None if it opens none of the secret's keys.

    ``secret`` is the ``m.megolm_backup.v1`` account data, ``key_descriptions``
    maps key ids to their ``m.secret_storage.key.<id>`` account data.
    """
    if not recovery_key:
        return None
    try:
        return _key_in_secret_storage(recovery_key, secret, key_descriptions)
    except Exception:  # noqa: BLE001 - malformed account data means "no key"
        # Caught here, so no traceback carrying the derived key material in its
        # frame locals reaches the bot's error logging.
        return None


def _key_in_secret_storage(recovery_key, secret, key_descriptions):
    try:
        secret_key = recovery_keys.decode_recovery_key(recovery_key)
    except recovery_keys.InvalidRecoveryKey:
        return None
    for key_id in secret_key_ids(secret):
        if not recovery_keys.recovery_key_opens(
            recovery_key, key_descriptions.get(key_id)
        ):
            continue
        private_key = decrypt_secret(
            secret_key, BACKUP_SECRET, secret["encrypted"][key_id]
        )
        if private_key is None:
            continue
        try:
            return public_key_of(private_key)
        except (binascii.Error, ValueError):
            continue
    return None


def signed_by_owner(version_info, user_id, keys):
    """Whether the backup's ``auth_data`` is signed by the user's master key, or
    by a device of theirs that it vouches for. ``keys`` is a /keys/query body."""
    auth_data = version_info.get("auth_data")
    if not isinstance(auth_data, dict):
        return False
    master = cross_signing.published_key(
        (keys.get("master_keys") or {}).get(user_id), user_id, cross_signing.MASTER
    )
    if master and cross_signing.has_signature(auth_data, user_id, master):
        return True
    return any(
        cross_signing.has_signature(
            auth_data, user_id, ed25519, key_id=f"ed25519:{device_id}"
        )
        for device_id, ed25519 in cross_signing.cross_signed_devices(
            user_id, keys
        ).items()
    )


def session_backup(session, public_key):
    """The ``KeyBackupData`` of an nio inbound group session, encrypted to the
    backup's ``public_key``, from the session's first known index."""
    plaintext = {
        "algorithm": MEGOLM_ALGORITHM,
        "sender_key": session.sender_key,
        "sender_claimed_keys": {"ed25519": session.ed25519},
        "forwarding_curve25519_key_chain": list(session.forwarding_chain),
        "session_key": session.export_session(session.first_known_index),
    }
    encryption = vodozemac.PkEncryption.from_key(
        vodozemac.Curve25519PublicKey.from_base64(public_key)
    )
    # vodozemac encrypts as libolm did, which clients expect: X25519 with an
    # ephemeral key, HKDF-SHA256 into an AES-256-CBC key, HMAC key and IV, and
    # a MAC that is libolm's quirk: HMAC-SHA256 over the EMPTY string,
    # truncated to 8 bytes, so it authenticates nothing. The ciphertext's
    # integrity rests on the backup's single-key encryption alone, as it does
    # for every client writing this backup algorithm.
    # By attribute: Message.to_base64() returns its parts in another order
    # than its type stub declares.
    message = encryption.encrypt(json.dumps(plaintext, separators=(",", ":")).encode())
    return {
        "first_message_index": session.first_known_index,
        "forwarded_count": len(session.forwarding_chain),
        # Restored keys never count as verified: the member's client did not
        # receive them from the sender.
        "is_verified": False,
        "session_data": {
            "ephemeral": cross_signing.encode(message.ephemeral_key),
            "ciphertext": cross_signing.encode(message.ciphertext),
            "mac": cross_signing.encode(message.mac),
        },
    }
