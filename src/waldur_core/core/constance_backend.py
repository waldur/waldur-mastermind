"""Constance backend that keeps ``secret_field`` settings encrypted at rest.

django-constance's database backend stores every value in clear; the
``secret_field`` type only masks the value in the settings API. This backend
Fernet-encrypts those values with the field-encryption key
(``waldur_core.core.encryption``) on write and decrypts them on read, so a
database dump or backup does not hand over credentials such as the Matrix
appservice token. Callers of ``config.<KEY>`` see plaintext as before.

Encryption is unconditional, as for ``EncryptedTextField``: a token-shaped
value is wrapped rather than trusted. A secret no configured key can decrypt
reads as an unguessable per-process value, never as its ciphertext: several
secrets are compared with what callers send (the homeserver token, webhook
secrets), and the ciphertext is exactly what a database dump holds. Nor does
it read as empty, which some checks take to mean "no secret, skip the check".
Values written before this backend existed stay readable until the
``0053_encrypt_constance_secrets`` migration encrypts them.

Interim measure: the in-house settings store that replaces Constance is
expected to encrypt secret settings itself, and this backend goes with it.
"""

import logging
import secrets

from constance import settings as constance_settings
from constance.backends.database import DatabaseBackend
from cryptography.fernet import InvalidToken

from waldur_core.core import encryption

logger = logging.getLogger(__name__)

SECRET_FIELD_TYPE = "secret_field"


def secret_keys():
    """The Constance keys declared with the ``secret_field`` type."""
    return frozenset(
        key
        for key, options in constance_settings.CONFIG.items()
        if len(options) > 2 and options[2] == SECRET_FIELD_TYPE
    )


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

    Anything that isn't a token passes through unchanged, including None, which
    the backend returns for a setting with no stored row: Constance then uses
    the default. Only a token no configured key decrypts reads as a stand-in.
    """
    if not encryption.is_encrypted(value):
        return value
    plaintext = decrypt_stored(value)
    if plaintext is not None:
        return plaintext
    if key not in _undecryptable:
        logger.error(
            "Constance setting %s cannot be decrypted with any configured "
            "FIELD_ENCRYPTION_KEY; it is unusable until that key is restored "
            "or the setting is set again",
            key,
        )
    return undecryptable_value(key)


class EncryptedDatabaseBackend(DatabaseBackend):
    def __init__(self):
        self._secret_keys = secret_keys()
        super().__init__()

    def _read(self, key, value):
        if key in self._secret_keys:
            return decrypt_secret(key, value)
        return value

    def _stored_mget(self, keys):
        """Values as stored: ciphertext for secrets."""
        yield from super().mget(keys)

    def mget(self, keys):
        for key, value in self._stored_mget(keys):
            yield key, self._read(key, value)

    def get(self, key):
        return self._read(key, super().get(key))

    def set(self, key, value):
        if key in self._secret_keys:
            # A form that re-submits every setting sends back what it read,
            # which for an undecryptable secret is its stand-in: keep the
            # stored value rather than make the stand-in the real secret.
            if isinstance(value, str) and value.startswith(UNDECRYPTABLE_PREFIX):
                return
            # The cache and config_updated receive the stored form too, so
            # neither a shared cache nor a signal receiver ever holds a secret
            # in clear.
            value = encrypt_secret(value)
        super().set(key, value)

    def autofill(self):
        # DatabaseBackend.autofill fills the cache through self.mget, which would
        # put decrypted secrets into a cache other processes share.
        if not self._autofill_timeout or not self._cache:
            return
        full_cachekey = self.add_prefix(self._autofill_cachekey)
        if self._cache.get(full_cachekey):
            return
        autofill_values = {full_cachekey: 1}
        for key, value in self._stored_mget(constance_settings.CONFIG):
            autofill_values[self.add_prefix(key)] = value
        self._cache.set_many(autofill_values, timeout=self._autofill_timeout)
