"""Attachments in an encrypted room, as the Matrix spec's "Sending encrypted
attachments" defines them (version 2).

A file is encrypted with AES-CTR under a random 256-bit key; the event carries
the key as a JWK, the IV, and the SHA-256 of the ciphertext in its ``file``
object. The homeserver only ever holds the ciphertext.

The ``file`` object is written by whoever sent the event, so every field is
checked before any of it is used, and the hash is checked before decrypting:
nothing is decrypted from a file other than the one the sender uploaded.
"""

import base64
import binascii
import dataclasses
import hashlib
import hmac
import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_BYTES = 32
IV_BYTES = 16
SHA256_BYTES = 32

# A media ID is [A-Za-z0-9_-]; a server name is a host name, IPv4 or bracketed
# IPv6 address, with an optional port. Strict, as the parts end up in a path.
MXC_URI = re.compile(
    r"^mxc://([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?/([A-Za-z0-9_\-]+)$"
)

_BASE64 = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
_BASE64_URL = re.compile(r"^[A-Za-z0-9_\-]*={0,2}$")


class AttachmentError(ValueError):
    """The attachment cannot be decrypted: malformed, altered, or not v2."""


@dataclasses.dataclass(frozen=True, repr=False)
class EncryptedFile:
    url: str
    attachment_key: bytes
    iv: bytes
    sha256: bytes

    def __repr__(self):
        # Never the key, wherever this object ends up.
        return f"EncryptedFile({self.url})"


def parse_mxc(url):
    """``(server_name, media_id)`` of an ``mxc://`` URI, or None."""
    if not isinstance(url, str):
        return None
    match = MXC_URI.match(url)
    if not match:
        return None
    return match.group(1) + (match.group(2) or ""), match.group(3)


def _decode(value, url_safe):
    """Unpadded (or padded) base64, strictly: no whitespace, no other alphabet."""
    if not isinstance(value, str):
        return None
    if not (_BASE64_URL if url_safe else _BASE64).match(value):
        return None
    unpadded = value.rstrip("=")
    if len(unpadded) % 4 == 1:
        return None
    padded = unpadded + "=" * (-len(unpadded) % 4)
    try:
        if url_safe:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def parse_encrypted_file(encrypted_file):
    """Check an event's ``file`` object; an :class:`EncryptedFile`, or None if
    anything is missing or malformed."""
    if not isinstance(encrypted_file, dict):
        return None
    jwk = encrypted_file.get("key")
    hashes = encrypted_file.get("hashes")
    if not isinstance(jwk, dict) or not isinstance(hashes, dict):
        return None
    if encrypted_file.get("v") != "v2":
        return None
    url = encrypted_file.get("url")
    if parse_mxc(url) is None:
        return None
    if jwk.get("kty") != "oct" or jwk.get("alg") != "A256CTR":
        return None
    key_ops = jwk.get("key_ops")
    if not isinstance(key_ops, list) or "decrypt" not in key_ops:
        return None
    attachment_key = _decode(jwk.get("k"), url_safe=True)
    iv = _decode(encrypted_file.get("iv"), url_safe=False)
    sha256 = _decode(hashes.get("sha256"), url_safe=False)
    if (
        attachment_key is None
        or len(attachment_key) != KEY_BYTES
        or iv is None
        or len(iv) != IV_BYTES
        or sha256 is None
        or len(sha256) != SHA256_BYTES
    ):
        return None
    return EncryptedFile(url=url, attachment_key=attachment_key, iv=iv, sha256=sha256)


def decrypt_attachment(ciphertext, encrypted_file):
    """The plaintext of ``ciphertext``; raises AttachmentError if its hash is
    not the one the sender recorded."""
    if not hmac.compare_digest(
        hashlib.sha256(ciphertext).digest(), encrypted_file.sha256
    ):
        raise AttachmentError("The attachment is not the file its sender uploaded.")
    # The counter is the IV's low 64 bits, and wraps without carrying into the
    # nonce, as browsers decrypt it; cryptography's CTR counts over all 128
    # bits. Senders start the counter at zero, but one that did not must not
    # get a different file in the export than members saw.
    nonce, counter = encrypted_file.iv[:8], int.from_bytes(encrypted_file.iv[8:])
    until_wrap = (2**64 - counter) * 16
    if len(ciphertext) <= until_wrap:
        return _ctr(encrypted_file.attachment_key, encrypted_file.iv, ciphertext)
    view = memoryview(ciphertext)
    return _ctr(
        encrypted_file.attachment_key, encrypted_file.iv, view[:until_wrap]
    ) + _ctr(encrypted_file.attachment_key, nonce + bytes(8), view[until_wrap:])


def _ctr(attachment_key, iv, data):
    # CTR is a stream mode: finalize() adds nothing, so its result is not
    # concatenated onto a copy of the whole file.
    decryptor = Cipher(algorithms.AES(attachment_key), modes.CTR(iv)).decryptor()
    plaintext = decryptor.update(data)
    decryptor.finalize()
    return plaintext
