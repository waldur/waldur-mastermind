import os

from constance import config
from constance.codecs import dumps
from constance.models import Constance
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.management import BaseCommand


class Command(BaseCommand):
    help = "Set or remove language-specific login logos"

    def add_arguments(self, parser):
        parser.add_argument(
            "-l",
            "--language",
            type=str,
            required=True,
            help="ISO 639-1 language code (e.g., 'de', 'et', 'fr')",
        )
        parser.add_argument(
            "-f",
            "--file",
            type=str,
            help="Path to the logo image file",
        )
        parser.add_argument(
            "-r",
            "--remove",
            action="store_true",
            help="Remove the language-specific logo",
        )

    def handle(self, *args, **options):
        language = options["language"]
        file_path = options.get("file")
        remove = options.get("remove")

        if remove and file_path:
            self.stdout.write(
                self.style.ERROR("Cannot use --file and --remove together")
            )
            return

        if not remove and not file_path:
            self.stdout.write(self.style.ERROR("Either --file or --remove is required"))
            return

        # Get current multilingual settings
        current_value = getattr(config, "LOGIN_LOGO_MULTILINGUAL", None) or {}
        if not isinstance(current_value, dict):
            current_value = {}

        if remove:
            if language in current_value:
                del current_value[language]
                self._save_multilingual_setting(current_value)
                self.stdout.write(
                    self.style.SUCCESS(f"Removed login logo for language '{language}'")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"No login logo found for language '{language}'")
                )
            return

        # Save the image file
        try:
            with open(file_path, "rb") as image_file:
                filename = os.path.basename(file_path)
                saved_path = default_storage.save(filename, image_file)
        except FileNotFoundError:
            self.stdout.write(
                self.style.ERROR(
                    f"File at {file_path} does not exist. "
                    "Make sure the specified path is correct"
                )
            )
            return

        # Update multilingual settings
        current_value[language] = os.path.basename(saved_path)
        self._save_multilingual_setting(current_value)

        self.stdout.write(
            self.style.SUCCESS(
                f"Login logo for language '{language}' has been set to {file_path}"
            )
        )

    def _save_multilingual_setting(self, value):
        """Save the LOGIN_LOGO_MULTILINGUAL setting to database."""
        setting, _ = Constance.objects.get_or_create(key="LOGIN_LOGO_MULTILINGUAL")
        setting.value = dumps(value)
        setting.save()
        cache.delete("API_CONFIGURATION")
