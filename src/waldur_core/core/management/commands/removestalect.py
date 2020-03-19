from django.contrib.admin.models import LogEntry
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remove instances that have FK to stale content types."

    def handle(self, *args, **options):
        for entry in LogEntry.objects.all():
            if entry.content_type.model_class() is None:
                entry.delete()
