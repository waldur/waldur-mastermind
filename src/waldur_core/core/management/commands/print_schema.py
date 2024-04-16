from django.core.management.base import BaseCommand

from waldur_core.core.metadata import WaldurConfiguration


class Command(BaseCommand):
    help = """Prints Waldur configuration options in JSON Schema format."""

    def handle(self, *args, **options):
        print(WaldurConfiguration().schema_json())
