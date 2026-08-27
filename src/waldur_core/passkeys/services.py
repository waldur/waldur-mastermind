"""Ceremony service layer.

Wraps the ``webauthn`` library so that views never touch it directly, and so
that the security-relevant asymmetries between the two sign-in flows live in
one auditable place.

No view wiring happens here — the login paths land in a later change.
"""

import json
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from waldur_core.passkeys import policy
from waldur_core.passkeys.enums import CeremonyKind
from waldur_core.passkeys.models import PasskeyCeremony, PasskeyCredential
from waldur_core.passkeys.utils import base64url_to_bytes, bytes_to_base64url

logger = logging.getLogger(__name__)


class PasskeyError(Exception):
    """Any ceremony failure that should surface to the client as a 4xx."""


class CeremonyUnusable(PasskeyError):
    """The ceremony expired, was already consumed, or ran out of attempts."""


def _options_to_dict(options):
    """Render library options as the JSON dict the browser API expects."""
    return json.loads(options_to_json(options))


def _descriptors(credentials):
    return [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
        for c in credentials
    ]


def start_registration(user):
    """Begin enrolling a new credential for an authenticated user.

    Resident (discoverable) keys are *preferred* rather than required: an
    authenticator that cannot store one still yields a working second factor,
    it just cannot carry passwordless sign-in. Which of the two a credential
    can do is recorded on the row at finish time.
    """
    rp_id = policy.get_rp_id()
    existing = PasskeyCredential.objects.filter(user=user, is_active=True)

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=policy.get_rp_name(),
        user_id=str(user.uuid).encode(),
        user_name=user.username,
        user_display_name=user.full_name or user.username,
        # Registering the same authenticator twice yields a second credential
        # that silently shadows the first; excluding what the user already has
        # makes the browser refuse instead.
        exclude_credentials=_descriptors(existing),
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable credentials are REQUIRED, not preferred.
            #
            # This is what makes the credential a passkey rather than a
            # second-factor token, and it is also what makes the platform
            # authenticator appear at all: iCloud Keychain and Chrome's Touch
            # ID integration store only discoverable credentials, so a browser
            # reads resident_key=PREFERRED as a legacy second-factor
            # registration and offers security keys and phone/QR while hiding
            # the built-in authenticator entirely.
            #
            # The cost is old security keys with few resident slots, which can
            # refuse the registration. That is the right trade for a feature
            # whose whole point is passwordless sign-in.
            resident_key=ResidentKeyRequirement.REQUIRED,
            # Touch ID, Windows Hello and a security-key PIN are all user
            # verification. Requiring it here both matches what a passkey is
            # and keeps the platform authenticator the natural match for the
            # request.
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        # Order matters: it is what the browser's chooser offers first. Without
        # a hint, Chrome and Safari lead with the phone/QR "hybrid" flow, and
        # the built-in authenticator most people actually have — Touch ID,
        # Windows Hello — is buried behind "Other options". Naming the local
        # device first makes the common case the default one.
        hints=[
            PublicKeyCredentialHint.CLIENT_DEVICE,
            PublicKeyCredentialHint.SECURITY_KEY,
            PublicKeyCredentialHint.HYBRID,
        ],
    )

    ceremony = PasskeyCeremony.start(
        kind=CeremonyKind.REGISTRATION, rp_id=rp_id, user=user
    )
    # The library generated its own challenge; keep the row authoritative so
    # that verification compares against exactly one value.
    options.challenge = base64url_to_bytes(ceremony.challenge)
    return ceremony, _options_to_dict(options)


def finish_registration(ceremony, credential, name, ip_address=None):
    """Verify a registration response and persist the credential."""
    _consume_attempt(ceremony)

    if ceremony.kind != CeremonyKind.REGISTRATION:
        raise PasskeyError("Ceremony is not a registration ceremony.")
    if ceremony.user_id is None:
        raise PasskeyError("Registration ceremony has no user.")

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(ceremony.challenge),
            expected_rp_id=ceremony.rp_id,
            expected_origin=policy.get_allowed_origins(),
        )
    except Exception as e:
        logger.info("Passkey registration verification failed: %s", e)
        raise PasskeyError("Passkey registration could not be verified.")

    ceremony.consume()

    credential_id = bytes_to_base64url(verified.credential_id)

    extras = credential if isinstance(credential, dict) else {}
    transports = extras.get("transports") or []
    attachment = extras.get("authenticatorAttachment") or ""

    # Whether the credential is actually discoverable is reported by the
    # browser through the credProps extension, not by the attestation. Resident
    # keys are requested as PREFERRED, so an authenticator is free to hand back
    # a non-discoverable credential — recording every credential as
    # discoverable would make passwordless sign-in offer keys that cannot
    # satisfy it, and fail at the authenticator with no useful error.
    # Absent credProps, assume not discoverable: under-claiming costs a
    # passwordless option, over-claiming produces a broken one.
    cred_props = (extras.get("clientExtensionResults") or {}).get("credProps") or {}
    is_discoverable = bool(cred_props.get("rk", False))

    # credential_id is unique, and a check-then-create would not be atomic:
    # two ceremonies finishing concurrently with the same authenticator both
    # pass the pre-check and the loser surfaces an IntegrityError as a 500.
    # Let the constraint decide and translate it instead.
    #
    # The savepoint is load-bearing: catching IntegrityError without one
    # leaves the surrounding transaction unusable, so the 400 this raises
    # would itself fail on the way out.
    try:
        with transaction.atomic():
            return _create_credential(
                ceremony=ceremony,
                name=name,
                credential_id=credential_id,
                verified=verified,
                transports=transports,
                attachment=attachment,
                is_discoverable=is_discoverable,
                ip_address=ip_address,
            )
    except IntegrityError:
        raise PasskeyError("This passkey is already registered.")


