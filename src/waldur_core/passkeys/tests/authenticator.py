"""A minimal software WebAuthn authenticator, for tests.

Real WebAuthn needs a browser and a hardware or platform authenticator, so
without something like this the ceremony code can only be tested by mocking
out the very verification it exists to perform.

This produces genuine, verifiable ES256 assertions: the responses it returns
are checked by the same ``webauthn`` library calls the production code uses,
against real signatures. It is a stand-in for the authenticator, not for the
verifier.

Deliberately supports being *wrong* — bad signature, replayed counter, wrong
origin — so the negative paths are testable too.
"""

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from waldur_core.passkeys.utils import bytes_to_base64url

# Authenticator data flag bits, per the WebAuthn spec.
FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
FLAG_BACKUP_ELIGIBLE = 0x08
FLAG_BACKED_UP = 0x10
FLAG_ATTESTED_CREDENTIAL_DATA = 0x40

COSE_KTY = 1
COSE_ALG = 3
COSE_CRV = -1
COSE_X = -2
COSE_Y = -3

COSE_KTY_EC2 = 2
COSE_ALG_ES256 = -7
COSE_CRV_P256 = 1


class SoftwareAuthenticator:
    """One virtual authenticator holding one credential."""

    def __init__(
        self,
        aaguid=None,
        user_verified=True,
        backed_up=False,
        discoverable=True,
        attachment="platform",
    ):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.aaguid = aaguid or bytes(16)
        self.sign_count = 0
        self.user_verified = user_verified
        self.backed_up = backed_up
        self.discoverable = discoverable
        self.attachment = attachment

    # -- key encoding ---------------------------------------------------

    def _cose_public_key(self):
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps(
            {
                COSE_KTY: COSE_KTY_EC2,
                COSE_ALG: COSE_ALG_ES256,
                COSE_CRV: COSE_CRV_P256,
                COSE_X: numbers.x.to_bytes(32, "big"),
                COSE_Y: numbers.y.to_bytes(32, "big"),
            }
        )

    # -- authenticator data ---------------------------------------------

    def _flags(self, attested):
        flags = FLAG_USER_PRESENT
        if self.user_verified:
            flags |= FLAG_USER_VERIFIED
        if self.backed_up:
            flags |= FLAG_BACKUP_ELIGIBLE | FLAG_BACKED_UP
        if attested:
            flags |= FLAG_ATTESTED_CREDENTIAL_DATA
        return flags

    def _authenticator_data(self, rp_id, attested):
        data = hashlib.sha256(rp_id.encode()).digest()
        data += struct.pack("!B", self._flags(attested))
        data += struct.pack("!I", self.sign_count)
        if attested:
            public_key = self._cose_public_key()
            data += self.aaguid
            data += struct.pack("!H", len(self.credential_id))
            data += self.credential_id
            data += public_key
        return data

    @staticmethod
    def _client_data(kind, challenge, origin):
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()

    # -- ceremonies ------------------------------------------------------

    def register(self, challenge, rp_id, origin, transports=None, report_rk=True):
        """Return a registration response for the given challenge.

        ``challenge`` is the base64url string handed to the browser.

        ``report_rk=False`` omits the credProps extension entirely, which is
        what a browser that does not implement it does — the server must then
        treat the credential as non-discoverable rather than assuming.
        """
        client_data = self._client_data("webauthn.create", challenge, origin)
        auth_data = self._authenticator_data(rp_id, attested=True)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        extension_results = {}
        if report_rk:
            extension_results["credProps"] = {"rk": self.discoverable}
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": self.attachment,
            "transports": transports or ["internal"],
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
            },
            "clientExtensionResults": extension_results,
        }

    def authenticate(
        self,
        challenge,
        rp_id,
        origin,
        user_handle=None,
        bump_counter=True,
        corrupt_signature=False,
    ):
        """Return an assertion response for the given challenge."""
        if bump_counter:
            self.sign_count += 1

        client_data = self._client_data("webauthn.get", challenge, origin)
        auth_data = self._authenticator_data(rp_id, attested=False)

        signed = auth_data + hashlib.sha256(client_data).digest()
        signature = self.private_key.sign(signed, ec.ECDSA(hashes.SHA256()))
        if corrupt_signature:
            # Flip a bit inside the DER payload rather than replacing it, so
            # the failure is a bad signature and not a parse error.
            signature = bytearray(signature)
            signature[-1] ^= 0xFF
            signature = bytes(signature)

        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(user_handle) if user_handle else None,
            },
            "clientExtensionResults": {},
        }
