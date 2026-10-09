import os
from urllib.parse import urlsplit, urlunsplit

from constance import settings as constance_settings
from constance.utils import get_values_for_keys
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from waldur_core.core.serializers import ConstanceSettingsSerializer
from waldur_mastermind.matrix_chat.serializers import (
    validate_homeserver_domain,
    validate_sender_localpart,
)

# Without these the appservice cannot authenticate against the homeserver, and
# a deployment that seeds the rest would come up looking configured while every
# bot call returns M_UNKNOWN_TOKEN, or every user provisioning is refused.
REQUIRED = (
    "MATRIX_HOMESERVER_URL",
    "MATRIX_HOMESERVER_DOMAIN",
    "MATRIX_APPSERVICE_AS_TOKEN",
    "MATRIX_APPSERVICE_HS_TOKEN",
    "MATRIX_USER_REGISTRATION_SECRET",
)

# Both values are interpolated into the appservice registration's namespace
# regexes, which the Constance serializer only knows as plain strings.
MATRIX_VALIDATORS = {
    "MATRIX_APPSERVICE_SENDER_LOCALPART": validate_sender_localpart,
    "MATRIX_HOMESERVER_DOMAIN": validate_homeserver_domain,
}

APPSERVICE_TOKENS = ("MATRIX_APPSERVICE_AS_TOKEN", "MATRIX_APPSERVICE_HS_TOKEN")

SENSITIVE_KEY_SUFFIXES = ("TOKEN", "SECRET", "PASSWORD", "KEY")

# What the encrypted Constance backend returns for a secret that no configured
# FIELD_ENCRYPTION_KEY decrypts: a random value with this prefix, non-empty so
# no check takes it for "unset". Must match UNDECRYPTABLE_PREFIX there.
UNDECRYPTABLE_PREFIX = "undecryptable:"


# The Constance settings a deployment may seed, by name. An explicit list rather
# than every MATRIX_* setting: the packagers' Jobs also carry credentials of
# their own in MATRIX_* variables (DEPLOYMENT_ONLY below), and a later Constance
# setting that happened to share such a name would have a homeserver admin
# credential written into the database. MATRIX_TOKENS_MANAGED_BY is not here
# because the command sets it itself.
SEEDABLE = (
    "MATRIX_ENABLED",
    "MATRIX_AUTO_CREATE_PROJECT_ROOMS",
    "MATRIX_HOMESERVER_URL",
    "MATRIX_HOMESERVER_PUBLIC_URL",
    "MATRIX_HOMESERVER_DOMAIN",
    "MATRIX_APPSERVICE_AS_TOKEN",
    "MATRIX_APPSERVICE_HS_TOKEN",
    "MATRIX_APPSERVICE_SENDER_LOCALPART",
    "MATRIX_HISTORY_EXPORT_ENABLED",
    "MATRIX_EXPORT_MEDIA",
    "MATRIX_HISTORY_EXPORT_RETENTION_DAYS",
    "MATRIX_USER_REGISTRATION_SECRET",
    "MATRIX_USER_ID_FORMAT",
    "MATRIX_EXTERNAL_LOGIN_METHOD",
    "MATRIX_LIVEKIT_KEY",
    "MATRIX_LIVEKIT_SECRET",
    "MATRIX_LIVEKIT_URL",
    "MATRIX_LIVEKIT_PUBLIC_URL",
)

# Environment variables the packagers set next to the seeded ones that are for
# the homeserver, never for Waldur. A test keeps them out of SEEDABLE.
DEPLOYMENT_ONLY = ("MATRIX_BOOTSTRAP_PASSWORD", "MATRIX_ADMIN_TOKEN")


def managed_keys():
    """Every Constance setting this command is willing to seed."""
    return sorted(k for k in SEEDABLE if k in constance_settings.CONFIG)


def is_sensitive(key):
    # The suffixes alone are not enough: a secret setting added to SEEDABLE
    # under a name that does not say so would be logged.
    options = constance_settings.CONFIG[key]
    is_secret_field = len(options) > 2 and options[2] == "secret_field"
    return is_secret_field or key.endswith(SENSITIVE_KEY_SUFFIXES)


def without_userinfo(value):
    """A URL with any user:password@ taken out, for printing."""
    if not isinstance(value, str) or "@" not in value:
        return value
    parts = urlsplit(value)
    if not parts.scheme or "@" not in parts.netloc:
        return value
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=host))


