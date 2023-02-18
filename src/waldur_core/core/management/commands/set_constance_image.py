import os

from constance.admin import get_values
from constance.backends.database.models import Constance
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.management import BaseCommand
from django.utils.translation import gettext as _


def make_constance_file_value(image_path, setting_key):
    image_content = open(image_path, 'rb')

    filename = os.path.basename(image_path)
    path = default_storage.save(filename, image_content)
    setting = Constance.objects.get_or_create(key=setting_key)
    setting.value = os.path.split(path)[1]
    setting.save()


class Command(BaseCommand):
    help = _('A custom command to set Constance image configs with CLI')

    def add_arguments(self, parser):
        parser.add_argument(
            'key',
            metavar='KEY',
            help='Constance settings key',
        )

        parser.add_argument(
            'path',
            metavar='PATH',
            help='Path to a logo',
        )

    def handle(self, *args, **options):
        setting_key = options['key']
        path = options['path']

        if setting_key not in get_values():
            self.stdout.write(
                self.style.ERROR(f'{setting_key} is not a valid Constance setting')
            )
            return

        try:
            make_constance_file_value(path, setting_key)
        except FileNotFoundError:
            self.stdout.write(
                self.style.ERROR(
                    f'File at {path} does not exist. Make sure the specified path is correct'
                )
            )
            return

        cache.delete('API_CONFIGURATION')

        self.stdout.write(self.style.SUCCESS(f'{setting_key} has been set to {path}'))
