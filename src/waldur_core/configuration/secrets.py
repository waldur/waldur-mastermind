"""Encryption at rest for ``secret_field`` settings.

``secret_field`` values are Fernet-encrypted with the field-encryption key
(``waldur_core.core.encryption``) on write and decrypted on read, so a database
dump or backup does not hand over credentials such as the Matrix appservice
token. Callers of ``config.<KEY>`` see plaintext.

Encryption is unconditional, as for ``EncryptedTextField``: a token-shaped
value is wrapped rather than trusted. A secret no configured key can decrypt
reads as an unguessable per-process value, never as its ciphertext: several
secrets are compared with what callers send (the homeserver token, webhook
secrets), and the ciphertext is exactly what a database dump holds. Nor does
it read as empty, which some checks take to mean "no secret, skip the check".
"""

import logging
import secrets

from cryptography.fernet import InvalidToken

from waldur_core.configuration import registry
from waldur_core.core import encryption

logger = logging.getLogger(__name__)


# Name fragments used only when redacting log output. They also match
# non-secret settings (token limits, token URLs), so API responses must
# not use this heuristic.
CONSTANCE_SECRET_NAME_PARTS = ("password", "token", "secret")


def is_constance_secret(key, *, name_heuristic=False):
    """Whether a setting must be kept out of non-staff output.

    A ``secret_field`` declaration is the canonical marker. ``name_heuristic``
    also treats keys containing "password", "token", or "secret" as secret.
    That extra match is for log lines, where hiding a token limit is harmless
    and it catches settings declared before ``secret_field`` existed.
    """
    # The management command passes keys as written in YAML, often lowercase.
    if key.upper() in registry.secret_keys():
        return True
    if not name_heuristic:
        return False
    lowered = key.lower()
    return any(part in lowered for part in CONSTANCE_SECRET_NAME_PARTS)


def redact_constance_secrets(values):
    """Return a copy with ``secret_field`` values replaced by an empty string."""
    hidden = registry.secret_keys()
    return {key: "" if key in hidden else value for key, value in values.items()}


def encrypt_secret(value):
    """The stored form of a secret setting's value."""
    if isinstance(value, str) and value:
        return encryption.encrypt_value(value)
    return value


# What a secret that can't be decrypted reads as: non-empty, so no check takes it
# for "unset", and unknowable, so nothing a caller sends can match it. One per
# key: a lost key makes every secret undecryptable at once, and outbound
# secrets go to third parties, so a shared value would let anyone who saw one
# (say, in a proxy log) pass a check made against another.
UNDECRYPTABLE_PREFIX = "undecryptable:"
_undecryptable = {}


def undecryptable_value(key):
    """The value an undecryptable secret ``key`` reads as, in this process."""
    return _undecryptable.setdefault(
        key, f"{UNDECRYPTABLE_PREFIX}{secrets.token_urlsafe(32)}"
    )


def is_undecryptable_value(value):
    return isinstance(value, str) and value.startswith(UNDECRYPTABLE_PREFIX)


def decrypt_stored(value):
    """The plaintext of a stored secret; None if no configured key decrypts it.

    A value that isn't a token is plaintext an earlier release stored.
    """
    if not encryption.is_encrypted(value):
        return value
    try:
        return encryption.decrypt_value(value)
    except InvalidToken:
        return None


def decrypt_secret(key, value):
    """The plaintext of a stored secret, or ``undecryptable_value(key)``.

    Anything that isn't a token passes through unchanged, including None. Only a
    token no configured key decrypts reads as a stand-in.
    """
    if not encryption.is_encrypted(value):
        return value
    plaintext = decrypt_stored(value)
    if plaintext is not None:
        return plaintext
    if key not in _undecryptable:
        logger.error(
            "Setting %s cannot be decrypted with any configured "
            "FIELD_ENCRYPTION_KEY; it is unusable until that key is restored "
            "or the setting is set again",
            key,
        )
    return undecryptable_value(key)