class Command(BaseCommand):
    help = (
        "Seed Matrix Constance settings from the environment. Each seedable "
        "setting is read from the environment variable of the same name; other "
        "MATRIX_* variables are ignored. Both waldur-helm "
        "and waldur-docker-compose set them. Supplied values are overwritten on "
        "every run, because the deployment owns them. MATRIX_ENABLED, when not "
        "supplied, is switched on only at the first seeding, when neither the "
        "tokens marker nor an appservice token is stored, so an administrator "
        "who turns chat off keeps it off. "
        "Appservice tokens already in Constance that the deployment did not "
        "seed are never replaced."
    )

    def refuse_to_replace_hand_configured_tokens(self, stored, validated_data):
        # Once the marker is set, the stored tokens came from the deployment
        # and changing them is its own rotation. Without it they came from the
        # Setup wizard, and the homeserver is registered with exactly those.
        if stored["MATRIX_TOKENS_MANAGED_BY"]:
            return
        # A token stored under a FIELD_ENCRYPTION_KEY this deployment no longer
        # has reads as a random stand-in, which would differ from any supplied
        # value. Nothing can be compared, and the hand-configured token may
        # still be the one the homeserver holds, so refuse and name the cause
        # rather than report a mismatch that may not exist.
        undecryptable = [
            key
            for key in APPSERVICE_TOKENS
            if isinstance(stored[key], str)
            and stored[key].startswith(UNDECRYPTABLE_PREFIX)
        ]
        if undecryptable:
            raise CommandError(
                "Constance holds appservice tokens that were not seeded by the "
                "deployment and cannot be decrypted with any configured "
                "FIELD_ENCRYPTION_KEY (%s), so they cannot be compared with the "
                "supplied ones. Nothing was written. Restore the key they were "
                "stored with, as FIELD_ENCRYPTION_KEY or in "
                "FIELD_ENCRYPTION_KEY_FALLBACKS, and deploy again. If that key "
                "is lost, clear MATRIX_APPSERVICE_AS_TOKEN and "
                "MATRIX_APPSERVICE_HS_TOKEN under Administration → "
                "Configuration → Matrix chat → Settings and deploy again, which "
                "registers the appservice with the deployment's tokens."
                % ", ".join(undecryptable)
            )
        replaced = [
            key
            for key in APPSERVICE_TOKENS
            if stored[key] and stored[key] != validated_data[key]
        ]
        if replaced:
            raise CommandError(
                "Constance holds appservice tokens that were not seeded by the "
                "deployment and differ from the supplied ones (%s), so the "
                "homeserver is registered with tokens the deployment does not "
                "know. Nothing was written. To hand the tokens to the "
                "deployment, clear MATRIX_APPSERVICE_AS_TOKEN and "
                "MATRIX_APPSERVICE_HS_TOKEN under Administration → "
                "Configuration → Matrix chat → Settings and deploy again, "
                "which registers the appservice with the deployment's tokens; "
                "otherwise start from a fresh stack." % ", ".join(replaced)
            )

    def validate(self, supplied):
        serializer = ConstanceSettingsSerializer(data=supplied)
        if serializer.is_valid():
            errors = {}
            for key, validator in MATRIX_VALIDATORS.items():
                if key not in serializer.validated_data:
                    continue
                try:
                    validator(serializer.validated_data[key])
                except ValidationError as exc:
                    errors[key] = exc.detail[0]
        else:
            errors = {name: errs[0] for name, errs in serializer.errors.items()}

        if errors:
            raise CommandError(
                "Rejected settings: %s"
                % "; ".join(f"{name}: {error}" for name, error in errors.items())
            )
        return serializer

    def handle(self, *args, **options):
        missing = [key for key in REQUIRED if not os.environ.get(key, "").strip()]
        if missing:
            raise CommandError(
                "Missing required environment variables: %s. The deployment "
                "must supply them; refusing to seed a half-configured Matrix "
                "integration." % ", ".join(missing)
            )

        # Read through the backend rather than `config`, whose attribute access
        # stores the default on a miss: a refusal must write nothing, and a
        # stored default would be indistinguishable from someone's choice.
        stored = get_values_for_keys(["MATRIX_TOKENS_MANAGED_BY", *APPSERVICE_TOKENS])

        supplied = {key: os.environ[key] for key in managed_keys() if key in os.environ}
        # The packagers render this Job only when chat is switched on in their
        # own values, so neither passes the flag. Defaulting it off here would
        # seed a complete configuration and leave chat invisible. Only the
        # first seeding gets the default, though, so an administrator who
        # switched chat off does not have it switched back on by the next
        # routine deploy. Whether MATRIX_ENABLED has a row cannot tell the first
        # seeding apart, because any read of it stores one. The marker alone
        # cannot either: an administrator who clears it to hand the tokens back
        # leaves it blank too, and would find chat switched on again. A fresh
        # install has neither the marker nor any appservice token stored.
        first_seeding = not stored["MATRIX_TOKENS_MANAGED_BY"] and not any(
            stored[key] for key in APPSERVICE_TOKENS
        )
        if "MATRIX_ENABLED" not in supplied and first_seeding:
            supplied["MATRIX_ENABLED"] = "true"
        # Forced rather than defaulted: the marker records that the deployment
        # seeds these settings, which is true by the fact that this command
        # ran. Were it settable from the environment, a deployment could
        # re-seed tokens on every sync while still presenting the Setup wizard
        # as authoritative, and every wizard rotation would be silently
        # reverted at the next deploy.
        supplied["MATRIX_TOKENS_MANAGED_BY"] = "deployment"

        serializer = self.validate(supplied)
        # Compared after validation, which trims the values, so a Secret
        # rendered with a trailing newline still matches what it holds.
        self.refuse_to_replace_hand_configured_tokens(stored, serializer.validated_data)
        serializer.save()

        for key in sorted(supplied):
            value = serializer.validated_data[key]
            if is_sensitive(key):
                value = "<redacted>"
            else:
                value = without_userinfo(value)
            self.stdout.write(self.style.SUCCESS(f"{key} has been set to {value}."))
