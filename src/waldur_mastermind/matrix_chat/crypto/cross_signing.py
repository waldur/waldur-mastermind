"""Matrix cross-signing for the bot, which matrix-nio 0.26 does not implement.

The bot publishes a master key and a self-signing key, and signs its own device
with the latter, so other users' clients see a device its owner vouches for.
It has no use for a user-signing key: it never vouches for anyone else.

For other users, :func:`cross_signed_devices` checks the chain the spec defines:
the user's master key signs their self-signing key, which signs each device.
The master key itself is taken from the homeserver as published.

Objects are signed over their canonical JSON, without ``signatures`` and
``unsigned``, with Ed25519; keys and signatures are unpadded base64.
"""

import base64
import binascii
import json
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

MASTER = "master"
SELF_SIGNING = "self_signing"


def encode(data):
    return base64.b64encode(data).decode().rstrip("=")


def decode(text):
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def canonical_json(obj):
    """The bytes a signature covers: everything but ``signatures`` and ``unsigned``."""
    signed = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    return json.dumps(
        signed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


class SigningKey:
    """An Ed25519 key the bot signs with, kept as its 32-byte seed."""

    def __init__(self, seed):
        self._private = Ed25519PrivateKey.from_private_bytes(seed)

    @classmethod
    def generate(cls):
        return cls(os.urandom(32))

    @property
    def seed(self):
        return self._private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    @property
    def public_key(self):
        return encode(
            self._private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    @property
    def key_id(self):
        return f"ed25519:{self.public_key}"

    def sign(self, obj, user_id):
        """``obj`` with this key's signature added under ``user_id``."""
        signature = encode(self._private.sign(canonical_json(obj)))
        signatures = {u: dict(s) for u, s in (obj.get("signatures") or {}).items()}
        signatures.setdefault(user_id, {})[self.key_id] = signature
        return {**obj, "signatures": signatures}


def cross_signing_key(user_id, usage, key, signed_by=None):
    """The published form of a cross-signing key, signed by ``signed_by`` if given."""
    published = {
        "user_id": user_id,
        "usage": [usage],
        "keys": {key.key_id: key.public_key},
    }
    return signed_by.sign(published, user_id) if signed_by else published


def has_signature(obj, user_id, public_key, key_id=None):
    """Whether ``obj`` carries a valid signature by ``public_key`` under ``user_id``.

    The signature is looked up under ``key_id``: by default the key itself names
    it, as for cross-signing keys; a device signs under its device id instead.
    """
    if not isinstance(obj, dict) or not isinstance(public_key, str):
        return False
    signatures = obj.get("signatures")
    by_user = signatures.get(user_id) if isinstance(signatures, dict) else None
    key_id = key_id or f"ed25519:{public_key}"
    signature = by_user.get(key_id) if isinstance(by_user, dict) else None
    if not isinstance(signature, str):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(decode(public_key)).verify(
            decode(signature), canonical_json(obj)
        )
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


def published_key(obj, user_id, usage):
    """The Ed25519 key of a published cross-signing key, or None if it is malformed.

    It must be the user's, be for ``usage``, and hold exactly one Ed25519 key
    whose id names it.
    """
    if not isinstance(obj, dict) or obj.get("user_id") != user_id:
        return None
    usages = obj.get("usage")
    if not isinstance(usages, list) or usage not in usages:
        return None
    keys = obj.get("keys")
    if not isinstance(keys, dict) or len(keys) != 1:
        return None
    ((key_id, key),) = keys.items()
    if key_id != f"ed25519:{key}":
        return None
    return key


def self_signing_key(user_id, response):
    """The user's self-signing key, if their master key signs it; else None.

    ``response`` is a ``/keys/query`` response body.
    """
    master = published_key(
        (response.get("master_keys") or {}).get(user_id), user_id, MASTER
    )
    self_signing = (response.get("self_signing_keys") or {}).get(user_id)
    key = published_key(self_signing, user_id, SELF_SIGNING)
    if (
        master is None
        or key is None
        or not has_signature(self_signing, user_id, master)
    ):
        return None
    return key


def cross_signed_devices(user_id, response):
    """``{device id: ed25519 key}`` of the user's devices their identity vouches for.

    A device counts if its keys are signed by the user's self-signing key, which
    the user's master key signs. ``response`` is a ``/keys/query`` response body.
    """
    key = self_signing_key(user_id, response)
    if key is None:
        return {}
    devices = (response.get("device_keys") or {}).get(user_id)
    if not isinstance(devices, dict):
        return {}
    signed = {}
    for device_id, device in devices.items():
        if not isinstance(device, dict):
            continue
        if device.get("user_id") != user_id or device.get("device_id") != device_id:
            continue
        ed25519 = (device.get("keys") or {}).get(f"ed25519:{device_id}")
        if isinstance(ed25519, str) and has_signature(device, user_id, key):
            signed[device_id] = ed25519
    return signed
