from django.core.management.base import BaseCommand

from waldur_core.core.metadata import WaldurConfiguration


class Command(BaseCommand):
    def handle(self, *args, **options):
        print(WaldurConfiguration().schema_json())
