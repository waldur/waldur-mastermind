import logging

from django.core.management.base import BaseCommand

from waldur_rancher.utils import SyncUser

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = """Sync users from Waldur to Rancher."""

    def handle(self, *args, **options):
        def print_message(count, action):
            if count == 1:
                self.stdout.write(
                    self.style.SUCCESS('%s user has been %s.' % (count, action))
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS('%s users have been %s.' % (count, action))
                )

        result = SyncUser.run()

        for action in ['blocked', 'created', 'activated', 'updated']:
            print_message(result[action], action)
