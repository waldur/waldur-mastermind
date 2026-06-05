import yaml
from constance import config
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from waldur_mastermind.matrix_chat import serializers as matrix_serializers


class Command(BaseCommand):
    help = "Generate a Matrix Application Service registration YAML for the homeserver."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            type=str,
            default="https://waldur.example.com",
            help="Base URL of the Waldur instance.",
        )
        parser.add_argument(
            "--as-token",
            type=str,
            default="",
            help="Override as_token (default: read from constance).",
        )
        parser.add_argument(
            "--hs-token",
            type=str,
            default="",
            help="Override hs_token (default: read from constance).",
        )

    def handle(self, *args, **options):
        as_token = options["as_token"] or config.MATRIX_APPSERVICE_AS_TOKEN
        hs_token = options["hs_token"] or config.MATRIX_APPSERVICE_HS_TOKEN
        sender_localpart = config.MATRIX_APPSERVICE_SENDER_LOCALPART or "waldur-bot"
        homeserver_domain = config.MATRIX_HOMESERVER_DOMAIN
        url = options["url"].rstrip("/")

        missing = []
        if not as_token:
            missing.append(
                "as_token (pass --as-token or set MATRIX_APPSERVICE_AS_TOKEN)"
            )
        if not hs_token:
            missing.append(
                "hs_token (pass --hs-token or set MATRIX_APPSERVICE_HS_TOKEN)"
            )
        if not homeserver_domain:
            missing.append(
                "MATRIX_HOMESERVER_DOMAIN (required for users namespace regex)"
            )
        if missing:
            raise CommandError("Missing prerequisites: " + "; ".join(missing))

        # Validate the values that will be interpolated into the regex so a
        # crafted localpart can't claim the entire user namespace.
        try:
            matrix_serializers.validate_sender_localpart(sender_localpart)
            matrix_serializers.validate_homeserver_domain(homeserver_domain)
        except ValidationError as exc:
            raise CommandError(str(exc.detail))

        registration = {
            "id": "waldur",
            "url": url,
            "as_token": as_token,
            "hs_token": hs_token,
            "sender_localpart": sender_localpart,
            "namespaces": {
                "users": [
                    {
                        "exclusive": True,
                        "regex": f"@{sender_localpart}:{homeserver_domain}",
                    },
                    {
                        "exclusive": False,
                        "regex": f"@.*:{homeserver_domain}",
                    },
                ],
                "rooms": [],
                "aliases": [],
            },
        }

        self.stdout.write(yaml.dump(registration, default_flow_style=False))
