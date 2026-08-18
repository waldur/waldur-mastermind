"""Application-level field encryption for secrets stored at rest.

Values are encrypted with Fernet under settings.FIELD_ENCRYPTION_KEY.
FIELD_ENCRYPTION_KEY_FALLBACKS enables MultiFernet key rotation: new writes
use the primary key, reads accept any listed key.

A dedicated FIELD_ENCRYPTION_KEY is the recommended setup: it has a separate
lifecycle from SECRET_KEY, so leaking Django settings must not, by itself, unlock
data at rest. When it is not set, encryption still works — the key is derived from
SECRET_KEY — so the feature is never left hard-broken by missing config; a warning
nudges operators to configure a dedicated key (which restores that separation).
The SECRET_KEY-derived key always remains an implicit last-resort decrypt
fallback, so rows written before a dedicated key was configured stay readable.
"""

import base64
import functools
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

logger = logging.getLogger(__name__)

# Every Fernet token starts with base64("\x80" + timestamp...), i.e. "gAAAA".
_FERNET_TOKEN_PREFIX = "gAAAA"

_fallback_warned = False


def _derive_key_from_secret() -> str:
    """A valid Fernet key derived deterministically from SECRET_KEY.

    Fallback when no dedicated FIELD_ENCRYPTION_KEY is configured. Deterministic so
    encrypt and decrypt agree across restarts and processes.
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def _resolve_keys() -> tuple[str, ...]:
    """The ordered key material: primary first, then every accepted decrypt key."""
    global _fallback_warned
    primary = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not primary:
        if not _fallback_warned:
            logger.warning(
                "FIELD_ENCRYPTION_KEY is not set; deriving the field-encryption key "
                "from SECRET_KEY. Set a dedicated FIELD_ENCRYPTION_KEY in production "
                "so leaking Django settings does not also unlock encrypted fields."
            )
            _fallback_warned = True
        primary = _derive_key_from_secret()
    keys = [primary] + list(getattr(settings, "FIELD_ENCRYPTION_KEY_FALLBACKS", []))
    # The SECRET_KEY-derived key is always an implicit last-resort fallback:
    # rows encrypted before a dedicated FIELD_ENCRYPTION_KEY was configured must
    # stay readable after one is introduced. This does not weaken at-rest
    # security — an attacker holding SECRET_KEY and a database dump can decrypt
    # those rows regardless of what this list contains.
    derived = _derive_key_from_secret()
    if derived not in keys:
        keys.append(derived)
    return tuple(keys)


@functools.lru_cache(maxsize=8)
def _build_fernet(keys: tuple[str, ...]) -> MultiFernet:
    """Build (and memoise) the MultiFernet for a given key tuple.

    Constructing a Fernet decodes the key and derives the signing/encryption halves.
    Doing that per value is measurable on read-heavy paths: ``from_db_value`` runs for
    every row loaded, so an offering list decrypts once per sensitive key per row.
    Cached on the key tuple rather than as a module singleton so ``override_settings``
    in tests, and a key rotation without a restart, both pick up new keys immediately.
    Fernet instances are stateless per operation, so sharing one across threads is safe.
    """
    return MultiFernet([Fernet(key) for key in keys])


def _get_fernet() -> MultiFernet:
    return _build_fernet(_resolve_keys())


def encrypt_value(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def rotate_value(ciphertext: str) -> str:
    """Re-encrypt an existing token under the primary key.

    ``MultiFernet.rotate`` decrypts with any configured key and re-encrypts with the
    first one. That is what makes retiring a fallback possible: until every row has
    been rotated there is no way to know whether an old key is still needed.
    """
    return _get_fernet().rotate(ciphertext.encode()).decode()


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_FERNET_TOKEN_PREFIX)


# The credential-key rule shared by every selectively-encrypted JSON column
# (Offering.secret_options, ServiceSettings.options). Values under these keys are
# unambiguous secrets: they grant access on their own and are never queried by value.
# The suffix form covers plugin-specific names automatically — vault_token,
# keycloak_password, client_secret, heappe_password, shared_user_password — so a new
# credential option is protected the day it is added rather than the day somebody
# notices. Deliberately narrow: endpoint URLs, usernames and identifiers stay
# plaintext, because they grant no access and keep the column readable for support.
_CREDENTIAL_KEYS = frozenset({"password", "token", "secret"})
_CREDENTIAL_SUFFIXES = ("_password", "_token", "_secret")


def is_credential_key(key) -> bool:
    """Whether a JSON value under this key is a credential that must be encrypted."""
    return isinstance(key, str) and (
        key in _CREDENTIAL_KEYS or key.endswith(_CREDENTIAL_SUFFIXES)
    )


def encrypt_dict_values(data, is_sensitive, skip_encrypted=False):
    """Return a copy of ``data`` with sensitive string values encrypted.

    ``is_sensitive(key)`` selects which keys to encrypt. Encryption is
    **unconditional**: a value that merely looks like a Fernet token is still
    encrypted (wrapped), so a caller cannot smuggle a chosen ciphertext into the
    column by shaping it like a token — otherwise the app would act as a decryption
    oracle on read. ``skip_encrypted=True`` restores the idempotent skip for the one
    caller that re-reads raw at-rest values (the backfill migration), where a
    token-shaped value is genuinely already encrypted.

    JSON keys and non-sensitive values stay plaintext, so ``has_key`` / value lookups
    keep working. The input dict is never mutated in place: the model attribute must
    stay plaintext, or a FieldTracker comparing attributes would see a spurious
    change on every save.
    """
    if not isinstance(data, dict):
        return data
    result = {}
    for key, value in data.items():
        if (
            is_sensitive(key)
            and isinstance(value, str)
            and value
            and not (skip_encrypted and is_encrypted(value))
        ):
            result[key] = encrypt_value(value)
        else:
            result[key] = value
    return result


def decrypt_dict_values(data, is_sensitive):
    """Return a copy of ``data`` with encrypted values under sensitive keys decrypted.

    Only values whose key satisfies ``is_sensitive`` are decrypted. Deciding by key
    rather than by token shape closes the decryption oracle: a Fernet token planted
    under a non-sensitive key is returned untouched, never decrypted. A value that no
    configured key can decrypt is left as the ciphertext rather than raising, so one
    unreadable secret does not break loading the whole row.
    """
    if not isinstance(data, dict):
        return data
    result = {}
    for key, value in data.items():
        if is_sensitive(key) and is_encrypted(value):
            try:
                result[key] = decrypt_value(value)
            except InvalidToken:
                result[key] = value
        else:
            result[key] = value
    return result


def encrypt_defaults_for_update(model, defaults: dict) -> dict:
    """Encrypt the encrypted-field entries of a ``QuerySet.update()`` kwargs dict.

    ``update()`` compiles straight to SQL and never calls ``Field.pre_save``, which is
    where at-rest encryption happens — so a bulk update writes plaintext into a column
    that everything else keeps encrypted, and nothing reports it. Callers that update
    in bulk on purpose (rather than saving through an instance) run their kwargs
    through this first.

    Dispatch is on the field, not on a hard-coded name list: a field class that knows
    how to encrypt exposes ``encrypt_for_update``, so a column that becomes encrypted
    later is covered here the moment its field changes, with no second place to update.
    Returns a new dict; the caller's is left alone.
    """
    result = dict(defaults)
    for name, value in defaults.items():
        encrypt = getattr(model._meta.get_field(name), "encrypt_for_update", None)
        if encrypt is not None:
            result[name] = encrypt(value)
    return result
