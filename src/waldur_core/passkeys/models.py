import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from waldur_core.core.models import NameMixin, User, UuidMixin
from waldur_core.passkeys.enums import AuthenticatorAttachment, CeremonyKind

# How long a ceremony row stays usable. The WebAuthn options we hand the
# browser carry a 60s timeout; this is deliberately more generous so that a
# user who is slow to reach for a security key gets a clean "expired" error
# rather than a verification failure.
CEREMONY_LIFETIME = timezone.timedelta(minutes=5)

# A ceremony is single-use, but a browser may legitimately retry once or twice
# against the same challenge (wrong key inserted first, user cancelled the
# platform prompt). Past this the row is burned, which bounds how many
# assertions an attacker may grind against one challenge.
CEREMONY_MAX_ATTEMPTS = 5

CHALLENGE_BYTES = 32


class PasskeyCredential(UuidMixin, NameMixin, models.Model):
    """A single WebAuthn credential registered by a user.

    Modelled on ``PersonalAccessToken``: named by the user, soft-revoked rather
    than deleted so an audit trail survives, and carrying last-used data.

    Nothing here is a secret. ``public_key`` is a public key, and
    ``credential_id`` is an identifier the authenticator hands out freely.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="passkey_credentials",
    )
    # Base64url, as the browser exchanges it. Unique across users: a single
    # authenticator credential must not be claimable by two accounts.
    credential_id = models.TextField(unique=True, db_index=True)
    # COSE-encoded public key, base64url.
    public_key = models.TextField()
    # Signature counter, monotonic per credential where the authenticator
    # implements one. Many modern authenticators pin it to zero.
    sign_count = models.PositiveBigIntegerField(default=0)
    # Authenticator model identifier. Kept so that FIDO-MDS allowlisting stays
    # an additive change rather than a re-registration.
    aaguid = models.CharField(max_length=36, blank=True)
    transports = models.JSONField(default=list, blank=True)
    attachment = models.CharField(
        max_length=20,
        choices=AuthenticatorAttachment.choices,
        default=AuthenticatorAttachment.UNKNOWN,
        blank=True,
    )
    # The RP ID this credential was registered under. Stored per credential so
    # that a deployment which changes its RP ID can be told exactly how many
    # credentials it just orphaned, instead of discovering it one failed login
    # at a time.
    rp_id = models.CharField(max_length=253)
    # Whether the authenticator reported the credential as backed up to, or
    # eligible for, a passkey provider's cloud. Surfaced in the UI so a user
    # can tell a synced passkey from one bound to a single device.
    is_backup_eligible = models.BooleanField(default=False)
    is_backed_up = models.BooleanField(default=False)
    # Whether the credential is client-side discoverable, i.e. usable for
    # usernameless sign-in. Only discoverable credentials can satisfy the
    # passwordless flow.
    is_discoverable = models.BooleanField(default=False)
    # Whether user verification (PIN, biometric) was performed at registration.
    # A credential registered without it cannot carry the passwordless flow's
    # user-verification requirement.
    is_user_verified = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    # Soft revoke. Revocation is a security event, so who did it and why are
    # part of the record; staff revoking another user's credential must supply
    # a reason.
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_passkey_credentials",
    )
    revocation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created", "id"]
        verbose_name = _("passkey credential")
        verbose_name_plural = _("passkey credentials")

    def __str__(self):
        return f"{self.name} ({self.user.username})"

    @property
    def is_orphaned(self):
        """True when this credential can no longer be used to authenticate.

        A credential is bound to the RP ID it was registered under. If the
        deployment's RP ID changes, the browser will not offer it any more —
        it is dead weight, not a working second factor.
        """
        return self.rp_id != settings.WALDUR_CORE.get("PASSKEY_RP_ID")

    def revoke(self, revoked_by=None, reason=""):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.revoked_by = revoked_by
        self.revocation_reason = reason
        self.save(
            update_fields=[
                "is_active",
                "revoked_at",
                "revoked_by",
                "revocation_reason",
            ]
        )

    def register_use(self, sign_count, ip_address=None):
        self.sign_count = sign_count
        self.last_used_at = timezone.now()
        self.last_used_ip = ip_address
        self.use_count = models.F("use_count") + 1
        self.save(
            update_fields=[
                "sign_count",
                "last_used_at",
                "last_used_ip",
                "use_count",
            ]
        )


class PasskeyCeremony(UuidMixin, models.Model):
    """Short-lived, single-use, attempt-capped state for one WebAuthn ceremony.

    Deliberately holds **no token and no token key**, unlike
    ``TokenExchangeCode`` which is redeemable. The worst an attacker can do
    with a stolen ceremony handle is present assertions against a challenge
    they cannot satisfy; it is not itself a credential.

    This lives in the database rather than the Django cache on purpose. The
    cache is a ``DatabaseCache`` shared with the login-lockout counters and the
    DRF throttle history, and it culls on overflow — putting
    unauthenticated-writable rows in there would hand an attacker an eviction
    primitive against those limits.
    """

    kind = models.CharField(max_length=20, choices=CeremonyKind.choices)
    # Base64url. Random per ceremony, never reused.
    challenge = models.TextField()
    rp_id = models.CharField(max_length=253)
    # Null for usernameless sign-in, where the whole point is that the server
    # does not know who is authenticating until the assertion comes back.
    # Populated for registration and for the second factor, where a correct
    # password has already identified the user.
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="passkey_ceremonies",
    )

    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created", "id"]
        verbose_name = _("passkey ceremony")
        verbose_name_plural = _("passkey ceremonies")
        indexes = [
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.kind} ceremony {self.uuid}"

    @classmethod
    def start(cls, kind, rp_id, user=None):
        return cls.objects.create(
            kind=kind,
            challenge=generate_challenge(),
            rp_id=rp_id,
            user=user,
            expires_at=timezone.now() + CEREMONY_LIFETIME,
        )

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self):
        return self.consumed_at is not None

    @property
    def is_exhausted(self):
        return self.attempts >= CEREMONY_MAX_ATTEMPTS

    @property
    def is_usable(self):
        return not (self.is_expired or self.is_consumed or self.is_exhausted)

    def consume(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])


def generate_challenge():
    """Return a fresh base64url challenge."""
    from waldur_core.passkeys.utils import bytes_to_base64url

    return bytes_to_base64url(secrets.token_bytes(CHALLENGE_BYTES))