def _create_credential(
    *,
    ceremony,
    name,
    credential_id,
    verified,
    transports,
    attachment,
    is_discoverable,
    ip_address,
):
    return PasskeyCredential.objects.create(
        user=ceremony.user,
        name=name,
        credential_id=credential_id,
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        aaguid=verified.aaguid or "",
        transports=transports,
        attachment=attachment,
        rp_id=ceremony.rp_id,
        # A multi-device credential is one a passkey provider may sync, i.e. it
        # is backup *eligible*; credential_backed_up says whether it currently
        # is. The two are distinct and the UI shows both.
        is_backup_eligible=verified.credential_device_type
        == CredentialDeviceType.MULTI_DEVICE,
        is_backed_up=verified.credential_backed_up,
        is_discoverable=is_discoverable,
        is_user_verified=verified.user_verified,
        last_used_ip=ip_address,
    )


def start_signin():
    """Begin a usernameless, discoverable-credential sign-in.

    ``allow_credentials`` is deliberately left empty. Populating it would
    require knowing who is signing in before they have proved anything, which
    turns the endpoint into a username enumeration oracle.

    User verification is ``REQUIRED``: this flow replaces both the password and
    the second factor, so the authenticator must prove the user is present
    *and* verified. Downgrading it to PREFERRED silently reduces passwordless
    sign-in to single-factor possession of a device.
    """
    rp_id = policy.get_rp_id()
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    ceremony = PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=rp_id)
    options.challenge = base64url_to_bytes(ceremony.challenge)
    return ceremony, _options_to_dict(options)


def create_mfa_ceremony(user):
    """Open a pending-login ceremony for a user who just passed a password check.

    Created by the login view *before* any token exists, so the handle it
    returns is not redeemable for anything — it only names a challenge the
    caller has yet to satisfy.
    """
    return PasskeyCeremony.start(
        kind=CeremonyKind.MFA, rp_id=policy.get_rp_id(), user=user
    )


def build_mfa_options(ceremony):
    """Render assertion options for an existing pending-login ceremony.

    Unlike the passwordless flow this *does* populate ``allow_credentials``:
    reaching this point already required a correct password, so naming the
    user's credentials leaks nothing new, and it lets the browser prompt for
    the right authenticator.

    User verification is ``PREFERRED`` rather than REQUIRED — the password is
    the knowledge factor here, so demanding a PIN on top would exclude
    authenticators that cannot do it without adding a factor.
    """
    credentials = PasskeyCredential.objects.filter(user=ceremony.user, is_active=True)
    options = generate_authentication_options(
        rp_id=ceremony.rp_id,
        allow_credentials=_descriptors(credentials),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    options.challenge = base64url_to_bytes(ceremony.challenge)
    return _options_to_dict(options)


def user_requires_mfa(user) -> bool:
    """Whether this user must satisfy a passkey after their password.

    A user with no credential is not locked out here — enforcement is a later
    concern. This only says "there is a second factor to satisfy".
    """
    if not policy.is_mfa_enabled():
        return False
    return PasskeyCredential.objects.filter(user=user, is_active=True).exists()


def finish_assertion(ceremony, credential, ip_address=None):
    """Verify an assertion and return the credential that satisfied it.

    Returns the ``PasskeyCredential``; the caller decides what authenticating
    means. Nothing here issues a token.
    """
    _consume_attempt(ceremony)

    if ceremony.kind not in (CeremonyKind.SIGNIN, CeremonyKind.MFA):
        raise PasskeyError("Ceremony is not an authentication ceremony.")

    raw_id = credential.get("id") if isinstance(credential, dict) else None
    if not raw_id:
        raise PasskeyError("Assertion is missing a credential id.")

    stored = PasskeyCredential.objects.filter(
        credential_id=raw_id, is_active=True
    ).first()
    if stored is None:
        raise PasskeyError("Unknown passkey.")

    # For the second factor the ceremony already names the user; an assertion
    # from somebody else's credential must not satisfy it.
    if ceremony.user_id is not None and stored.user_id != ceremony.user_id:
        raise PasskeyError("Passkey does not belong to this user.")

    require_uv = ceremony.kind == CeremonyKind.SIGNIN

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(ceremony.challenge),
            expected_rp_id=ceremony.rp_id,
            expected_origin=policy.get_allowed_origins(),
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=require_uv,
        )
    except Exception as e:
        logger.info("Passkey assertion verification failed: %s", e)
        raise PasskeyError("Passkey could not be verified.")

    ceremony.consume()
    stored.register_use(verified.new_sign_count, ip_address=ip_address)
    return stored


def _consume_attempt(ceremony):
    """Count an attempt against the cap before doing any verification work.

    Incrementing first means a client that keeps throwing malformed responses
    still burns the ceremony, rather than getting unlimited tries at a
    challenge because every one of them errored out early.
    """
    if not ceremony.is_usable:
        raise CeremonyUnusable("This passkey ceremony is no longer valid.")
    ceremony.attempts += 1
    ceremony.save(update_fields=["attempts"])


def purge_expired_ceremonies():
    """Delete ceremonies that can no longer be used.

    Rows are unauthenticated-writable, so they need a reaper; without one the
    table grows with every abandoned login attempt.
    """
    return PasskeyCeremony.objects.filter(expires_at__lt=timezone.now()).delete()
