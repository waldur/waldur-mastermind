from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.models.signals import (
    post_delete,
    post_init,
    post_save,
    pre_delete,
    pre_init,
    pre_save,
)


class Command(BaseCommand):
    help = "Load data with disabled signals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            "-p",
            dest="path",
            default="waldur.json",
            help="Path to dumped database.",
        )

    def handle(self, *args, **options):
        path = options.get("path")
        for signal in [
            pre_save,
            pre_init,
            pre_delete,
            post_save,
            post_delete,
            post_init,
        ]:
            signal.receivers = []
        call_command("loaddata", path)
