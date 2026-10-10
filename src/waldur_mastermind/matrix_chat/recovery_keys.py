"""Matrix secret-storage recovery keys, as the spec defines them.

A recovery key is the base58 encoding of ``0x8B 0x01``, the 32-byte secret-storage
key and a parity byte, shown in groups of four characters. A secret-storage key
description (``m.secret_storage.key.<id>`` account data) carries an ``iv`` and
``mac`` computed by encrypting 32 zero bytes under the key, so whether a recovery
key opens a user's secret storage can be checked without touching any secret.

Each secret in storage (``m.cross_signing.master``, say) is encrypted the same
way under keys derived for its own name, so a recovery key can also be checked
against the secret it is meant to unlock.
"""

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

AES_HMAC_SHA2 = "m.secret_storage.v1.aes-hmac-sha2"

_PREFIX = b"\x8b\x01"
# A recovery key is 48 base58 characters, shown in groups of four. Anything much
# longer is refused before decoding, which is quadratic in the input length.
MAX_RECOVERY_KEY_LENGTH = 64
_KEY_LENGTH = 32
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class InvalidRecoveryKey(ValueError):
    pass


def _base58_decode(text):
    number = 0
    for char in text:
        index = _BASE58.find(char)
        if index < 0:
            raise InvalidRecoveryKey("The recovery key contains invalid characters.")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(text) - len(text.lstrip(_BASE58[0]))
    return b"\x00" * leading_zeros + body


def decode_recovery_key(recovery_key):
    """The 32-byte secret-storage key a recovery key encodes."""
    if not isinstance(recovery_key, str) or len(recovery_key) > MAX_RECOVERY_KEY_LENGTH:
        raise InvalidRecoveryKey("This is not a Matrix recovery key.")
    data = _base58_decode("".join(recovery_key.split()))
    if len(data) != len(_PREFIX) + _KEY_LENGTH + 1 or not data.startswith(_PREFIX):
        raise InvalidRecoveryKey("This is not a Matrix recovery key.")
    parity = 0
    for byte in data:
        parity ^= byte
    if parity:
        raise InvalidRecoveryKey("The recovery key has a typo: its check digit fails.")
    return data[len(_PREFIX) : -1]


def _unbase64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4))


def recovery_key_opens(recovery_key, key_info):
    """Whether the recovery key opens the secret-storage key ``key_info`` describes.

    False for a key that doesn't decode, a description of another algorithm, or one
    without a usable check (``iv``/``mac``), since then nothing can be confirmed.
    The description is whatever the user's account data holds, so a malformed one
    must answer False rather than raise: an exception here would fail the request
    and carry the key material in its frame to error reporting.
    """
    if not isinstance(key_info, dict) or key_info.get("algorithm") != AES_HMAC_SHA2:
        return False
    iv, mac = key_info.get("iv"), key_info.get("mac")
    if not isinstance(iv, str) or not isinstance(mac, str) or not iv or not mac:
        return False
    try:
        return _check(recovery_key, iv, mac)
    except Exception:  # noqa: BLE001 - any malformed input means "not confirmed"
        return False


def _derive(recovery_key, name):
    """The AES and HMAC keys for one secret, or for the key check (name "")."""
    derived = HKDF(
        algorithm=hashes.SHA256(), length=64, salt=b"\x00" * 32, info=name.encode()
    ).derive(decode_recovery_key(recovery_key))
    return derived[:32], derived[32:]


def _check(recovery_key, iv, mac):
    iv_bytes, expected = _unbase64(iv), _unbase64(mac)
    if len(iv_bytes) != 16:
        return False
    aes_key, mac_key = _derive(recovery_key, "")
    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv_bytes)).encryptor()
    ciphertext = encryptor.update(b"\x00" * 32) + encryptor.finalize()
    actual = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    return hmac.compare_digest(actual, expected)


def _decrypt_secret(recovery_key, name, encrypted):
    iv, ciphertext, mac = (encrypted.get(k) for k in ("iv", "ciphertext", "mac"))
    if not all(isinstance(v, str) and v for v in (iv, ciphertext, mac)):
        return None
    iv_bytes, ciphertext_bytes = _unbase64(iv), _unbase64(ciphertext)
    if len(iv_bytes) != 16:
        return None
    aes_key, mac_key = _derive(recovery_key, name)
    actual = hmac.new(mac_key, ciphertext_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, _unbase64(mac)):
        return None
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv_bytes)).decryptor()
    return decryptor.update(ciphertext_bytes) + decryptor.finalize()


def unlocks_master_key(recovery_key, encrypted, master_key):
    """Whether the recovery key decrypts the stored private master key, and that
    key is the one the homeserver publishes for the user.

    ``encrypted`` is the ``m.cross_signing.master`` item for the default key, and
    ``master_key`` the user's published master key (``/keys/query``). Both come
    from the homeserver, so anything malformed answers False, as above; the
    private key is never kept or returned.
    """
    if not isinstance(encrypted, dict) or not isinstance(master_key, dict):
        return False
    keys = master_key.get("keys")
    if not isinstance(keys, dict):
        return False
    published = {
        value
        for name, value in keys.items()
        if isinstance(name, str) and name.startswith("ed25519:")
    }
    try:
        seed = _decrypt_secret(recovery_key, "m.cross_signing.master", encrypted)
        if seed is None:
            return False
        private = Ed25519PrivateKey.from_private_bytes(_unbase64(seed.decode()))
        raw = private.public_key().public_bytes_raw()
    except Exception:  # noqa: BLE001 - any malformed input means "not confirmed"
        return False
    return base64.b64encode(raw).decode().rstrip("=") in published
