import os

from constance import config
from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.matrix_chat import appservice_registration
from waldur_mastermind.matrix_chat.appservice_registration import (
    AppserviceRegistrationError,
)


def _env(name):
    """Read a secret from the environment, treating a blank value as unset.

    Compose passes `${VAR:-}` through, so an unset variable usually arrives as
    an empty string. A non-blank value is kept as is: surrounding spaces may be
    part of the password the homeserver already holds.
    """
    value = os.environ.get(name, "")
    return value if value.strip() else ""


class Command(BaseCommand):
    help = (
        "Register Waldur's appservice on a Conduit-family homeserver (Tuwunel) "
        "via its admin-room command, so no operator has to paste the "
        "registration YAML into a Matrix client. Synapse is not supported — it "
        "loads appservices from app_service_config_files at startup instead. "
        "It acts as the bootstrap admin, @waldur-bootstrap. The first run "
        "creates it through the homeserver's shared-secret registration, keyed "
        "with MATRIX_USER_REGISTRATION_SECRET, which must also be the "
        "homeserver's registration_shared_secret; later runs sign in as it. "
        "Set MATRIX_BOOTSTRAP_PASSWORD in the environment and keep it: it is "
        "the bootstrap admin's password, and without it no bootstrap admin is "
        "created. Set MATRIX_ADMIN_TOKEN to an admin's access token to act as "
        "that admin instead, which later runs need when password login is off "
        "on the homeserver. The bootstrap admin is signed out when the command "
        "ends; an admin token is not."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            type=str,
            required=True,
            help="Base URL of the Waldur instance, as reachable from the homeserver.",
        )
        parser.add_argument(
            "--homeserver-url",
            type=str,
            default="",
            help="Homeserver client-server API URL (default: MATRIX_HOMESERVER_URL).",
        )
        parser.add_argument(
            "--registration-token",
            type=str,
            default="",
            help=(
                "Homeserver registration token, which must also be its "
                "registration_shared_secret: the bootstrap admin is created "
                "through the shared-secret registration keyed with it (default: "
                "MATRIX_USER_REGISTRATION_SECRET from Constance). Whoever holds it "
                "can create homeserver admins. Prefer setting it there, from the "
                "environment through the deployment: an argument shows up in "
                "process listings."
            ),
        )
        parser.add_argument(
            "--admin-token",
            type=str,
            default="",
            help=(
                "Access token of an existing homeserver admin, used instead of "
                "registering a bootstrap user. Prefer the MATRIX_ADMIN_TOKEN "
                "environment variable: an argument shows up in process listings."
            ),
        )
        parser.add_argument(
            "--admin-room",
            type=str,
            default="",
            help="Admin room ID, if it cannot be discovered automatically.",
        )
        parser.add_argument(
            "--bootstrap-localpart",
            type=str,
            default=appservice_registration.BOOTSTRAP_LOCALPART,
            help="Localpart of the bootstrap admin user to create.",
        )
        parser.add_argument(
            "--as-token",
            type=str,
            default="",
            help=(
                "Override as_token (default: MATRIX_APPSERVICE_AS_TOKEN from "
                "Constance). Prefer setting it there, from the environment "
                "through the deployment: an argument shows up in process listings."
            ),
        )
        parser.add_argument(
            "--hs-token",
            type=str,
            default="",
            help=(
                "Override hs_token (default: MATRIX_APPSERVICE_HS_TOKEN from "
                "Constance). Prefer setting it there, from the environment "
                "through the deployment: an argument shows up in process listings."
            ),
        )

    def handle(self, *args, **options):
        homeserver_url = options["homeserver_url"] or config.MATRIX_HOMESERVER_URL
        if not homeserver_url:
            raise CommandError(
                "No homeserver URL: pass --homeserver-url or set MATRIX_HOMESERVER_URL."
            )
        registration_token = (
            options["registration_token"] or config.MATRIX_USER_REGISTRATION_SECRET
        )
        for name, setting in (
            ("registration_token", "MATRIX_USER_REGISTRATION_SECRET"),
            ("as_token", "MATRIX_APPSERVICE_AS_TOKEN"),
            ("hs_token", "MATRIX_APPSERVICE_HS_TOKEN"),
        ):
            if options[name]:
                self.stderr.write(
                    self.style.WARNING(
                        f"--{name.replace('_', '-')} shows up in process listings; "
                        f"prefer {setting} in Constance."
                    )
                )

        try:
            appservice_registration.refuse_undecryptable(
                {"MATRIX_USER_REGISTRATION_SECRET": registration_token}
            )
            registration = appservice_registration.build_registration_from_config(
                url=options["url"],
                as_token=options["as_token"],
                hs_token=options["hs_token"],
            )
            enrolment = appservice_registration.register_appservice_on_homeserver(
                homeserver_url=homeserver_url,
                homeserver_domain=config.MATRIX_HOMESERVER_DOMAIN,
                registration=registration,
                registration_token=registration_token,
                admin_token=options["admin_token"] or _env("MATRIX_ADMIN_TOKEN"),
                admin_room=options["admin_room"],
                bootstrap_localpart=options["bootstrap_localpart"],
                # Read from the environment only, so it never shows up in a
                # process listing the way an argument would.
                bootstrap_password=_env("MATRIX_BOOTSTRAP_PASSWORD"),
            )
        except AppserviceRegistrationError as exc:
            raise CommandError(str(exc))

        self._report(enrolment.status)
        if enrolment.unchecked:
            self.stdout.write(
                self.style.WARNING(
                    "The registration is live; could not compare it with the "
                    f"expected descriptor: {enrolment.unchecked}"
                )
            )
        if enrolment.ping_error:
            # A warning, not a failure: on a fresh install the API may still be
            # starting when this runs.
            self.stdout.write(
                self.style.WARNING(
                    f"The appservice is registered, but the homeserver could not reach Waldur at "
                    f"{options['url']}: {enrolment.ping_error}. Events are not "
                    "delivered until it can."
                )
            )
        bot_user_id = appservice_registration.bot_user_id_for(
            registration, config.MATRIX_HOMESERVER_DOMAIN
        )
        if enrolment.bot_made_admin:
            self.stdout.write(
                self.style.SUCCESS(f"Made {bot_user_id} a homeserver admin.")
            )
        if enrolment.bot_admin_error:
            # A warning, not a failure: chat works without it, only what goes
            # through the homeserver's admin API waits for it.
            self.stdout.write(
                self.style.WARNING(
                    f"{enrolment.bot_admin_error}. Make it one from the admin "
                    f"room: !admin users make-user-admin {bot_user_id}"
                )
            )

    def _report(self, status):
        if status == "replaced":
            self.stdout.write(
                self.style.WARNING(
                    f"Appservice '{appservice_registration.APPSERVICE_ID}' was "
                    "registered with other tokens, another URL or other "
                    "namespaces; replaced it with Waldur's."
                )
            )
        elif status == "already-registered":
            self.stdout.write(
                self.style.WARNING(
                    "Appservice was already registered on the homeserver; "
                    "the registration is unchanged."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Appservice '{appservice_registration.APPSERVICE_ID}' "
                    "registered on the homeserver."
                )
            )
