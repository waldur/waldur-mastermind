from django.core.management.base import BaseCommand

from ... import tasks


class Command(BaseCommand):
    help = "Pull priorities from support backend."

    def handle(self, *args, **options):
        tasks.pull_priorities()
