import os

from constance import config
from constance.codecs import loads
from constance.models import Constance
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management import BaseCommand
from django.utils.translation import gettext as _


def drop_unencoded_value(setting_key):
    """Earlier versions of this command stored the bare file name, which the
    codec cannot decode, and Constance decodes the old value on every write."""
    row = Constance.objects.filter(key=setting_key).first()
    if row is None or row.value is None:
        return
    try:
        loads(row.value)
    except ValueError:
        row.delete()


def make_constance_file_value(image_path, setting_key):
    with open(image_path, "rb") as image_content:
        filename = os.path.basename(image_path)
        path = default_storage.save(filename, image_content)
    drop_unencoded_value(setting_key)
    setattr(config, setting_key, os.path.split(path)[1])


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

        if setting_key not in settings.CONSTANCE_CONFIG:
            self.stdout.write(
                self.style.ERROR(f"{setting_key} is not a valid Constance setting")
            )
            return

        definition = settings.CONSTANCE_CONFIG[setting_key]
        if len(definition) < 3 or definition[2] != "image_field":
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
