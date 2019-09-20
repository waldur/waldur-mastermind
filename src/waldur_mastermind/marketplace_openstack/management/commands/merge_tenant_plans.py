from django.core.management.base import BaseCommand

from ... import utils


class Command(BaseCommand):
    help = """Import OpenStack tenant quotas to marketplace."""

    def handle(self, dry_run, *args, **options):
        utils.merge_plans()
