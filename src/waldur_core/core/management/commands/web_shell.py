from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from waldur_core.web_shell import assets, server, tickets

DEFAULT_PORT = 8090


class Command(BaseCommand):
    help = (
        "Serve `waldur shell` to staff in the browser. Development only: needs "
        "DEBUG, WALDUR_CORE['WEB_SHELL_ENABLED'] and WALDUR_CORE['WEB_SHELL_URL']."
    )

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument(
            "--port",
            type=int,
            help="Port to listen on. Defaults to the port of WEB_SHELL_URL.",
        )
        parser.add_argument(
            "--fetch-assets",
            action="store_true",
            help=f"Download ghostty-web {assets.GHOSTTY_WEB_VERSION} and exit.",
        )
        parser.add_argument(
            "--mint",
            metavar="USERNAME",
            help="Print a single-use link for a staff user and exit.",
        )

    def handle(self, *args, **options):
        if options["fetch_assets"]:
            path = assets.fetch()
            self.stdout.write(
                f"ghostty-web {assets.GHOSTTY_WEB_VERSION} installed in {path}"
            )
            return

        if not tickets.is_enabled():
            raise CommandError(
                "The web shell needs DEBUG, WALDUR_CORE['WEB_SHELL_ENABLED'] "
                "and WALDUR_CORE['WEB_SHELL_URL']."
            )

        if options["mint"]:
            self.stdout.write(server.mint_link(options["mint"]))
            return

        if not assets.is_installed():
            raise CommandError(
                f"ghostty-web is missing from {assets.assets_dir()}. "
                "Run `waldur web_shell --fetch-assets` first."
            )

        port = options["port"] or (
            urlsplit(settings.WALDUR_CORE["WEB_SHELL_URL"]).port or DEFAULT_PORT
        )
        server.serve(options["host"], port)
