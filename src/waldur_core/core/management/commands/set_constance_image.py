import os

from django.core.files.storage import default_storage
from django.core.management import BaseCommand
from django.utils.translation import gettext as _

from waldur_core.configuration import registry
from waldur_core.configuration.models import Setting, Source


def make_constance_file_value(image_path, setting_key):
    with open(image_path, "rb") as image_content:
        filename = os.path.basename(image_path)
        path = default_storage.save(filename, image_content)
    Setting.objects.set(
        setting_key,
        os.path.split(path)[1],
        source=Source.CLI,
        source_detail="set_constance_image",
    )


class Command(BaseCommand):
    help = _("A custom command to set Constance image configs with CLI")

    def add_arguments(self, parser):
        parser.add_argument(
            "key",
            metavar="KEY",
            help="Constance settings key",
        )

        parser.add_argument(
            "path",
            metavar="PATH",
            help="Path to a logo",
        )

    def handle(self, *args, **options):
        setting_key = options["key"]
        path = options["path"]

        if not registry.has(setting_key):
            self.stdout.write(
                self.style.ERROR(f"{setting_key} is not a valid Constance setting")
            )
            return

        if registry.field_type(setting_key) != "image_field":
            self.stdout.write(
                self.style.ERROR(f"{setting_key} is not an image setting")
            )
            return

        try:
            make_constance_file_value(path, setting_key)
        except FileNotFoundError:
            self.stdout.write(
                self.style.ERROR(
                    f"File at {path} does not exist. Make sure the specified path is correct"
                )
            )
            return

        self.stdout.write(self.style.SUCCESS(f"{setting_key} has been set to {path}"))
