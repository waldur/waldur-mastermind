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
import hashlib
import logging

from cryptography.fernet import Fernet, MultiFernet
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


def _get_fernet() -> MultiFernet:
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
    return MultiFernet([Fernet(key) for key in keys])


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
