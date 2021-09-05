from django.core.management.base import BaseCommand

from waldur_core.core.features import FEATURES


class Command(BaseCommand):
    def handle(self, *args, **options):
        print('## Features')
        print()
        for section in sorted(FEATURES, key=lambda section: section['key']):
            for feature in sorted(section['items'], key=lambda section: section['key']):
                print(
                    f'* **{section["key"]}.{feature["key"]}**: {feature["description"]}'
                )
                print()
