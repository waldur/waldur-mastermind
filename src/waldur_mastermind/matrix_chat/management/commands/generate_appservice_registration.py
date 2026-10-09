import yaml
from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.matrix_chat import appservice_registration
from waldur_mastermind.matrix_chat.appservice_registration import (
    AppserviceRegistrationError,
)


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
        try:
            registration = appservice_registration.build_registration_from_config(
                url=options["url"],
                as_token=options["as_token"],
                hs_token=options["hs_token"],
            )
        except AppserviceRegistrationError as exc:
            raise CommandError(str(exc))

        self.stdout.write(yaml.dump(registration, default_flow_style=False))
