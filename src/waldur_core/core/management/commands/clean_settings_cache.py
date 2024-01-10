from django.core.cache import cache
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Clean API configuration settings cache."

    def handle(self, *args, **options):
        cache.delete("API_CONFIGURATION")
        self.stdout.write(
            self.style.SUCCESS("API_CONFIGURATION key has been cleaned from cache.")
        )
